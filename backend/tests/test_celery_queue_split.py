"""Celery queue split (#379): interactive and background workers.

- Off (CELERY_SPLIT_QUEUES unset, empty or false) no routes are set and
  every task goes to the default queue, which the one worker reads, as
  before the split.
- On, every registered task goes to the interactive or the background
  queue, and the workers read all three queues between them (the
  background worker reads the default queue too).
- The systemd units, the Compose files and deploy.sh agree with
  backend/celery_queues.py, and deploy.sh changes nothing until a split
  worker unit is enabled.

The real Celery app is built in a subprocess: it reads the setting at
import time, and many test files replace backend.celery_app in
sys.modules."""
import json
import os
import pathlib
import re
import signal
import subprocess
import sys

import pytest

from backend import celery_queues as cq

REPO = pathlib.Path(__file__).resolve().parents[2]
UNITS = REPO / "deploy" / "systemd"
DEPLOY = REPO / "deploy.sh"
LAUNCHER = REPO / "backend" / "celery_queues.py"

OFF_VALUES = [None, "", "  ", "false", "False", "0", "no", "off", "maybe"]
ON_VALUES = ["1", "true", "True", "TRUE", "yes", "Yes", " true "]


# --- the setting -------------------------------------------------------------

@pytest.mark.parametrize("value", OFF_VALUES)
def test_setting_off(value, monkeypatch):
    if value is None:
        monkeypatch.delenv(cq.SETTING, raising=False)
    else:
        monkeypatch.setenv(cq.SETTING, value)
    assert cq.split_queues_enabled() is False


@pytest.mark.parametrize("value", ON_VALUES)
def test_setting_on(value, monkeypatch):
    monkeypatch.setenv(cq.SETTING, value)
    assert cq.split_queues_enabled() is True


# --- the routing table -------------------------------------------------------

def test_off_sets_no_routes():
    assert cq.task_routes(False) is None


def test_on_routes_every_task_to_a_split_queue():
    routes = cq.task_routes(True)
    assert set(routes) == set(cq.TASK_QUEUES)
    for name, route in routes.items():
        assert route == {"queue": cq.TASK_QUEUES[name]}
        assert route["queue"] in (cq.INTERACTIVE_QUEUE, cq.BACKGROUND_QUEUE)


def test_the_work_users_wait_for_is_interactive():
    for name in ("llm_completion.generate_llm_response",
                 "llm_completion.prewarm_anthropic_cache",
                 "transcription.transcribe_audio",
                 "streaming_transcription.finalize_draft_streaming",
                 "streaming_transcription.transcribe_chunk_batch",
                 "tts.generate_tts_audio"):
        assert cq.TASK_QUEUES["backend.tasks." + name] == cq.INTERACTIVE_QUEUE
    for name in ("exports.update_user_profile",
                 "profile_batch.poll_profile_batches",
                 "profile_windows.advance_profile_windows",
                 "chat_imports.import_conversations",
                 "imports.refresh_community_archive_snapshot",
                 "external_digest.sweep_external_digests"):
        assert cq.TASK_QUEUES["backend.tasks." + name] == cq.BACKGROUND_QUEUE


def test_workers_read_every_queue_and_only_background_reads_default():
    read = [q for queues in cq.WORKER_QUEUES.values() for q in queues]
    assert sorted(read) == sorted(
        [cq.INTERACTIVE_QUEUE, cq.BACKGROUND_QUEUE, cq.DEFAULT_QUEUE])
    assert cq.WORKER_QUEUES["interactive"] == (cq.INTERACTIVE_QUEUE,)
    assert cq.DEFAULT_QUEUE in cq.WORKER_QUEUES["background"]
    assert cq.DEFAULT_QUEUE == "celery"


# --- the real Celery app -----------------------------------------------------

_PROBE = """
import json
from backend.celery_app import celery
routes = {}
for name in sorted(celery.tasks):
    queue = celery.amqp.router.route({}, name, (), {}).get("queue")
    routes[name] = getattr(queue, "name", queue)
print(json.dumps({"routes": routes,
                  "task_routes_unset": celery.conf.task_routes is None}))
"""


def _real_app_routes(setting):
    env = dict(os.environ)
    # Set, so the app's load_dotenv() keeps them; sqlite and no Sentry,
    # so nothing is contacted.
    env.update({cq.SETTING: setting, "DATABASE_URL": "sqlite:///:memory:",
                "ENCRYPTION_DISABLED": "true", "SENTRY_DSN": "",
                "PYTHONPATH": str(REPO)})
    result = subprocess.run([sys.executable, "-c", _PROBE], cwd=REPO,
                            env=env, capture_output=True, text=True,
                            timeout=120)
    assert result.returncode == 0, result.stderr[-3000:]
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_real_app_off_routes_everything_to_the_default_queue():
    # Empty is what Compose passes for an unset variable.
    probe = _real_app_routes("")
    assert probe["task_routes_unset"] is True
    assert set(probe["routes"].values()) == {"celery"}


