"""Beat schedule rules (#614): deploy.sh deletes beat's schedule file, so a
daily-or-longer job on an interval restarts its countdown on every deploy.

The schedule is read from the source (AST), not by importing celery_app:
other test modules replace `celery` in sys.modules with a stub.
"""
import ast
import subprocess
import sys
import textwrap
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "celery_app.py"


def _schedule_entries():
    """{entry name: schedule expression node} from beat_schedule."""
    tree = ast.parse(SOURCE.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "beat_schedule":
            out = {}
            for k, v in zip(node.value.keys, node.value.values):
                for kk, vv in zip(v.keys, v.values):
                    if kk.value == "schedule":
                        out[k.value] = vv
            return out
    raise AssertionError("beat_schedule not found")


def _conf_value(name):
    tree = ast.parse(SOURCE.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == name:
            return ast.literal_eval(node.value)


def test_beat_runs_in_utc():
    assert _conf_value("timezone") == "UTC"
    assert _conf_value("enable_utc") is True


def test_no_daily_or_longer_interval_schedule():
    for name, expr in _schedule_entries().items():
        if isinstance(expr, ast.Constant):
            assert expr.value < 86400, f"{name} needs a crontab, not an interval"


def test_cleanup_deleted_nodes_runs_daily_at_fixed_utc_time():
    expr = _schedule_entries()["cleanup-deleted-nodes"]
    assert isinstance(expr, ast.Call) and expr.func.attr == "crontab"
    kwargs = {k.arg: ast.literal_eval(k.value) for k in expr.keywords}
    assert kwargs == {"hour": 3, "minute": 17}


def test_spend_carry_rolls_hourly_at_a_fixed_minute():
    """#594: a new month's carry caps are set by 00:07 UTC on the 1st, also
    when a deploy restarted beat shortly before."""
    expr = _schedule_entries()["roll-spend-carry"]
    assert isinstance(expr, ast.Call) and expr.func.attr == "crontab"
    kwargs = {k.arg: ast.literal_eval(k.value) for k in expr.keywords}
    assert kwargs == {"minute": 7}


def test_fresh_schedule_file_neither_skips_nor_doubles_the_day():
    """Beat restarted with no schedule file starts with last_run_at = now."""
    code = textwrap.dedent("""
        from datetime import datetime, timezone
        from celery.schedules import crontab
        utc = timezone.utc
        def due(last, now):
            s = crontab(hour=3, minute=17, nowfun=lambda: now)
            return s.is_due(last)
        d = lambda h, m, s=0: datetime(2026, 11, 9, h, m, s, tzinfo=utc)
        # restarted before 03:17: runs once at 03:17
        assert due(d(3, 15), d(3, 16)).is_due is False
        assert due(d(3, 15), d(3, 17, 5)).is_due is True
        # restarted after 03:17: not due again today, next in ~7 h
        r = due(d(3, 18), d(20, 0))
        assert r.is_due is False and 6 * 3600 < r.next < 8 * 3600
        print("ok")
    """)
    out = subprocess.run([sys.executable, "-I", "-c", code],
                         capture_output=True, text=True)
    assert out.stdout.strip() == "ok", out.stderr
