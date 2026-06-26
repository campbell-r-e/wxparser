"""PostgreSQL store (PLAN §6, §8) — generic, city-agnostic.

PostgreSQL (permissive license) via pg8000 (pure-Python, BSD — nothing copyleft
imported, §2.2); runs locally so §2.1 (offline) holds.

Conditions are stored long-format: one row per (city, condition) reading with a
timestamp and the vote provenance, so the same store answers "every city's latest
temperature", "this city's history between two times", etc. Forecasts are tagged
with the area/city they cover. Everything is append-only history.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone

import pg8000.native

from .config import CONFIG, Config

from .extract import ALMANAC_NUMERIC

_NUMERIC_CONDITIONS = {
    "temperature_f", "dewpoint_f", "humidity_pct", "pressure_in", "wind_speed_mph",
}

_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS city_observations (
        captured_at TIMESTAMPTZ NOT NULL,
        city TEXT NOT NULL,
        condition TEXT NOT NULL,
        value_num DOUBLE PRECISION,
        value_text TEXT,
        votes INTEGER,
        total INTEGER,
        PRIMARY KEY (captured_at, city, condition)
    )""",
    "CREATE INDEX IF NOT EXISTS ix_cobs_cond ON city_observations(condition, captured_at)",
    "CREATE INDEX IF NOT EXISTS ix_cobs_city ON city_observations(city, captured_at)",
    # latest value + cumulative sightings per (city, condition); sightings gates
    # STT-garbage one-off city names out of the public endpoints.
    """CREATE TABLE IF NOT EXISTS city_conditions (
        city TEXT NOT NULL,
        condition TEXT NOT NULL,
        value_num DOUBLE PRECISION,
        value_text TEXT,
        votes INTEGER,
        total INTEGER,
        sightings INTEGER NOT NULL DEFAULT 0,
        first_seen TIMESTAMPTZ,
        last_seen TIMESTAMPTZ,
        PRIMARY KEY (city, condition)
    )""",
    "CREATE INDEX IF NOT EXISTS ix_cc_cond ON city_conditions(condition)",
    """CREATE TABLE IF NOT EXISTS forecasts (
        issued_at TIMESTAMPTZ NOT NULL,
        city TEXT NOT NULL,
        period TEXT NOT NULL,
        valid_from TIMESTAMPTZ,
        valid_to TIMESTAMPTZ,
        high_f INTEGER,
        low_f INTEGER,
        precip_pct INTEGER,
        sky TEXT,
        source TEXT DEFAULT 'voice',
        PRIMARY KEY (issued_at, city, period)
    )""",
    "CREATE INDEX IF NOT EXISTS ix_fc_time ON forecasts(issued_at)",
    "CREATE INDEX IF NOT EXISTS ix_fc_city ON forecasts(city, issued_at)",
    """CREATE TABLE IF NOT EXISTS alerts (
        id TEXT PRIMARY KEY,
        captured_at TIMESTAMPTZ NOT NULL,
        event TEXT, event_label TEXT,
        areas JSONB, counties JSONB,
        purge_minutes INTEGER, issued_raw TEXT, station TEXT, raw TEXT,
        expires_at TIMESTAMPTZ
    )""",
    "CREATE INDEX IF NOT EXISTS ix_alert_exp ON alerts(expires_at)",
    # Structured fields parsed from a spoken warning/statement transcript
    # (extract.extract_alert_details). Keyed by the transcript's report id so
    # re-hearing the same airing updates in place. Linked to SAME alerts at read
    # time by capture-time window (the SAME header and the spoken detail arrive
    # as separate events).
    """CREATE TABLE IF NOT EXISTS alert_details (
        report_id TEXT PRIMARY KEY,
        captured_at TIMESTAMPTZ NOT NULL,
        product_type TEXT,
        until_text TEXT,
        motion JSONB,
        threats JSONB,
        locations JSONB,
        spotter_activation BOOLEAN DEFAULT FALSE,
        text TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS ix_ad_time ON alert_details(captured_at)",
    # Climate/almanac recap (single-site): latest voted value per field plus an
    # append-only history. Mirrors the city_conditions/city_observations split but
    # without a city key (the home station's daily climate summary).
    """CREATE TABLE IF NOT EXISTS almanac (
        field TEXT PRIMARY KEY,
        value_num DOUBLE PRECISION,
        value_text TEXT,
        votes INTEGER,
        total INTEGER,
        sightings INTEGER NOT NULL DEFAULT 0,
        first_seen TIMESTAMPTZ,
        last_seen TIMESTAMPTZ
    )""",
    """CREATE TABLE IF NOT EXISTS almanac_observations (
        captured_at TIMESTAMPTZ NOT NULL,
        field TEXT NOT NULL,
        value_num DOUBLE PRECISION,
        value_text TEXT,
        votes INTEGER,
        total INTEGER,
        PRIMARY KEY (captured_at, field)
    )""",
    "CREATE INDEX IF NOT EXISTS ix_alm_obs ON almanac_observations(field, captured_at)",
]

_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s):
    if s is None or isinstance(s, datetime):
        return s
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _ts(v):
    if isinstance(v, datetime):
        return _iso(v.astimezone(timezone.utc))
    return v


def _as_obj(v):
    if v is None or isinstance(v, (dict, list)):
        return v
    return json.loads(v)


def _value(row: dict):
    if row.get("value_num") is not None:
        n = row["value_num"]
        return int(n) if float(n).is_integer() else n
    return row.get("value_text")


def period_window(period: str, issued: datetime) -> tuple[str | None, str | None]:
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
            user=self._cfg.pg_user, host=self._cfg.pg_host, port=self._cfg.pg_port,
            database=self._database, password=self._cfg.pg_password or None,
        )

    def _run(self, sql: str, **params):
        with self._lock:
            try:
                return self._conn.run(sql, **params)
            except Exception:  # pragma: no cover - reconnect on a dropped connection
                self._conn = self._connect()
                return self._conn.run(sql, **params)

    def _query(self, sql: str, **params) -> list[dict]:
        with self._lock:
            try:
                rows = self._conn.run(sql, **params)
                cols = [c["name"] for c in self._conn.columns]
            except Exception:  # pragma: no cover - reconnect on a dropped connection
                self._conn = self._connect()
                rows = self._conn.run(sql, **params)
                cols = [c["name"] for c in self._conn.columns]
        return [dict(zip(cols, r)) for r in rows]

    # --- writers ---------------------------------------------------------- #
    def record_reading(self, reading: dict, captured_at: str) -> None:
        """Record one heard reading: bump the (city,condition) sightings counter and
        append a history row. Sightings let the read endpoints suppress one-off
        STT-garbage city names."""
        ca = _parse_iso(captured_at)
        cond = reading["condition"]
        num = float(reading["value"]) if cond in _NUMERIC_CONDITIONS else None
        txt = None if cond in _NUMERIC_CONDITIONS else str(reading["value"])
        votes, total = reading.get("votes"), reading.get("total")
        self._run(
            "INSERT INTO city_conditions"
            "(city,condition,value_num,value_text,votes,total,sightings,first_seen,last_seen) "
            "VALUES(:city,:cond,:num,:txt,:votes,:total,1,:ca,:ca) "
            "ON CONFLICT (city,condition) DO UPDATE SET "
            "value_num=EXCLUDED.value_num, value_text=EXCLUDED.value_text, "
            "votes=EXCLUDED.votes, total=EXCLUDED.total, "
            "sightings=city_conditions.sightings+1, last_seen=EXCLUDED.last_seen",
            city=reading["city"], cond=cond, num=num, txt=txt, votes=votes, total=total, ca=ca,
        )
        self._run(
            "INSERT INTO city_observations"
            "(captured_at,city,condition,value_num,value_text,votes,total) "
            "VALUES(:ca,:city,:cond,:num,:txt,:votes,:total) "
            "ON CONFLICT (captured_at,city,condition) DO NOTHING",
            ca=ca, city=reading["city"], cond=cond, num=num, txt=txt, votes=votes, total=total,
        )

    def write_forecast(self, periods: list[dict], issued_at: str, city: str = "Muncie") -> None:
        issued_dt = _parse_iso(issued_at)
        for p in periods:
            vf, vt = period_window(p["period"], issued_dt)
            self._run(
                "INSERT INTO forecasts"
                "(issued_at,city,period,valid_from,valid_to,high_f,low_f,precip_pct,sky,source) "
                "VALUES(:ia,:city,:pd,:vf,:vt,:hi,:lo,:pp,:sky,:src) "
                "ON CONFLICT (issued_at,city,period) DO UPDATE SET "
                "valid_from=EXCLUDED.valid_from, valid_to=EXCLUDED.valid_to, "
                "high_f=EXCLUDED.high_f, low_f=EXCLUDED.low_f, "
                "precip_pct=EXCLUDED.precip_pct, sky=EXCLUDED.sky, source=EXCLUDED.source",
                ia=issued_dt, city=city, pd=p["period"], vf=_parse_iso(vf), vt=_parse_iso(vt),
                hi=p.get("high_f"), lo=p.get("low_f"), pp=p.get("precip_pct"),
                sky=p.get("sky"), src=p.get("source", "voice"),
            )

    def write_alert(self, record: dict) -> None:
        a = record["alert"]
        captured = _parse_iso(record["captured_at"])
        expires = captured + timedelta(minutes=a.get("purge_minutes", 0))
        self._run(
            "INSERT INTO alerts(id,captured_at,event,event_label,areas,counties,"
            "purge_minutes,issued_raw,station,raw,expires_at) "
            "VALUES(:id,:ca,:ev,:el,CAST(:ar AS jsonb),CAST(:co AS jsonb),:pm,:ir,:st,:rw,:ex) "
            "ON CONFLICT (id) DO UPDATE SET expires_at=EXCLUDED.expires_at",
            id=record["id"], ca=captured, ev=a.get("event"), el=a.get("event_label"),
            ar=json.dumps(a.get("areas", [])), co=json.dumps(a.get("counties", [])),
            pm=a.get("purge_minutes"), ir=a.get("issued_raw"), st=a.get("station"),
            rw=a.get("raw"), ex=expires,
        )

    def write_alert_detail(self, report_id: str, captured_at: str,
                           product_type: str, details: dict, text: str = "") -> None:
        """Persist structured fields parsed from a spoken warning transcript."""
        ca = _parse_iso(captured_at)
        self._run(
            "INSERT INTO alert_details"
            "(report_id,captured_at,product_type,until_text,motion,threats,"
            "locations,spotter_activation,text) "
            "VALUES(:id,:ca,:pt,:ut,CAST(:mo AS jsonb),CAST(:th AS jsonb),"
            "CAST(:lo AS jsonb),:sp,:tx) "
            "ON CONFLICT (report_id) DO UPDATE SET "
            "captured_at=EXCLUDED.captured_at, product_type=EXCLUDED.product_type, "
            "until_text=EXCLUDED.until_text, motion=EXCLUDED.motion, "
            "threats=EXCLUDED.threats, locations=EXCLUDED.locations, "
            "spotter_activation=EXCLUDED.spotter_activation, text=EXCLUDED.text",
            id=report_id, ca=ca, pt=product_type, ut=details.get("until"),
            mo=json.dumps(details.get("motion")) if details.get("motion") else None,
            th=json.dumps(details.get("threats")) if details.get("threats") else None,
            lo=json.dumps(details.get("locations")) if details.get("locations") else None,
            sp=bool(details.get("spotter_activation")), tx=text,
        )

    def record_almanac(self, reading: dict, captured_at: str) -> None:
        """Record one heard almanac field: bump its sightings/latest value and
        append a history row. Mirrors record_reading without a city key."""
        ca = _parse_iso(captured_at)
        field = reading["field"]
        num = float(reading["value"]) if field in ALMANAC_NUMERIC else None
        txt = None if field in ALMANAC_NUMERIC else str(reading["value"])
        votes, total = reading.get("votes"), reading.get("total")
        self._run(
            "INSERT INTO almanac"
            "(field,value_num,value_text,votes,total,sightings,first_seen,last_seen) "
            "VALUES(:f,:num,:txt,:votes,:total,1,:ca,:ca) "
            "ON CONFLICT (field) DO UPDATE SET "
            "value_num=EXCLUDED.value_num, value_text=EXCLUDED.value_text, "
            "votes=EXCLUDED.votes, total=EXCLUDED.total, "
            "sightings=almanac.sightings+1, last_seen=EXCLUDED.last_seen",
            f=field, num=num, txt=txt, votes=votes, total=total, ca=ca,
        )
        self._run(
            "INSERT INTO almanac_observations"
            "(captured_at,field,value_num,value_text,votes,total) "
            "VALUES(:ca,:f,:num,:txt,:votes,:total) "
            "ON CONFLICT (captured_at,field) DO NOTHING",
            ca=ca, f=field, num=num, txt=txt, votes=votes, total=total,
        )

    def latest_almanac(self, min_sightings: int = 1) -> list[dict]:
        """Latest voted value for every almanac field (sightings-gated)."""
        rows = self._query(
            "SELECT field,value_num,value_text,votes,total,sightings,last_seen "
            "FROM almanac WHERE sightings >= :m ORDER BY field", m=min_sightings)
        return [{"field": r["field"], "value": _value(r), "captured_at": _ts(r["last_seen"]),
                 "votes": r["votes"], "total": r["total"], "sightings": r["sightings"]}
                for r in rows]

    def latest_almanac_readings(self) -> list[dict]:
        """field -> value for priming the aggregator on restart."""
        rows = self._query("SELECT field, value_num, value_text FROM almanac")
        return [{"field": r["field"], "value": _value(r)} for r in rows]

    def almanac_since(self, since: str, limit: int, offset: int = 0) -> list[dict]:
        rows = self._query(
            "SELECT captured_at,field,value_num,value_text,votes,total "
            "FROM almanac_observations WHERE captured_at > :s "
            "ORDER BY captured_at LIMIT :lim OFFSET :off",
            s=_parse_iso(since), lim=limit, off=offset)
        return [{"field": r["field"], "value": _value(r), "captured_at": _ts(r["captured_at"]),
                 "votes": r["votes"], "total": r["total"]} for r in rows]

    def alert_details_between(self, frm: str, to: str) -> list[dict]:
        """Structured spoken-alert details captured in [frm, to] (newest first)."""
        rows = self._query(
            "SELECT report_id,captured_at,product_type,until_text,motion,threats,"
            "locations,spotter_activation,text FROM alert_details "
            "WHERE captured_at >= :frm AND captured_at <= :to "
            "ORDER BY captured_at DESC", frm=_parse_iso(frm), to=_parse_iso(to),
        )
        out = []
        for r in rows:
            out.append({
                "report_id": r["report_id"], "captured_at": _ts(r["captured_at"]),
                "product_type": r["product_type"], "until": r["until_text"],
                "motion": _as_obj(r["motion"]), "threats": _as_obj(r["threats"]),
                "locations": _as_obj(r["locations"]),
                "spotter_activation": r["spotter_activation"], "text": r["text"],
            })
        return out

    # --- readers: conditions --------------------------------------------- #
    def list_conditions(self, min_sightings: int = 1) -> list[dict]:
        rows = self._query(
            "SELECT condition, COUNT(*) AS cities, MAX(last_seen) AS latest "
            "FROM city_conditions WHERE sightings >= :m GROUP BY condition ORDER BY condition",
            m=min_sightings,
        )
        return [{"condition": r["condition"], "cities": r["cities"], "latest": _ts(r["latest"])}
                for r in rows]

    def latest_for_condition(self, condition: str, min_sightings: int = 1) -> list[dict]:
        rows = self._query(
            "SELECT city, value_num, value_text, last_seen, votes, total, sightings "
            "FROM city_conditions WHERE condition=:c AND sightings >= :m "
            "ORDER BY city", c=condition, m=min_sightings,
        )
        return [{"city": r["city"], "value": _value(r), "captured_at": _ts(r["last_seen"]),
                 "votes": r["votes"], "total": r["total"], "sightings": r["sightings"]}
                for r in rows]

    def _obs_where(self, condition: str, city: str | None,
                   frm: str | None, to: str | None) -> tuple[str, dict]:
        where = "WHERE condition=:c"
        params: dict = {"c": condition}
        if city:
            where += " AND city=:city"; params["city"] = city
        if frm:
            where += " AND captured_at >= :frm"; params["frm"] = _parse_iso(frm)
        if to:
            where += " AND captured_at <= :to"; params["to"] = _parse_iso(to)
        return where, params

    def condition_history_count(self, condition: str, city: str | None,
                                frm: str | None, to: str | None) -> int:
        where, params = self._obs_where(condition, city, frm, to)
        return self._query(f"SELECT COUNT(*) AS n FROM city_observations {where}", **params)[0]["n"]

    def condition_history(self, condition: str, city: str | None,
                          frm: str | None, to: str | None,
                          limit: int = 1000, offset: int = 0) -> list[dict]:
        where, params = self._obs_where(condition, city, frm, to)
        rows = self._query(
            f"SELECT city,condition,value_num,value_text,captured_at,votes,total "
            f"FROM city_observations {where} ORDER BY captured_at DESC LIMIT :lim OFFSET :off",
            lim=limit, off=offset, **params)
        return [{"city": r["city"], "condition": r["condition"], "value": _value(r),
                 "captured_at": _ts(r["captured_at"]), "votes": r["votes"], "total": r["total"]}
                for r in rows]

    # --- readers: forecast ----------------------------------------------- #
    def latest_forecasts(self) -> list[dict]:
        cities = self._query("SELECT DISTINCT city FROM forecasts")
        out = []
        for c in cities:
            city = c["city"]
            mx = self._query("SELECT MAX(issued_at) AS m FROM forecasts WHERE city=:city", city=city)
            issued = mx[0]["m"]
            rows = self._query(
                "SELECT period,valid_from,valid_to,high_f,low_f,precip_pct,sky,source "
                "FROM forecasts WHERE city=:city AND issued_at=:ia ORDER BY valid_from NULLS LAST, period",
                city=city, ia=issued,
            )
            for r in rows:
                r["valid_from"] = _ts(r["valid_from"]); r["valid_to"] = _ts(r["valid_to"])
            out.append({"city": city, "issued_at": _ts(issued), "periods": rows})
        return out

    def _fc_where(self, frm: str | None, to: str | None, city: str | None) -> tuple[str, dict]:
        where = "WHERE 1=1"
        params: dict = {}
        if city:
            where += " AND city=:city"; params["city"] = city
        if frm:
            where += " AND issued_at >= :frm"; params["frm"] = _parse_iso(frm)
        if to:
            where += " AND issued_at <= :to"; params["to"] = _parse_iso(to)
        return where, params

    def forecast_history_count(self, frm: str | None, to: str | None, city: str | None) -> int:
        where, params = self._fc_where(frm, to, city)
        return self._query(f"SELECT COUNT(*) AS n FROM forecasts {where}", **params)[0]["n"]

    def forecast_history(self, frm: str | None, to: str | None, city: str | None,
                         limit: int = 1000, offset: int = 0) -> list[dict]:
        where, params = self._fc_where(frm, to, city)
        rows = self._query(
            f"SELECT issued_at,city,period,valid_from,valid_to,high_f,low_f,precip_pct,sky "
            f"FROM forecasts {where} ORDER BY issued_at DESC, city, period LIMIT :lim OFFSET :off",
            lim=limit, off=offset, **params)
        for r in rows:
            for k in ("issued_at", "valid_from", "valid_to"):
                r[k] = _ts(r[k])
        return rows

    # --- readers: alerts -------------------------------------------------- #
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

    def alerts_history(self, frm: str | None, to: str | None, event: str | None,
                       limit: int = 1000, offset: int = 0) -> tuple[int, list[dict]]:
        """All SAME alerts (active and expired), newest first, paginated."""
        where = "WHERE 1=1"
        base: dict = {}
        if frm:
            where += " AND captured_at >= :frm"; base["frm"] = _parse_iso(frm)
        if to:
            where += " AND captured_at <= :to"; base["to"] = _parse_iso(to)
        if event:
            where += " AND event = :event"; base["event"] = event
        total = self._query(f"SELECT COUNT(*) AS n FROM alerts {where}", **base)[0]["n"]
        rows = self._query(
            f"SELECT * FROM alerts {where} ORDER BY captured_at DESC LIMIT :lim OFFSET :off",
            lim=limit, off=offset, **base)
        for d in rows:
            d["areas"] = _as_obj(d["areas"]) or []
            d["counties"] = _as_obj(d["counties"]) or []
            for k in ("captured_at", "expires_at"):
                d[k] = _ts(d[k])
        return total, rows

    # --- readers: per-city / index --------------------------------------- #
    def all_conditions_for_city(self, city: str, min_sightings: int = 1) -> list[dict]:
        """Every current condition for one city (the full local observation)."""
        rows = self._query(
            "SELECT condition, value_num, value_text, last_seen, votes, total, sightings "
            "FROM city_conditions WHERE LOWER(city)=LOWER(:c) AND sightings >= :m ORDER BY condition",
            c=city, m=min_sightings)
        return [{"condition": r["condition"], "value": _value(r),
                 "captured_at": _ts(r["last_seen"]), "votes": r["votes"],
                 "total": r["total"], "sightings": r["sightings"]} for r in rows]

    def cities(self, min_sightings: int = 1) -> list[dict]:
        """Distinct cities with data, with per-city condition count and freshness."""
        rows = self._query(
            "SELECT city, COUNT(*) AS conditions, MIN(first_seen) AS first_seen, "
            "MAX(last_seen) AS last_seen FROM city_conditions WHERE sightings >= :m "
            "GROUP BY city ORDER BY city", m=min_sightings)
        return [{"city": r["city"], "conditions": r["conditions"],
                 "first_seen": _ts(r["first_seen"]), "last_seen": _ts(r["last_seen"])}
                for r in rows]

    # --- readers: incremental export (watermark sync) -------------------- #
    def observations_since(self, since: str, limit: int, offset: int = 0) -> list[dict]:
        rows = self._query(
            "SELECT city,condition,value_num,value_text,captured_at,votes,total "
            "FROM city_observations WHERE captured_at > :s "
            "ORDER BY captured_at LIMIT :lim OFFSET :off",
            s=_parse_iso(since), lim=limit, off=offset)
        return [{"city": r["city"], "condition": r["condition"], "value": _value(r),
                 "captured_at": _ts(r["captured_at"]), "votes": r["votes"], "total": r["total"]}
                for r in rows]

    def forecasts_since(self, since: str, limit: int, offset: int = 0) -> list[dict]:
        rows = self._query(
            "SELECT issued_at,city,period,valid_from,valid_to,high_f,low_f,precip_pct,sky "
            "FROM forecasts WHERE issued_at > :s ORDER BY issued_at LIMIT :lim OFFSET :off",
            s=_parse_iso(since), lim=limit, off=offset)
        for r in rows:
            for k in ("issued_at", "valid_from", "valid_to"):
                r[k] = _ts(r[k])
        return rows

    def alerts_since(self, since: str, limit: int, offset: int = 0) -> list[dict]:
        rows = self._query(
            "SELECT * FROM alerts WHERE captured_at > :s ORDER BY captured_at LIMIT :lim OFFSET :off",
            s=_parse_iso(since), lim=limit, off=offset)
        for d in rows:
            d["areas"] = _as_obj(d["areas"]) or []
            d["counties"] = _as_obj(d["counties"]) or []
            for k in ("captured_at", "expires_at"):
                d[k] = _ts(d[k])
        return rows

    def alert_details_since(self, since: str, limit: int, offset: int = 0) -> list[dict]:
        rows = self._query(
            "SELECT report_id,captured_at,product_type,until_text,motion,threats,"
            "locations,spotter_activation,text FROM alert_details WHERE captured_at > :s "
            "ORDER BY captured_at LIMIT :lim OFFSET :off",
            s=_parse_iso(since), lim=limit, off=offset)
        return [{"report_id": r["report_id"], "captured_at": _ts(r["captured_at"]),
                 "product_type": r["product_type"], "until": r["until_text"],
                 "motion": _as_obj(r["motion"]), "threats": _as_obj(r["threats"]),
                 "locations": _as_obj(r["locations"]),
                 "spotter_activation": r["spotter_activation"], "text": r["text"]}
                for r in rows]

    def latest_readings(self) -> list[dict]:
        """All latest (city, condition) readings — used to prime the aggregator."""
        rows = self._query("SELECT city, condition, value_num, value_text FROM city_conditions")
        return [{"city": r["city"], "condition": r["condition"], "value": _value(r)} for r in rows]

    def clear(self) -> None:
        self._run("TRUNCATE city_observations, city_conditions, forecasts, alerts, "
                  "alert_details, almanac, almanac_observations")

    def close(self) -> None:
        with self._lock:
            self._conn.close()
