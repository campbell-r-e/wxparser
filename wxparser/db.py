"""PostgreSQL store (PLAN §6, §8).

PostgreSQL (permissive PostgreSQL License) via pg8000 — a pure-Python BSD driver,
so nothing copyleft is imported (§2.2). Runs locally on the box, so the offline
constraint (§2.1) still holds. Append-only history of observations, forecast
issuances, and alerts; forecast rows carry valid_from/valid_to so "predicted vs.
what actually happened" is a join. The capture service writes; the API service
opens its own connection — Postgres handles the concurrency natively.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone

import pg8000.native

from .config import CONFIG, Config

_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS observations (
        id TEXT PRIMARY KEY,
        captured_at TEXT NOT NULL,
        station TEXT,
        fields TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS ix_obs_time ON observations(captured_at)",
    """CREATE TABLE IF NOT EXISTS forecasts (
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
    )""",
    "CREATE INDEX IF NOT EXISTS ix_fc_time ON forecasts(issued_at)",
    """CREATE TABLE IF NOT EXISTS alerts (
        id TEXT PRIMARY KEY,
        captured_at TEXT NOT NULL,
        event TEXT,
        event_label TEXT,
        areas TEXT,
        counties TEXT,
        purge_minutes INTEGER,
        issued_raw TEXT,
        station TEXT,
        raw TEXT,
        expires_at TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS ix_alert_exp ON alerts(expires_at)",
]

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
    def __init__(self, cfg: Config = CONFIG, database: str | None = None):
        self._cfg = cfg
        self._database = database or cfg.pg_database
        self._lock = threading.Lock()
        self._conn = self._connect()
        for stmt in _SCHEMA:
            self._conn.run(stmt)

    def _connect(self) -> pg8000.native.Connection:
        return pg8000.native.Connection(
            user=self._cfg.pg_user,
            host=self._cfg.pg_host,
            port=self._cfg.pg_port,
            database=self._database,
            password=self._cfg.pg_password or None,
        )

    def _run(self, sql: str, **params):
        """Execute with a single reconnect retry (long-lived service)."""
        with self._lock:
            try:
                return self._conn.run(sql, **params)
            except Exception:
                self._conn = self._connect()
                return self._conn.run(sql, **params)

    def _query(self, sql: str, **params) -> list[dict]:
        with self._lock:
            try:
                rows = self._conn.run(sql, **params)
                cols = [c["name"] for c in self._conn.columns]
            except Exception:
                self._conn = self._connect()
                rows = self._conn.run(sql, **params)
                cols = [c["name"] for c in self._conn.columns]
        return [dict(zip(cols, r)) for r in rows]

    # --- writers ---------------------------------------------------------- #
    def write_observation(self, record: dict) -> None:
        self._run(
            "INSERT INTO observations(id,captured_at,station,fields) "
            "VALUES(:id,:ca,:st,:f) ON CONFLICT (id) DO UPDATE SET "
            "captured_at=EXCLUDED.captured_at, station=EXCLUDED.station, fields=EXCLUDED.fields",
            id=record["id"], ca=record["captured_at"],
            st=record.get("station"), f=json.dumps(record["fields"]),
        )

    def write_forecast(self, periods: list[dict], issued_at: str) -> None:
        issued_dt = _parse_iso(issued_at)
        for p in periods:
            vf, vt = period_window(p["period"], issued_dt)
            self._run(
                "INSERT INTO forecasts"
                "(issued_at,period,valid_from,valid_to,high_f,low_f,precip_pct,sky,source) "
                "VALUES(:ia,:pd,:vf,:vt,:hi,:lo,:pp,:sky,:src) "
                "ON CONFLICT (issued_at,period) DO UPDATE SET "
                "valid_from=EXCLUDED.valid_from, valid_to=EXCLUDED.valid_to, "
                "high_f=EXCLUDED.high_f, low_f=EXCLUDED.low_f, "
                "precip_pct=EXCLUDED.precip_pct, sky=EXCLUDED.sky, source=EXCLUDED.source",
                ia=issued_at, pd=p["period"], vf=vf, vt=vt, hi=p.get("high_f"),
                lo=p.get("low_f"), pp=p.get("precip_pct"), sky=p.get("sky"),
                src=p.get("source", "voice"),
            )

    def write_alert(self, record: dict) -> None:
        a = record["alert"]
        captured = record["captured_at"]
        expires = _iso(_parse_iso(captured) + timedelta(minutes=a.get("purge_minutes", 0)))
        self._run(
            "INSERT INTO alerts"
            "(id,captured_at,event,event_label,areas,counties,purge_minutes,"
            "issued_raw,station,raw,expires_at) "
            "VALUES(:id,:ca,:ev,:el,:ar,:co,:pm,:ir,:st,:rw,:ex) "
            "ON CONFLICT (id) DO UPDATE SET expires_at=EXCLUDED.expires_at",
            id=record["id"], ca=captured, ev=a.get("event"), el=a.get("event_label"),
            ar=json.dumps(a.get("areas", [])), co=json.dumps(a.get("counties", [])),
            pm=a.get("purge_minutes"), ir=a.get("issued_raw"), st=a.get("station"),
            rw=a.get("raw"), ex=expires,
        )

    # --- readers ---------------------------------------------------------- #
    def get_current(self, n: int = 12) -> dict | None:
        rows = self._query(
            "SELECT captured_at, station, fields FROM observations "
            "ORDER BY captured_at DESC LIMIT :n", n=n,
        )
        if not rows:
            return None
        merged: dict = {}
        for r in reversed(rows):  # oldest -> newest, so newer overwrites
            merged.update(json.loads(r["fields"]))
        return {"captured_at": rows[0]["captured_at"], "station": rows[0]["station"], "fields": merged}

    def get_forecast(self) -> dict:
        latest = self._query("SELECT MAX(issued_at) AS m FROM forecasts")
        if not latest or latest[0]["m"] is None:
            return {"issued_at": None, "periods": []}
        issued = latest[0]["m"]
        rows = self._query(
            "SELECT * FROM forecasts WHERE issued_at=:ia ORDER BY period", ia=issued,
        )
        return {"issued_at": issued, "periods": rows}

    def get_active_alerts(self, now: str | None = None) -> list[dict]:
        now = now or _iso(datetime.now(timezone.utc))
        rows = self._query(
            "SELECT * FROM alerts WHERE expires_at > :now ORDER BY captured_at DESC", now=now,
        )
        for d in rows:
            d["areas"] = json.loads(d["areas"] or "[]")
            d["counties"] = json.loads(d["counties"] or "[]")
        return rows

    # --- test helper ------------------------------------------------------ #
    def clear(self) -> None:
        self._run("TRUNCATE observations, forecasts, alerts")

    def close(self) -> None:
        with self._lock:
            self._conn.close()