def test_real_app_on_routes_every_registered_task():
    probe = _real_app_routes("true")
    assert probe["task_routes_unset"] is False
    ours = {n: q for n, q in probe["routes"].items()
            if not n.startswith("celery.")}
    # Every task registered from backend/tasks/ has a queue, and the table
    # names no task that no longer exists.
    assert set(ours) == set(cq.TASK_QUEUES), (
        "missing: %s; stale: %s" % (sorted(set(ours) - set(cq.TASK_QUEUES)),
                                    sorted(set(cq.TASK_QUEUES) - set(ours))))
    assert ours == cq.TASK_QUEUES
    # Celery's own tasks stay on the default queue, which the background
    # worker reads.
    builtins = {q for n, q in probe["routes"].items() if n.startswith("celery.")}
    assert builtins == {"celery"}


# --- the Compose launcher ----------------------------------------------------

def test_launcher_off_runs_the_command_used_before_the_split():
    options = ["--loglevel=info", "--concurrency=2"]
    assert cq.worker_command("interactive", False, options) == (
        "celery -A backend.celery_app.celery worker "
        "--loglevel=info --concurrency=2").split()
    assert cq.worker_command("background", False, options) is None


def test_launcher_on_reads_each_workers_queues():
    assert cq.worker_command("interactive", True, ["--concurrency=2"]) == [
        "celery", "-A", "backend.celery_app.celery", "worker",
        "-Q", "interactive", "-n", "interactive@%h", "--concurrency=2"]
    assert cq.worker_command("background", True, []) == [
        "celery", "-A", "backend.celery_app.celery", "worker",
        "-Q", "background,celery", "-n", "background@%h"]
    with pytest.raises(ValueError):
        cq.worker_command("beat", True)


def _launcher_env(setting):
    env = dict(os.environ)
    env[cq.SETTING] = setting
    return env


def test_launcher_off_background_waits_and_stops_on_sigterm():
    proc = subprocess.Popen([sys.executable, "-I", str(LAUNCHER), "background",
                             "--concurrency=1"], env=_launcher_env(""),
                            stdout=subprocess.PIPE, text=True)
    try:
        line = proc.stdout.readline()
        assert "does not run" in line
        assert proc.poll() is None  # still there: Compose would restart it
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=10) == 0
    finally:
        if proc.poll() is None:
            proc.kill()


def test_launcher_refuses_an_unknown_role():
    result = subprocess.run([sys.executable, "-I", str(LAUNCHER), "beat"],
                            env=_launcher_env("true"), capture_output=True,
                            text=True, timeout=30)
    assert result.returncode != 0
    assert "usage" in result.stderr


def _service(compose_file, service):
    """A service's block in a Compose file (PyYAML is not a dependency)."""
    text = (REPO / compose_file).read_text()
    m = re.search(rf"^  {re.escape(service)}:.*?(?=^  \S|^\S|\Z)", text,
                  re.M | re.S)
    assert m, (compose_file, service)
    return m.group(0)


def test_compose_workers_start_through_the_launcher():
    for name in ("docker-compose.yml", "docker-compose.override.yml",
                 "docker-compose.prod.yml", "docker-compose.staging.yml"):
        for service, role in (("celery", "interactive"),
                              ("celery-background", "background")):
            block = _service(name, service)
            assert re.search(rf"^    command: .*backend/celery_queues\.py {role} ",
                             block, re.M), (name, service)


def test_compose_passes_the_setting_to_every_process_that_queues_tasks():
    line = "- CELERY_SPLIT_QUEUES=${CELERY_SPLIT_QUEUES:-}"
    for service in ("backend", "celery", "celery-beat"):
        assert line in _service("docker-compose.yml", service), service
    # The background worker is a copy of the interactive one (YAML merge),
    # environment included, in the base file and on staging.
    assert _service("docker-compose.yml", "celery").startswith(
        "  celery: &celery-worker\n")
    assert "    <<: *celery-worker\n" in _service("docker-compose.yml",
                                                  "celery-background")
    assert "    environment: &staging-worker-environment\n" in _service(
        "docker-compose.staging.yml", "celery")
    assert "    environment: *staging-worker-environment\n" in _service(
        "docker-compose.staging.yml", "celery-background")


def test_staging_deploy_starts_the_background_worker():
    workflow = (REPO / ".github" / "workflows" / "deploy-staging.yml").read_text()
    assert "up -d --build --force-recreate backend celery celery-background" \
        in workflow


# --- systemd units -----------------------------------------------------------

def _exec_start(role):
    text = (UNITS / f"write-or-perish-celery-{role}.service").read_text()
    m = re.search(r"^ExecStart=(.*)$", text, re.M)
    assert m, role
    return text, m.group(1)


