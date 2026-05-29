"""Tests for backend.utils.timefmt.

Covers the two consumer-visible behaviors introduced for timezone handling:

  - iso_utc(): serialized timestamps carry an explicit UTC marker (#128).
  - local_stamp(): the per-message LLM temporal-grounding prefix has the exact
    "[YYYY-MM-DD HH:MM TZ]" shape and converts naive-UTC timestamps into the
    target timezone (#130). The model's *reasoning* about time isn't
    deterministically testable, but the prefix format and conversion are.
  - is_valid_timezone(): validation used by PATCH /dashboard/timezone.

This module imports only stdlib, so no Flask app or DB is required.
"""

import re
from datetime import datetime, timezone

from backend.utils.timefmt import iso_utc, local_stamp, is_valid_timezone


# A stored timestamp is naive UTC (matches the db.DateTime + datetime.utcnow
# convention used throughout the models).
NAIVE_UTC = datetime(2026, 5, 29, 14, 30, 0)

# Exact prefix shape the LLM context relies on: [YYYY-MM-DD HH:MM TZ]
STAMP_RE = re.compile(
    r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2} [A-Za-z0-9:+\-]+\]$"
)


# ── iso_utc (#128) ──────────────────────────────────────────────────────────

def test_iso_utc_naive_gets_z_suffix():
    assert iso_utc(NAIVE_UTC) == "2026-05-29T14:30:00Z"


def test_iso_utc_aware_keeps_offset():
    aware = datetime(2026, 5, 29, 14, 30, 0, tzinfo=timezone.utc)
    assert iso_utc(aware) == "2026-05-29T14:30:00+00:00"


def test_iso_utc_none_is_none():
    assert iso_utc(None) is None


# ── local_stamp (#130) ────────────────────────────────────────────────────────

def test_local_stamp_format_shape():
    stamp = local_stamp(NAIVE_UTC, "Europe/Prague")
    assert STAMP_RE.match(stamp), stamp


def test_local_stamp_utc():
    # Naive UTC interpreted as UTC, rendered in UTC.
    assert local_stamp(NAIVE_UTC, "UTC") == "[2026-05-29 14:30 UTC]"


def test_local_stamp_default_tz_is_utc():
    assert local_stamp(NAIVE_UTC, None) == "[2026-05-29 14:30 UTC]"


def test_local_stamp_converts_to_user_timezone():
    # 14:30 UTC on 2026-05-29 is 16:30 CEST in Prague (DST active).
    assert local_stamp(NAIVE_UTC, "Europe/Prague") == "[2026-05-29 16:30 CEST]"


def test_local_stamp_new_york():
    # 14:30 UTC is 10:30 EDT in New York (DST active in May).
    assert local_stamp(NAIVE_UTC, "America/New_York") == "[2026-05-29 10:30 EDT]"


def test_local_stamp_invalid_tz_falls_back_to_utc():
    assert local_stamp(NAIVE_UTC, "Not/AReal_Zone") == "[2026-05-29 14:30 UTC]"


def test_local_stamp_none_datetime():
    assert local_stamp(None, "UTC") == "[unknown time]"


def test_local_stamp_uniform_across_message_types():
    # All message types (user / assistant / deleted-tombstone) are stamped by
    # the same call; the prefix is identical for the same node timestamp.
    user_line = f"{local_stamp(NAIVE_UTC, 'UTC')} author alice: hi"
    assistant_line = f"{local_stamp(NAIVE_UTC, 'UTC')} sure, here you go"
    assert user_line.startswith("[2026-05-29 14:30 UTC] ")
    assert assistant_line.startswith("[2026-05-29 14:30 UTC] ")


# ── is_valid_timezone ─────────────────────────────────────────────────────────

def test_is_valid_timezone_accepts_iana_name():
    assert is_valid_timezone("Europe/Prague") is True


def test_is_valid_timezone_accepts_utc():
    assert is_valid_timezone("UTC") is True


def test_is_valid_timezone_rejects_garbage():
    assert is_valid_timezone("Not/AReal_Zone") is False


def test_is_valid_timezone_rejects_empty_and_none():
    assert is_valid_timezone("") is False
    assert is_valid_timezone(None) is False
