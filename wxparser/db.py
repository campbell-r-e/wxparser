"""Local SQLite store (PLAN §6, §8).

Public-domain SQLite (stdlib), fully offline. Append-only history of observations,
forecast issuances, and alerts — so consumers can ask "what's current?" and also,
later, "what did we forecast for Tuesday vs. what actually happened?" (forecast
rows carry valid_from/valid_to; observations carry captured_at).

Writers run in the capture service; the API service opens its own read connection.
WAL mode lets the reader and writer coexist across processes.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    id TEXT PRIMARY KEY,
    captured_at TEXT NOT NULL,
    station TEXT,
    fields TEXT NOT NULL          -- json: field -> {value,votes,total,source}
);
CREATE INDEX IF NOT EXISTS ix_obs_time ON observations(captured_at);

CREATE TABLE IF NOT EXISTS forecasts (
    issued_at TEXT NOT NULL,
    period TEXT NOT NULL,
    valid_from TEXT,
    valid_to TEXT,
    high_f INTEGER,
    low_f INTEGER,
    precip_pct INTEGER,
    sky TEXT,
    source TEXT DEFAULT 'voice',
    PRIMARY KEY (issued_at, period)
);
CREATE INDEX IF NOT EXISTS ix_fc_time ON forecasts(issued_at);

CREATE TABLE IF NOT EXISTS alerts (
    id TEXT PRIMARY KEY,
    captured_at TEXT NOT NULL,
    event TEXT,
    event_label TEXT,
    areas TEXT,                   -- json list
    counties TEXT,                -- json list
    purge_minutes INTEGER,
    issued_raw TEXT,
    station TEXT,
    raw TEXT,
    expires_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_alert_exp ON alerts(expires_at);
"""

_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def period_window(period: str, issued: datetime) -> tuple[str | None, str | None]:
    """Best-effort absolute valid window for a relative forecast period name."""
    p = period.lower().strip()
    day = issued.replace(hour=0, minute=0, second=0, microsecond=0)
    if p in ("today", "this afternoon", "this morning", "rest of today"):
        return _iso(day.replace(hour=6)), _iso(day.replace(hour=18))
    if p in ("tonight", "this evening", "overnight", "rest of tonight"):
        return _iso(day.replace(hour=18)), _iso((day + timedelta(days=1)).replace(hour=6))
    night = p.endswith(" night")
    name = p[:-6].strip() if night else p
    if name in _WEEKDAYS:
        delta = (_WEEKDAYS.index(name) - issued.weekday()) % 7
        target = day + timedelta(days=delta or (7 if delta == 0 else 0))
        if night:
            return _iso(target.replace(hour=18)), _iso((target + timedelta(days=1)).replace(hour=6))
        return _iso(target.replace(hour=6)), _iso(target.replace(hour=18))
    return None, None


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # --- writers ---------------------------------------------------------- #
    def write_observation(self, record: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO observations(id,captured_at,station,fields) VALUES(?,?,?,?)",
                (record["id"], record["captured_at"], record.get("station"),
                 json.dumps(record["fields"])),
            )
            self._conn.commit()

    def write_forecast(self, periods: list[dict], issued_at: str) -> None:
        issued_dt = _parse_iso(issued_at)
        with self._lock:
            for p in periods:
                vf, vt = period_window(p["period"], issued_dt)
                self._conn.execute(
                    "INSERT OR REPLACE INTO forecasts"
                    "(issued_at,period,valid_from,valid_to,high_f,low_f,precip_pct,sky,source)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (issued_at, p["period"], vf, vt, p.get("high_f"), p.get("low_f"),
                     p.get("precip_pct"), p.get("sky"), p.get("source", "voice")),
                )
            self._conn.commit()

    def write_alert(self, record: dict) -> None:
        a = record["alert"]
        captured = record["captured_at"]
        expires = _iso(_parse_iso(captured) + timedelta(minutes=a.get("purge_minutes", 0)))
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO alerts"
                "(id,captured_at,event,event_label,areas,counties,purge_minutes,"
                "issued_raw,station,raw,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (record["id"], captured, a.get("event"), a.get("event_label"),
                 json.dumps(a.get("areas", [])), json.dumps(a.get("counties", [])),
                 a.get("purge_minutes"), a.get("issued_raw"), a.get("station"),
                 a.get("raw"), expires),
            )
            self._conn.commit()

    # --- readers ---------------------------------------------------------- #
    def get_current(self, n: int = 12) -> dict | None:
        """Latest value of each field, merged across the last n observations.

        Fields can be reported in different observations (and a restart can emit a
        sparse one), so the freshest reading of each field wins rather than the
        last single snapshot.
        """
        rows = self._conn.execute(
            "SELECT captured_at, station, fields FROM observations "
            "ORDER BY captured_at DESC LIMIT ?", (n,)
        ).fetchall()
        if not rows:
            return None
        merged: dict = {}
        for r in reversed(rows):  # oldest -> newest, so newer overwrites
            merged.update(json.loads(r["fields"]))
        return {
            "captured_at": rows[0]["captured_at"],
            "station": rows[0]["station"],
            "fields": merged,
        }

    def get_forecast(self) -> dict:
        latest = self._conn.execute("SELECT MAX(issued_at) AS m FROM forecasts").fetchone()
        if not latest or latest["m"] is None:
            return {"issued_at": None, "periods": []}
        rows = self._conn.execute(
            "SELECT * FROM forecasts WHERE issued_at=? ORDER BY rowid", (latest["m"],)
        ).fetchall()
        return {"issued_at": latest["m"], "periods": [dict(r) for r in rows]}

    def get_active_alerts(self, now: str | None = None) -> list[dict]:
        now = now or _iso(datetime.now(timezone.utc))
        rows = self._conn.execute(
            "SELECT * FROM alerts WHERE expires_at > ? ORDER BY captured_at DESC", (now,)
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["areas"] = json.loads(d["areas"] or "[]")
            d["counties"] = json.loads(d["counties"] or "[]")
            out.append(d)
        return out

    def close(self) -> None:
        self._conn.close()