@pytest.mark.parametrize("role", sorted(cq.WORKER_QUEUES))
def test_unit_reads_the_workers_queues(role):
    text, cmd = _exec_start(role)
    m = re.search(r" -Q (\S+) ", cmd)
    assert m and tuple(m.group(1).split(",")) == cq.WORKER_QUEUES[role]
    # Its own node name; %% so that systemd passes Celery's %h through.
    assert f" -n {role}@%%h " in cmd
    assert f"--pidfile=/home/hrosspet/write-or-perish/celery-{role}.pid" in cmd
    assert f"--logfile=/home/hrosspet/write-or-perish/logs/celery-{role}.log" in cmd
    assert f"PIDFile=/home/hrosspet/write-or-perish/celery-{role}.pid" in text
    # The #312 warm shutdown with the maintainer's 90 s drain.
    assert re.search(r"^ExecStop=.*celery-graceful-stop\.sh \$MAINPID 90$",
                     text, re.M)
    assert "KillMode=mixed" in text


# --- deploy.sh ---------------------------------------------------------------

def _deploy_snippet():
    """deploy.sh's worker-unit block: the unit list and both functions."""
    text = DEPLOY.read_text()
    parts = [re.search(r"^SPLIT_WORKER_UNITS=.*$", text, re.M).group(0)]
    for fn in ("celery_split_installed", "celery_worker_units"):
        m = re.search(rf"^{fn}\(\) \{{\n.*?^\}}$", text, re.M | re.S)
        assert m, fn
        parts.append(m.group(0))
    return "\n".join(parts)


# A stand-in for `sudo systemctl is-enabled|is-active --quiet UNIT`.
_FAKE_SUDO = """
sudo() {
    [ "$1" = systemctl ] || return 99
    case "$2" in
        is-enabled) list="$ENABLED" ;;
        is-active) list="$ACTIVE" ;;
        *) return 98 ;;
    esac
    for u in $list; do [ "$u" = "$4" ] && return 0; done
    return 1
}
"""


def _worker_units(enabled="", active=""):
    script = _FAKE_SUDO + _deploy_snippet() + "\ncelery_worker_units\n"
    result = subprocess.run(["bash", "-c", script], capture_output=True,
                            text=True, timeout=30,
                            env={"PATH": os.environ.get("PATH", ""),
                                 "ENABLED": enabled, "ACTIVE": active})
    assert result.returncode == 0, result.stderr
    return result.stdout.split()


SPLIT = ["write-or-perish-celery-interactive", "write-or-perish-celery-background"]


def test_deploy_restarts_the_single_worker_until_a_split_unit_is_enabled():
    assert _worker_units() == ["write-or-perish-celery"]
    assert _worker_units(enabled="write-or-perish-celery",
                         active="write-or-perish-celery") == [
        "write-or-perish-celery"]
    # Copied into place but not enabled yet: still the single worker.
    assert _worker_units(active="write-or-perish-celery") == [
        "write-or-perish-celery"]


def test_deploy_restarts_the_split_workers_once_enabled():
    assert _worker_units(enabled=" ".join(SPLIT)) == SPLIT
    # While switching over, the single worker still running is restarted
    # with them.
    assert _worker_units(enabled=" ".join(SPLIT),
                         active="write-or-perish-celery") == SPLIT + [
        "write-or-perish-celery"]


def test_deploy_installs_no_split_unit_itself():
    text = DEPLOY.read_text()
    assert not re.search(r"systemctl (enable|start)\b.*celery-(interactive|background)",
                         text)
    assert re.search(r"if ! celery_split_installed; then\n\s+sudo systemctl "
                     r"enable write-or-perish-celery ", text)
    assert "deploy/systemd/$unit.service" in text
    # Every stop and restart of the workers goes through the unit list.
    assert "systemctl stop $CELERY_WORKER_UNITS" in text
    assert "systemctl restart $CELERY_WORKER_UNITS" in text
    assert not re.search(r"systemctl (stop|restart) write-or-perish-celery\b(?!-)",
                         text)


@pytest.mark.parametrize("line", [
    "CELERY_SPLIT_QUEUES=true", "CELERY_SPLIT_QUEUES=1", "CELERY_SPLIT_QUEUES=yes",
    "CELERY_SPLIT_QUEUES=TRUE", 'CELERY_SPLIT_QUEUES="true"',
    "CELERY_SPLIT_QUEUES='yes'", "CELERY_SPLIT_QUEUES=true  ",
    "CELERY_SPLIT_QUEUES=", "CELERY_SPLIT_QUEUES=false", "CELERY_SPLIT_QUEUES=0",
    "CELERY_SPLIT_QUEUES=no", "# CELERY_SPLIT_QUEUES=true",
    "OTHER_CELERY_SPLIT_QUEUES=true", "CELERY_SPLIT_QUEUES_X=true",
])
def test_deploy_reads_the_setting_as_the_app_does(line):
    assignment = re.search(r"^CELERY_SPLIT_ON_RE=.*$", DEPLOY.read_text(),
                           re.M).group(0)
    # The check exactly as deploy.sh runs it, on a one-line env file.
    script = assignment + '\ngrep -qE "$CELERY_SPLIT_ON_RE"\n'
    grep = subprocess.run(["bash", "-c", script], input=line + "\n",
                          text=True, timeout=30)
    key, _, value = line.partition("=")
    app_on = key == cq.SETTING and cq.split_queues_enabled(value.strip("\"'"))
    assert (grep.returncode == 0) == app_on, line
