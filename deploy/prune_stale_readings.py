#!/usr/bin/env python3
"""Age out very stale out-of-state city readings (roadmap follow-up).

The current-conditions view (`city_conditions`) keeps one row per city ever heard
in the regional roundup, so a nearby/out-of-state city that has dropped off the
broadcast lingers forever — only flagged `stale`. This prunes those: it deletes
non-home-city rows whose `last_seen` is older than `WX_STALE_PRUNE_HOURS`
(default 24h). The home city (`WX_PRIMARY_CITY`) is always kept even if stale,
and the append-only history (`city_observations`) is left untouched — only the
"latest value" view is decluttered. A pruned city simply reappears when next
heard.

Standalone (python3 + pg8000 only), same WX_PG_* env vars as the service.

Run by hand:
    python3 deploy/prune_stale_readings.py
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pg8000.native

PRIMARY = os.environ.get("WX_PRIMARY_CITY", "Muncie")
HOURS = int(os.environ.get("WX_STALE_PRUNE_HOURS", "24"))


def _connect() -> pg8000.native.Connection:
    return pg8000.native.Connection(
        user=os.environ.get("WX_PG_USER", "wxparser"),
        host=os.environ.get("WX_PG_HOST", "127.0.0.1"),
        port=int(os.environ.get("WX_PG_PORT", "5432")),
        database=os.environ.get("WX_PG_DATABASE", "wxparser"),
        password=os.environ.get("WX_PG_PASSWORD") or None,
    )


def main() -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cutoff = datetime.now(timezone.utc) - timedelta(hours=HOURS)
    conn = _connect()
    try:
        rows = conn.run(
            "DELETE FROM city_conditions WHERE city <> :p AND last_seen < :cut "
            "RETURNING city, condition",
            p=PRIMARY, cut=cutoff,
        )
    finally:
        conn.close()
    n = len(rows or [])
    if n:
        cities = sorted({r[0] for r in rows})
        print(f"[{stamp}] pruned {n} reading(s) not heard in >{HOURS}h "
              f"from {len(cities)} cities: {', '.join(cities)}")
    else:
        print(f"[{stamp}] done — nothing older than {HOURS}h to prune.")


if __name__ == "__main__":
    main()
