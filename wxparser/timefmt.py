"""The project-wide timestamp format, in one place.

Every store column, API field, and log line uses the same second-resolution
UTC ISO-8601 stamp; the sync watermarks (/export, /stream) rely on these
strings sorting chronologically. One shared helper replaces the four private
copies that used to live in api/store/health/db.
"""

from __future__ import annotations

from datetime import datetime, timezone

ISO_FMT = "%Y-%m-%dT%H:%M:%SZ"


def utc_now_iso() -> str:
    """Current UTC time as the project-wide ISO-8601 second stamp."""
    return datetime.now(timezone.utc).strftime(ISO_FMT)
