"""Timezone-aware ISO serialization helpers.

All timestamp columns in this project are stored as **naive UTC** (the models
use ``default=datetime.utcnow`` against plain ``db.DateTime`` columns). When we
serialize those to JSON with ``.isoformat()`` the result carries no timezone
marker, so the frontend (and any consumer) has no way to know the value is UTC
and may interpret it as local time — producing wrong "x minutes ago" labels and
clock skew.

``iso_utc()`` is the single decision point for emitting a timestamp: it appends
an explicit ``"Z"`` (Zulu / UTC) suffix when the value is naive, and otherwise
relies on the offset already produced by ``.isoformat()`` for aware datetimes.
"""

from datetime import datetime, timezone
from typing import Optional

try:  # Python 3.9+
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover - fallback for older runtimes
    ZoneInfo = None

    class ZoneInfoNotFoundError(Exception):
        pass


def iso_utc(dt: Optional[datetime]) -> Optional[str]:
    """Serialize a (naive-UTC or aware) datetime to an ISO string with an
    explicit UTC marker.

    - ``None`` -> ``None`` (callers frequently serialize nullable columns).
    - naive datetime (no tzinfo): treated as UTC, ``"Z"`` is appended.
    - aware datetime: ``.isoformat()`` already includes the offset
      (e.g. ``+00:00``), so it is returned unchanged.
    """
    if dt is None:
        return None
    iso = dt.isoformat()
    if dt.tzinfo is None:
        return iso + "Z"
    return iso


def local_stamp(dt: Optional[datetime], tz_name: Optional[str] = None) -> str:
    """Render a stored (naive-UTC or aware) datetime as an absolute local-time
    stamp for LLM temporal grounding (#130).

    Format: ``[YYYY-MM-DD HH:MM TZ]`` where TZ is the timezone abbreviation in
    effect at that instant (e.g. ``CEST``, ``UTC``). The model derives "now"
    from the most recent message's stamp, so every message gets one — there is
    no separate "Today is X" anchor and no relative phrasing.

    Args:
        dt: the timestamp (naive values are interpreted as UTC; ``None`` yields
            a stable ``[unknown time]`` marker rather than crashing context
            assembly).
        tz_name: an IANA timezone name (e.g. ``"Europe/Prague"``). Falls back
            to UTC when ``None``, empty, or unrecognized.
    """
    if dt is None:
        return "[unknown time]"
    # Interpret naive timestamps as UTC (all DB timestamps are naive UTC).
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    target = timezone.utc
    if tz_name and ZoneInfo is not None:
        try:
            target = ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            target = timezone.utc

    local = dt.astimezone(target)
    abbrev = local.strftime("%Z") or "UTC"
    return f"[{local.strftime('%Y-%m-%d %H:%M')} {abbrev}]"


def is_valid_timezone(tz_name: Optional[str]) -> bool:
    """Return True if ``tz_name`` is a resolvable IANA timezone name.

    Used to validate the browser-reported timezone before persisting it.
    "UTC" is always considered valid. When ``zoneinfo`` is unavailable, only
    "UTC" is accepted.
    """
    if not tz_name or not isinstance(tz_name, str):
        return False
    if tz_name == "UTC":
        return True
    if ZoneInfo is None:
        return False
    try:
        ZoneInfo(tz_name)
        return True
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return False
