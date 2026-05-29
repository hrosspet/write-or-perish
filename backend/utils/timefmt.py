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

from datetime import datetime
from typing import Optional


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
