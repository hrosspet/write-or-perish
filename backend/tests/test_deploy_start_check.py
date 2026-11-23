"""deploy.sh imports the app once, with the new code and the server's env
file, before its schema step and before it stops, restarts or reloads
anything. When the import fails the deploy ends there with a fixed line,
and the reason stays in deployment.log on the VM.

Runs deploy.sh's own block in bash with `python` replaced by a shell
function, as test_deploy_migration_output.py does for the schema step.
"""
import os
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEPLOY = REPO / "deploy.sh"
REASON = "GCP_KMS_KEY_NAME is not set, value-from-the-env-file"
BLOCK = (r'^log "Checking that the app starts with this code and '
         r'\.env\.production\.\.\."$.*?^log "The app starts"$')


def _block():
    text = DEPLOY.read_text()
    parts = ["set -e"]
    for line in (r'^LOG_FILE=.*$', r'^touch "\$LOG_FILE".*$'):
        parts.append(re.search(line, text, re.M).group(0))
    for fn in ("log", "error", "warn"):
        parts.append(re.search(rf"^{fn}\(\) \{{\n.*?^\}}$", text,
                               re.M | re.S).group(0))
    parts.append(re.search(BLOCK, text, re.M | re.S).group(0))
    parts.append('echo "the deploy went on"')
    return "\n".join(parts)


_FAKE_PYTHON = """
python() {
    echo "python $*" >> "$PROJECT_DIR/calls"
    if [ "$IMPORT_EXIT" -ne 0 ]; then
        echo "Traceback (most recent call last):" >&2
        echo "RuntimeError: $REASON" >&2
    fi
    return "$IMPORT_EXIT"
}
"""


def _run(tmp_path, import_exit):
    result = subprocess.run(
        ["bash", "-c", _FAKE_PYTHON + _block()], cwd=tmp_path,
        capture_output=True, text=True, timeout=60,
        env={"PATH": os.environ.get("PATH", ""), "REASON": REASON,
             "PROJECT_DIR": str(tmp_path),
             "IMPORT_EXIT": str(import_exit)})
    return result, tmp_path / "deployment.log"


def test_an_app_that_does_not_start_ends_the_deploy_with_a_fixed_line(
        tmp_path):
    result, log_file = _run(tmp_path, import_exit=1)

    printed = result.stdout + result.stderr
    assert result.returncode == 1
    assert "The app does not start with this code and .env.production" \
        in printed
    assert "Nothing was stopped, restarted or reloaded" in printed
    assert "deployment.log on the VM" in printed
    assert "the deploy went on" not in printed
    # The reason stays on the VM.
    assert REASON not in printed
    assert "Traceback" not in printed
    assert REASON in log_file.read_text()


def test_an_app_that_starts_lets_the_deploy_go_on(tmp_path):
    result, log_file = _run(tmp_path, import_exit=0)

    printed = result.stdout + result.stderr
    assert result.returncode == 0, printed
    assert "The app starts" in printed
    assert "the deploy went on" in printed
    assert (tmp_path / "calls").read_text() == \
        "python -c import backend.app\n"


def test_the_check_comes_before_the_schema_step_and_any_restart():
    text = DEPLOY.read_text()
    check = re.search(BLOCK, text, re.M | re.S)
    assert check
    schema = text.index('log "Running database migrations..."')
    assert check.end() < schema
    # Nothing outside a function body stops, restarts or reloads a
    # service before the check (the helpers above it are only defined).
    for m in re.finditer(r"^(sudo )?systemctl (stop|restart|reload)",
                         text, re.M):
        assert m.start() > check.end()
