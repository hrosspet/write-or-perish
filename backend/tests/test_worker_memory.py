"""The workers' memory limits (#613).

- The single worker's unit and the background worker's unit have a
  memory limit (MemoryMax, no swap, no MemoryHigh) and
  OOMPolicy=continue, so a kill inside the limit takes one pool process
  and the unit keeps running; the interactive worker has no limit but the
  same policy.
- Pool processes raise their own oom_score_adj (backend/utils/
  worker_oom.py), so the kernel picks one of them, not the main process.
- deploy.sh installs the single worker's unit file when it changed, and
  the split units once enabled, each followed by a daemon-reload, so the
  limits reach the server with the next deploy.
"""
import os
import pathlib
import re
import subprocess
import sys

import pytest

from backend.utils import worker_oom

REPO = pathlib.Path(__file__).resolve().parents[2]
SINGLE = REPO / "write-or-perish-celery.service"
BACKGROUND = REPO / "deploy" / "systemd" / "write-or-perish-celery-background.service"
INTERACTIVE = REPO / "deploy" / "systemd" / "write-or-perish-celery-interactive.service"
DEPLOY = REPO / "deploy.sh"

_UNITS = {"K": 1024, "M": 1024 ** 2, "G": 1024 ** 3}


def _service(path):
    """The [Service] section's settings (the last value of each key)."""
    section, values = None, {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            section = line
            continue
        if section == "[Service]" and "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values


def _bytes(value):
    m = re.fullmatch(r"(\d+)([KMG]?)", value)
    assert m, value
    return int(m.group(1)) * _UNITS.get(m.group(2), 1)


@pytest.mark.parametrize("unit", [SINGLE, BACKGROUND], ids=lambda p: p.name)
def test_the_worker_units_with_heavy_jobs_have_a_memory_limit(unit):
    s = _service(unit)
    assert s.get("MemoryAccounting") == "yes"
    assert _bytes(s["MemoryMax"]) >= 512 * _UNITS["M"]
    # Past the limit the job is killed, also on a server with swap...
    assert s.get("MemorySwapMax") == "0"
    # ...and not first held in a crawl that slows the whole unit.
    assert "MemoryHigh" not in s
    # A kill takes one pool process; systemd must not stop the unit.
    assert s.get("OOMPolicy") == "continue"
    assert "MemoryLimit" not in s   # the cgroup v1 name, deprecated


def test_the_background_limit_is_below_the_single_workers():
    # The background worker has fewer pool processes and runs beside the
    # interactive worker.
    assert (_bytes(_service(BACKGROUND)["MemoryMax"])
            < _bytes(_service(SINGLE)["MemoryMax"]))


def test_the_interactive_worker_has_no_limit_but_survives_a_kill():
    s = _service(INTERACTIVE)
    assert "MemoryMax" not in s and "MemoryHigh" not in s
    assert s.get("OOMPolicy") == "continue"


def test_deploy_reloads_systemd_after_copying_a_worker_unit():
    text = DEPLOY.read_text()
    single = text[text.index('CELERY_SERVICE_SOURCE="$PROJECT_DIR/write-or-perish-celery.service"'):]
    single = single[:single.index("# Keep the queue-split worker units current")]
    assert 'sudo cp "$CELERY_SERVICE_SOURCE" "$CELERY_SERVICE_TARGET"' in single
    assert single.index("sudo cp") < single.index("sudo systemctl daemon-reload")
    split = text[text.index("# Keep the queue-split worker units current"):]
    split = split[:split.index("# Install/update Celery beat service")]
    assert 'sudo cp "$src" "$dst"' in split
    assert "sudo systemctl daemon-reload" in split
    # The restart that applies the new limits comes after both.
    assert text.index("sudo systemctl restart $CELERY_WORKER_UNITS") > \
        text.index("# Install/update Celery beat service")


# --- pool processes first ----------------------------------------------------

def test_a_pool_process_raises_its_oom_score(tmp_path):
    path = tmp_path / "oom_score_adj"
    path.write_text("0\n")
    assert worker_oom.raise_oom_score(path) == worker_oom.POOL_PROCESS_OOM_SCORE_RAISE
    assert path.read_text().strip() == str(worker_oom.POOL_PROCESS_OOM_SCORE_RAISE)


def test_the_raise_adds_to_the_inherited_value_and_stops_at_1000(tmp_path):
    path = tmp_path / "oom_score_adj"
    path.write_text("-200\n")
    assert worker_oom.raise_oom_score(path, by=300) == 100
    path.write_text("900\n")
    assert worker_oom.raise_oom_score(path, by=300) == 1000
    assert path.read_text().strip() == "1000"


def test_no_proc_file_changes_nothing(tmp_path):
    assert worker_oom.raise_oom_score(tmp_path / "missing") is None
    bad = tmp_path / "bad"
    bad.write_text("not a number")
    assert worker_oom.raise_oom_score(bad) is None
    assert bad.read_text() == "not a number"


def test_the_handler_raises_the_score(monkeypatch):
    calls = []
    monkeypatch.setattr(worker_oom, "raise_oom_score",
                        lambda *a, **kw: calls.append(1))
    worker_oom.on_pool_process_start(sender=None)
    assert calls == [1]


_PROBE = """
import weakref
from celery.signals import worker_process_init
import backend.celery_app  # noqa: F401
names = []
for _, receiver in worker_process_init.receivers:
    if isinstance(receiver, weakref.ReferenceType):
        receiver = receiver()
    names.append(getattr(receiver, "__name__", None))
print("on_pool_process_start" in names)
"""


def test_the_real_app_runs_it_when_a_pool_process_starts():
    env = dict(os.environ)
    env.update({"DATABASE_URL": "sqlite:///:memory:",
                "ENCRYPTION_DISABLED": "true", "SENTRY_DSN": "",
                "PYTHONPATH": str(REPO)})
    result = subprocess.run([sys.executable, "-c", _PROBE], cwd=REPO,
                            env=env, capture_output=True, text=True,
                            timeout=120)
    assert result.returncode == 0, result.stderr[-3000:]
    assert result.stdout.strip().splitlines()[-1] == "True"
