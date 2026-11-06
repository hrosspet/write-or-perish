"""Tests for the disk-full alert (backend/utils/disk_alert.py).

Pure logic: usage and the sender are injected, time is patched, no app.
"""
import pathlib
import sys
from unittest.mock import MagicMock

import pytest

sys.modules.setdefault("celery", MagicMock())
sys.modules.setdefault("celery.utils", MagicMock())
sys.modules.setdefault("celery.utils.log", MagicMock())

from backend.utils import disk_alert  # noqa: E402

GB = 1024 ** 3
ROOT = pathlib.Path(__file__).resolve().parents[1]


def usage(percent):
    return (100 * GB, percent * GB, (100 - percent) * GB)


def cfg(**kw):
    c = {"TESTING": True, "DISK_ALERT_ENABLED": True,
         "SPEND_ALERT_EMAIL": "ops@example.com"}
    c.update(kw)
    return c


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    disk_alert._local_claims.clear()
    clock = {"t": 1000.0}
    monkeypatch.setattr(disk_alert.time, "monotonic", lambda: clock["t"])
    yield clock
    disk_alert._local_claims.clear()


def test_below_threshold_sends_nothing():
    send = MagicMock()
    r = disk_alert.check_disk_and_alert(cfg(), usage(89), send)
    assert not r["sent"]
    send.assert_not_called()


def test_at_threshold_sends_once_with_figures():
    send = MagicMock()
    r = disk_alert.check_disk_and_alert(cfg(), usage(90), send)
    assert r["sent"]
    send.assert_called_once()
    kw = send.call_args.kwargs
    assert kw["to_email"] == "ops@example.com"
    assert kw["percent"] == 90.0
    assert (kw["used_gb"], kw["total_gb"], kw["free_gb"]) == (90, 100, 10)


def test_not_again_within_24h_but_again_after(clean_state):
    send = MagicMock()
    disk_alert.check_disk_and_alert(cfg(), usage(91), send)
    clean_state["t"] += 23 * 3600
    disk_alert.check_disk_and_alert(cfg(), usage(92), send)
    assert send.call_count == 1
    clean_state["t"] += 2 * 3600
    disk_alert.check_disk_and_alert(cfg(), usage(92), send)
    assert send.call_count == 2


def test_crossing_95_sends_again_once():
    send = MagicMock()
    disk_alert.check_disk_and_alert(cfg(), usage(91), send)
    disk_alert.check_disk_and_alert(cfg(), usage(96), send)
    disk_alert.check_disk_and_alert(cfg(), usage(97), send)
    assert send.call_count == 2
    assert send.call_args.kwargs["critical"] is True


def test_first_seen_at_96_then_falling_to_92_sends_once():
    send = MagicMock()
    disk_alert.check_disk_and_alert(cfg(), usage(96), send)
    disk_alert.check_disk_and_alert(cfg(), usage(92), send)
    assert send.call_count == 1


def test_dropping_below_resets_state():
    send = MagicMock()
    disk_alert.check_disk_and_alert(cfg(), usage(91), send)
    disk_alert.check_disk_and_alert(cfg(), usage(70), send)
    disk_alert.check_disk_and_alert(cfg(), usage(91), send)
    assert send.call_count == 2


def test_disabled_sends_nothing():
    send = MagicMock()
    r = disk_alert.check_disk_and_alert(
        cfg(DISK_ALERT_ENABLED=False), usage(99), send)
    assert r == {"status": "disabled"}
    send.assert_not_called()


def test_custom_threshold():
    send = MagicMock()
    disk_alert.check_disk_and_alert(
        cfg(DISK_ALERT_PERCENT=80), usage(85), send)
    assert send.call_count == 1


def test_failed_send_is_retried_next_run():
    send = MagicMock(side_effect=[RuntimeError("smtp"), None])
    r1 = disk_alert.check_disk_and_alert(cfg(), usage(91), send)
    r2 = disk_alert.check_disk_and_alert(cfg(), usage(91), send)
    assert not r1["sent"] and r2["sent"]


def test_measures_a_real_path():
    r = disk_alert.check_disk_and_alert(cfg(DISK_ALERT_PERCENT=101),
                                        None, MagicMock())
    assert 0 <= r["percent"] <= 100


def test_task_registered_in_beat_schedule_and_imported():
    text = (ROOT / "celery_app.py").read_text()
    assert "'task': 'backend.tasks.disk_monitor.check_disk_usage'" in text
    assert "from backend.tasks import disk_monitor  # noqa: F401" in text
    assert "backend.tasks.disk_monitor.check_disk_usage" in (
        ROOT / "tasks" / "disk_monitor.py").read_text()
