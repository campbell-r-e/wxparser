"""PostgreSQL store (PLAN §6, §8).

PostgreSQL (permissive PostgreSQL License) via pg8000 — a pure-Python BSD driver,
so nothing copyleft is imported (§2.2). Runs locally on the box, so the offline
constraint (§2.1) still holds.

Native typing: timestamps are `timestamptz`, the voted-field detail and SAME area
lists are `jsonb`, and the hot current-conditions fields are promoted to typed
columns so `/current` is a plain typed query. Append-only history; forecast rows
carry valid_from/valid_to so "predicted vs. what actually happened" is a join.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone

import pg8000.native

from .config import CONFIG, Config

# current-conditions fields promoted from the voted snapshot into typed columns
_PROMOTED = {
    "temperature_f": "integer",
    "dewpoint_f": "integer",
    "humidity_pct": "integer",
    "pressure_in": "double precision",
    "pressure_trend": "text",
    "wind": "text",
    "wind_speed_mph": "integer",
    "sky": "text",
}

_SCHEMA = [
    "CREATE TABLE IF NOT EXISTS observations ("
    "id TEXT PRIMARY KEY, captured_at TIMESTAMPTZ NOT NULL, station TEXT, "
    + ", ".join(f"{c} {t}" for c, t in _PROMOTED.items())
    + ", fields JSONB NOT NULL)",
    "CREATE INDEX IF NOT EXISTS ix_obs_time ON observations(captured_at)",
    """CREATE TABLE IF NOT EXISTS forecasts (
        issued_at TIMESTAMPTZ NOT NULL,
        period TEXT NOT NULL,
        valid_from TIMESTAMPTZ,
        valid_to TIMESTAMPTZ,
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
        captured_at TIMESTAMPTZ NOT NULL,
        event TEXT,
        event_label TEXT,
        areas JSONB,
        counties JSONB,
        purge_minutes INTEGER,
        issued_raw TEXT,
        station TEXT,
        raw TEXT,
        expires_at TIMESTAMPTZ
    )""",
    "CREATE INDEX IF NOT EXISTS ix_alert_exp ON alerts(expires_at)",
]

_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s: str | datetime | None) -> datetime | None:
    if s is None or isinstance(s, datetime):
        return s
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _ts(v) -> str | None:
    """Render a DB timestamptz (datetime) back to ISO-Z for JSON."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return _iso(v.astimezone(timezone.utc))
    return v


def _as_obj(v):
    """jsonb may come back already-parsed or as text depending on the driver."""
    if v is None or isinstance(v, (dict, list)):
        return v
    return json.loads(v)


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


def _field_value(fields: dict, key: str):
    v = fields.get(key)
    return v.get("value") if isinstance(v, dict) else v


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
            user=self._cfg.pg_user, host=self._cfg.pg_host, port=self._cfg.pg_port,
            database=self._database, password=self._cfg.pg_password or None,
        )

    def _run(self, sql: str, **params):
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
        fields = record["fields"]
        cols = ["id", "captured_at", "station", *_PROMOTED, "fields"]
        ph = ["CAST(:fields AS jsonb)" if c == "fields" else f":{c}" for c in cols]
        updates = ", ".join(f"{c}=EXCLUDED.{c}" for c in cols if c != "id")
        params = {
            "id": record["id"],
            "captured_at": _parse_iso(record["captured_at"]),
            "station": record.get("station"),
            "fields": json.dumps(fields),
            **{c: _field_value(fields, c) for c in _PROMOTED},
        }
        self._run(
            f"INSERT INTO observations({', '.join(cols)}) VALUES({', '.join(ph)}) "
            f"ON CONFLICT (id) DO UPDATE SET {updates}",
            **params,
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
                ia=issued_dt, pd=p["period"], vf=_parse_iso(vf), vt=_parse_iso(vt),
                hi=p.get("high_f"), lo=p.get("low_f"), pp=p.get("precip_pct"),
                sky=p.get("sky"), src=p.get("source", "voice"),
            )

    def write_alert(self, record: dict) -> None:
        a = record["alert"]
        captured = _parse_iso(record["captured_at"])
        expires = captured + timedelta(minutes=a.get("purge_minutes", 0))
        self._run(
            "INSERT INTO alerts"
            "(id,captured_at,event,event_label,areas,counties,purge_minutes,"
            "issued_raw,station,raw,expires_at) "
            "VALUES(:id,:ca,:ev,:el,CAST(:ar AS jsonb),CAST(:co AS jsonb),:pm,:ir,:st,:rw,:ex) "
            "ON CONFLICT (id) DO UPDATE SET expires_at=EXCLUDED.expires_at",
            id=record["id"], ca=captured, ev=a.get("event"), el=a.get("event_label"),
            ar=json.dumps(a.get("areas", [])), co=json.dumps(a.get("counties", [])),
            pm=a.get("purge_minutes"), ir=a.get("issued_raw"), st=a.get("station"),
            rw=a.get("raw"), ex=expires,
        )

    # --- readers ---------------------------------------------------------- #
    def get_current(self, n: int = 12) -> dict | None:
        cols = ", ".join(_PROMOTED)
        rows = self._query(
            f"SELECT captured_at, station, {cols}, fields FROM observations "
            "ORDER BY captured_at DESC LIMIT :n", n=n,
        )
        if not rows:
            return None
        conditions: dict = {}
        detail: dict = {}
        for r in reversed(rows):  # oldest -> newest, newer wins
            for c in _PROMOTED:
                if r[c] is not None:
                    conditions[c] = r[c]
            detail.update(_as_obj(r["fields"]) or {})
        return {
            "captured_at": _ts(rows[0]["captured_at"]),
            "station": rows[0]["station"],
            "conditions": conditions,
            "fields": detail,
        }

    def get_forecast(self) -> dict:
        latest = self._query("SELECT MAX(issued_at) AS m FROM forecasts")
        if not latest or latest[0]["m"] is None:
            return {"issued_at": None, "periods": []}
        issued = latest[0]["m"]
        rows = self._query(
            "SELECT * FROM forecasts WHERE issued_at=:ia ORDER BY period", ia=issued,
        )
        for r in rows:
            for k in ("issued_at", "valid_from", "valid_to"):
                r[k] = _ts(r[k])
        return {"issued_at": _ts(issued), "periods": rows}

    def get_active_alerts(self, now: str | None = None) -> list[dict]:
        now_dt = _parse_iso(now) or datetime.now(timezone.utc)
        rows = self._query(
            "SELECT * FROM alerts WHERE expires_at > :now ORDER BY captured_at DESC", now=now_dt,
        )
        for d in rows:
            d["areas"] = _as_obj(d["areas"]) or []
            d["counties"] = _as_obj(d["counties"]) or []
            for k in ("captured_at", "expires_at"):
                d[k] = _ts(d[k])
        return rows

    # --- test helper ------------------------------------------------------ #
    def clear(self) -> None:
        self._run("TRUNCATE observations, forecasts, alerts")

    def close(self) -> None:
        with self._lock:
            self._conn.close()
