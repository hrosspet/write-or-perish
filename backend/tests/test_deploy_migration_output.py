"""deploy.sh's schema step keeps the output of `flask db migrate` and
`flask db upgrade` on the VM (deployment.log, readable by its owner
only) and prints only what happened: what the script prints is the
Actions log of the repo.

Runs deploy.sh's own block in bash, with `flask` and the service stop
replaced by shell functions that print a database error naming a row's
value.
"""
import os
import re
import stat
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEPLOY = REPO / "deploy.sh"
VALUE = "row.value@example.org"


def _deploy_block():
    """The log helpers, the log file's setup and the migrations block."""
    text = DEPLOY.read_text()
    parts = ["set -e"]
    for line in (r'^LOG_FILE=.*$', r'^touch "\$LOG_FILE".*$'):
        m = re.search(line, text, re.M)
        if m:
            parts.append(m.group(0))
    for fn in ("log", "error", "warn"):
        m = re.search(rf"^{fn}\(\) \{{\n.*?^\}}$", text, re.M | re.S)
        assert m, fn
        parts.append(m.group(0))
    m = re.search(r'^log "Running database migrations\.\.\."$.*?'
                  r'^    warn "No migrations directory found, skipping '
                  r'migrations"\nfi$', text, re.M | re.S)
    assert m, "migrations block"
    parts.append(m.group(0))
    return "\n".join(parts)


_FAKES = """
stop_services_for_upgrade() { echo "services stopped"; }
flask() {
    case "$2" in
        migrate)
            echo "INFO  [alembic.runtime.migration] Context impl PostgresqlImpl."
            echo "DETAIL:  Key (email)=($VALUE) is duplicated." >&2
            return "$MIGRATE_EXIT" ;;
        heads) echo "bbbb2222 (head)" ;;
        current)
            if [ -f "$PROJECT_DIR/upgraded" ]; then echo "bbbb2222 (head)";
            else echo "aaaa1111"; fi ;;
        upgrade)
            echo "INFO  [alembic.runtime.migration] Running upgrade aaaa1111 -> bbbb2222"
            if [ "$UPGRADE_EXIT" -ne 0 ]; then
                echo "sqlalchemy.exc.IntegrityError: could not create unique index" >&2
                echo "DETAIL:  Key (email)=($VALUE) is duplicated." >&2
                return "$UPGRADE_EXIT"
            fi
            touch "$PROJECT_DIR/upgraded" ;;
    esac
}
"""


def _run(tmp_path, migrate_exit=0, upgrade_exit=0):
    (tmp_path / "migrations").mkdir()
    script = _FAKES + _deploy_block()
    result = subprocess.run(
        ["bash", "-c", script], cwd=tmp_path, capture_output=True,
        text=True, timeout=60,
        env={"PATH": os.environ.get("PATH", ""), "VALUE": VALUE,
             "PROJECT_DIR": str(tmp_path), "BACKEND_DIR": str(tmp_path),
             "MIGRATE_EXIT": str(migrate_exit),
             "UPGRADE_EXIT": str(upgrade_exit)})
    log_file = tmp_path / "deployment.log"
    return result, log_file


def test_a_failed_upgrade_prints_no_database_text(tmp_path):
    result, log_file = _run(tmp_path, migrate_exit=1, upgrade_exit=1)

    printed = result.stdout + result.stderr
    assert result.returncode == 1
    assert VALUE not in printed
    assert "Database migration failed" in printed
    assert "deployment.log on the VM" in printed
    assert "flask db migrate exited with status 1" in printed
    # The full text stays on the VM, for the owner only.
    kept = log_file.read_text()
    assert kept.count(VALUE) == 2
    assert "Running upgrade aaaa1111 -> bbbb2222" in kept
    assert stat.S_IMODE(log_file.stat().st_mode) == 0o600


def test_a_successful_upgrade_prints_its_revisions(tmp_path):
    result, log_file = _run(tmp_path)

    printed = result.stdout + result.stderr
    assert result.returncode == 0, printed
    assert "Pending migration (aaaa1111 -> bbbb2222)" in printed
    assert "Database now at bbbb2222" in printed
    assert "No model changes detected" in printed
    assert "Running upgrade" not in printed
    assert VALUE not in printed
    assert "Running upgrade aaaa1111 -> bbbb2222" in log_file.read_text()
