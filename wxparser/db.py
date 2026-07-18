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
from datetime import date, datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo

import pg8000.exceptions
import pg8000.native

from .config import Config
from .timefmt import ISO_FMT, parse_iso_utc

from .data.same_events import event_label
from .extract import ALMANAC_NUMERIC

# A dropped/broken connection is worth a one-shot reconnect+retry; a SQL or data
# error is NOT (retrying just hides the bug, and for a non-idempotent statement
# like the sightings bump it could double-apply). Catch only connection failures.
_CONN_ERRORS = (pg8000.exceptions.InterfaceError, OSError)

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
    # valid_from/valid_to are NOT stored: they're a pure function of (period,
    # issued_at) via period_window(), so storing them would duplicate the key
    # derivation (a 2NF partial-key dependency — they don't depend on `city`) and
    # could drift if the windowing logic changes. They're computed on read.
    """CREATE TABLE IF NOT EXISTS forecasts (
        issued_at TIMESTAMPTZ NOT NULL,
        city TEXT NOT NULL,
        period TEXT NOT NULL,
        high_f INTEGER,
        low_f INTEGER,
        precip_pct INTEGER,
        sky TEXT,
        source TEXT DEFAULT 'voice',
        confidence JSONB,
        PRIMARY KEY (issued_at, city, period)
    )""",
    "CREATE INDEX IF NOT EXISTS ix_fc_time ON forecasts(issued_at)",
    "CREATE INDEX IF NOT EXISTS ix_fc_city ON forecasts(city, issued_at)",
    # event_label is NOT stored: it's a pure lookup on `event` (event ->
    # event_label is a transitive dependency, a 3NF violation if stored). The
    # label is resolved from data.same_events at read time.
    """CREATE TABLE IF NOT EXISTS alerts (
        id TEXT PRIMARY KEY,
        captured_at TIMESTAMPTZ NOT NULL,
        event TEXT,
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
    # Raw transcript landing zone (PLAN §5.1) — the source of truth the
    # structured tables above are a re-derivable projection of (see reprocess.py).
    # Append-mostly rather than strictly immutable: rows are never deleted, but a
    # term-fix (deploy/fix_stt_terms.py) or supersede rewrites a row in place by id.
    # Replaces the append-only transcripts/reports.jsonl file: `payload` holds the
    # whole report doc, and the columns beside it are denormalized out of it purely
    # so the /transcripts filters (from/to/q/product) and the /export watermark feed
    # stay index-fast. Every record type the pipeline emits lands here — routine
    # transcripts plus the type=same_alert / type=observation envelopes — keyed by
    # the report id so a term-fix or supersede rewrite updates in place.
    """CREATE TABLE IF NOT EXISTS raw_reports (
        id TEXT PRIMARY KEY,
        captured_at TIMESTAMPTZ NOT NULL,
        type TEXT,
        product_type TEXT,
        station TEXT,
        text TEXT,
        payload JSONB NOT NULL,
        fingerprint TEXT,
        supersedes TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS ix_raw_time ON raw_reports(captured_at)",
    "CREATE INDEX IF NOT EXISTS ix_raw_product ON raw_reports(product_type, captured_at)",
    # pipeline liveness heartbeat (see health.py) — published through the DB so
    # the API can assess /health from a different machine than the capture box
    """CREATE TABLE IF NOT EXISTS pipeline_health (
        station TEXT PRIMARY KEY,
        updated_at TIMESTAMPTZ NOT NULL,
        payload JSONB NOT NULL
    )""",
]

# A fixed advisory-lock key serializes schema creation across the co-starting
# wxparser and wxparser-api processes. `CREATE ... IF NOT EXISTS` is NOT race-safe
# against a concurrent create — both sides probe the catalog, both try to create,
# and one loses a pg_catalog unique insert (SQLSTATE 23505 on pg_type). Holding
# this lock means only one process builds the schema at a time; the other waits,
# then its IF NOT EXISTS statements cleanly no-op.
_SCHEMA_LOCK_KEY = 0x77787061  # "wxpa"

# Idempotent migrations to bring a PRE-EXISTING database up to the current schema
# (CREATE TABLE IF NOT EXISTS never alters an existing table). A fresh DB already
# matches via _SCHEMA, so each of these is a no-op there. Run under the same
# advisory lock right after the CREATEs. Append new ALTERs here as the schema
# evolves rather than hand-running them on the box.
_MIGRATIONS = [
    "ALTER TABLE forecasts DROP COLUMN IF EXISTS valid_from",
    "ALTER TABLE forecasts DROP COLUMN IF EXISTS valid_to",
    "ALTER TABLE forecasts ADD COLUMN IF NOT EXISTS confidence JSONB",
    "ALTER TABLE alerts DROP COLUMN IF EXISTS event_label",
]

_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _iso(dt: datetime) -> str:
    return dt.strftime(ISO_FMT)


def _parse_iso(s):
    if s is None or isinstance(s, datetime):
        return s
    return parse_iso_utc(s)


def _ts(v):
    if isinstance(v, datetime):
        return _iso(v.astimezone(timezone.utc))
    return v


def _as_obj(v):
    if v is None or isinstance(v, (dict, list)):
        return v
    return json.loads(v)


def _split_value(value, numeric: bool) -> tuple[float | None, str | None]:
    """Route a reading's value to the numeric or the text column."""
    return (float(value), None) if numeric else (None, str(value))


def _value(row: dict):
    if row.get("value_num") is not None:
        n = row["value_num"]
        return int(n) if float(n).is_integer() else n
    return row.get("value_text")


def _reading(row: dict, *ident: str, ts: str = "captured_at") -> dict:
    """The reading shape every conditions/almanac reader serves: identity
    columns, the voted value, the capture stamp, and vote provenance
    (sightings when the query selected it).
    """
    out = {k: row[k] for k in ident}
    out["value"] = _value(row)
    out["captured_at"] = _ts(row[ts])
    out["votes"] = row["votes"]
    out["total"] = row["total"]
    if "sightings" in row:
        out["sightings"] = row["sightings"]
    return out


def period_window(period: str, issued: datetime,
                  tz: tzinfo | None = None) -> tuple[str | None, str | None]:
    """Derived (valid_from, valid_to) for a forecast period — a pure function
    of (period name, issued_at); computed on read, never stored.

    Periods are named against the station's local calendar day AND local clock, so
    `tz` (the station zone) anchors both. The day: the loop airing at 10pm Wednesday
    is stamped 02:00Z *Thursday*, and reading "Thursday" off the UTC day resolves
    tomorrow's forecast to a week out (and "tonight" to the wrong night). The hours:
    a daytime period runs 06:00-18:00 *local* wall-clock (10:00Z-22:00Z in EDT),
    matching what NWS publishes -- not 06:00Z-18:00Z, which is ~4h early and expires
    "Today" before the afternoon it describes. Without `tz` the boundaries stay UTC
    (the plain callers and their tests are unchanged).
    """
    p = period.lower().strip()
    local = issued.astimezone(tz) if tz else issued
    base = local.date()

    def bound(d: date, hour: int) -> str:
        """Wall-clock `hour` on local day `d`, expressed as a UTC ISO stamp."""
        dt = datetime(d.year, d.month, d.day, hour, tzinfo=tz or timezone.utc)
        return _iso(dt.astimezone(timezone.utc))

    if p in ("today", "this afternoon", "this morning", "rest of today"):
        return bound(base, 6), bound(base, 18)
    if p in ("tonight", "this evening", "overnight", "rest of tonight"):
        return bound(base, 18), bound(base + timedelta(days=1), 6)
    night = p.endswith(" night")
    name = p[:-6].strip() if night else p
    if name in _WEEKDAYS:
        delta = (_WEEKDAYS.index(name) - local.weekday()) % 7
        target = base + timedelta(days=delta or 7)
        if night:
            return bound(target, 18), bound(target + timedelta(days=1), 6)
        return bound(target, 6), bound(target, 18)
    return None, None


class Database:
    """The one persistence gateway: owns the schema, a single locked pg8000
    connection, and every reader/writer the pipeline and API use.
    """
    def __init__(self, cfg: Config, database: str | None = None):
        self._cfg = cfg
        self._tz = ZoneInfo(cfg.station_tz)
        self._database = database or cfg.pg_database
        self._lock = threading.Lock()
        self._conn = self._connect()
        self._conn.run("SELECT pg_advisory_lock(CAST(:k AS bigint))", k=_SCHEMA_LOCK_KEY)
        try:
            for stmt in _SCHEMA:
                self._conn.run(stmt)
            for stmt in _MIGRATIONS:
                self._conn.run(stmt)
        finally:
            self._conn.run("SELECT pg_advisory_unlock(CAST(:k AS bigint))", k=_SCHEMA_LOCK_KEY)

    def _connect(self) -> pg8000.native.Connection:
        return pg8000.native.Connection(
            user=self._cfg.pg_user, host=self._cfg.pg_host, port=self._cfg.pg_port,
            database=self._database, password=self._cfg.pg_password or None,
        )

    def _run(self, sql: str, **params):
        with self._lock:
            try:
                return self._conn.run(sql, **params)
            except _CONN_ERRORS:  # pragma: no cover - reconnect on a dropped connection
                self._conn = self._connect()
                return self._conn.run(sql, **params)

    def _query(self, sql: str, **params) -> list[dict]:
        with self._lock:
            try:
                rows = self._conn.run(sql, **params)
                cols = [c["name"] for c in self._conn.columns]
            except _CONN_ERRORS:  # pragma: no cover - reconnect on a dropped connection
                self._conn = self._connect()
                rows = self._conn.run(sql, **params)
                cols = [c["name"] for c in self._conn.columns]
        return [dict(zip(cols, r)) for r in rows]

    def _count(self, table: str, where: str, params: dict) -> int:
        return self._query(f"SELECT COUNT(*) AS n FROM {table} {where}", **params)[0]["n"]

    @staticmethod
    def _time_range(where: str, params: dict, col: str, frm, to) -> tuple[str, dict]:
        """Append the shared inclusive [frm, to] range fragments to a WHERE."""
        if frm:
            where += f" AND {col} >= :frm"; params["frm"] = _parse_iso(frm)
        if to:
            where += f" AND {col} <= :to"; params["to"] = _parse_iso(to)
        return where, params

    # --- writers ---------------------------------------------------------- #
    def record_reading(self, reading: dict, captured_at: str) -> None:
        """Record one heard reading: bump the (city,condition) sightings counter and
        append a history row. Sightings let the read endpoints suppress one-off
        STT-garbage city names.
        """
        ca = _parse_iso(captured_at)
        cond = reading["condition"]
        num, txt = _split_value(reading["value"], cond in _NUMERIC_CONDITIONS)
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

    def write_forecast(self, periods: list[dict], issued_at: str, city: str | None = None) -> None:
        """Store one voted forecast issuance (a row per period), keyed by issued_at."""
        city = city or self._cfg.primary_city   # no hard-coded station city
        issued_dt = _parse_iso(issued_at)
        for p in periods:
            conf = p.get("confidence")
            self._run(
                "INSERT INTO forecasts"
                "(issued_at,city,period,high_f,low_f,precip_pct,sky,source,confidence) "
                "VALUES(:ia,:city,:pd,:hi,:lo,:pp,:sky,:src,CAST(:cf AS jsonb)) "
                "ON CONFLICT (issued_at,city,period) DO UPDATE SET "
                "high_f=EXCLUDED.high_f, low_f=EXCLUDED.low_f, "
                "precip_pct=EXCLUDED.precip_pct, sky=EXCLUDED.sky, source=EXCLUDED.source, "
                "confidence=EXCLUDED.confidence",
                ia=issued_dt, city=city, pd=p["period"],
                hi=p.get("high_f"), lo=p.get("low_f"), pp=p.get("precip_pct"),
                sky=p.get("sky"), src=p.get("source", "voice"),
                cf=json.dumps(conf) if conf else None,
            )

    def write_alert(self, record: dict) -> None:
        """Store a decoded SAME alert envelope; upserts by alert id."""
        a = record["alert"]
        captured = _parse_iso(record["captured_at"])
        expires = captured + timedelta(minutes=a.get("purge_minutes") or 0)  # tolerate None
        self._run(
            "INSERT INTO alerts(id,captured_at,event,areas,counties,"
            "purge_minutes,issued_raw,station,raw,expires_at) "
            "VALUES(:id,:ca,:ev,CAST(:ar AS jsonb),CAST(:co AS jsonb),:pm,:ir,:st,:rw,:ex) "
            "ON CONFLICT (id) DO UPDATE SET expires_at=EXCLUDED.expires_at",
            id=record["id"], ca=captured, ev=a.get("event"),
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
        append a history row. Mirrors record_reading without a city key.
        """
        ca = _parse_iso(captured_at)
        field = reading["field"]
        num, txt = _split_value(reading["value"], field in ALMANAC_NUMERIC)
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

    # --- raw transcript store (source of truth; see reprocess.py) --------- #
    def insert_raw_report(self, report: dict) -> None:
        """Land one raw report in the immutable transcript store, keyed by its id.
        Denormalizes the query columns out of the doc and keeps the whole doc in
        `payload`. Upserts so a term-fix / supersede rewrite of the same id updates
        in place (and a replayed backfill is idempotent).
        """
        self._run(
            "INSERT INTO raw_reports"
            "(id,captured_at,type,product_type,station,text,payload,fingerprint,supersedes) "
            "VALUES(:id,:ca,:ty,:pt,:st,:tx,CAST(:pl AS jsonb),:fp,:sup) "
            "ON CONFLICT (id) DO UPDATE SET "
            "captured_at=EXCLUDED.captured_at, type=EXCLUDED.type, "
            "product_type=EXCLUDED.product_type, station=EXCLUDED.station, "
            "text=EXCLUDED.text, payload=EXCLUDED.payload, "
            "fingerprint=EXCLUDED.fingerprint, supersedes=EXCLUDED.supersedes",
            id=report["id"], ca=_parse_iso(report["captured_at"]),
            ty=report.get("type"), pt=report.get("product_type"),
            st=report.get("station"), tx=report.get("text"),
            pl=json.dumps(report, ensure_ascii=False),
            fp=report.get("fingerprint"), sup=report.get("supersedes"),
        )

    def write_heartbeat(self, station: str, payload: dict) -> None:
        """Publish the pipeline heartbeat (health.py flushes one per segment).
        One row per station, upserted in place — tiny and bounded.
        """
        self._run(
            "INSERT INTO pipeline_health (station, updated_at, payload) "
            "VALUES (:st, now(), CAST(:pl AS jsonb)) "
            "ON CONFLICT (station) DO UPDATE SET "
            "updated_at=EXCLUDED.updated_at, payload=EXCLUDED.payload",
            st=station, pl=json.dumps(payload),
        )

    def read_heartbeat(self) -> dict | None:
        """The newest published heartbeat, or None if no pipeline has written one.
        One pipeline instance per database is the invariant (multi-transmitter
        runs one DB per instance), so newest-row is the instance's heartbeat.
        """
        rows = self._run(
            "SELECT payload FROM pipeline_health ORDER BY updated_at DESC LIMIT 1")
        return _as_obj(rows[0][0]) if rows else None

    def _raw_where(self, frm, to, q, product) -> tuple[str, dict]:
        where, params = self._time_range("WHERE 1=1", {}, "captured_at", frm, to)
        if product:
            where += " AND product_type = :product"; params["product"] = product
        if q:
            where += " AND text ILIKE :q"; params["q"] = f"%{q}%"
        return where, params

    def count_raw_reports(self, frm=None, to=None, q=None, product=None) -> int:
        """Row count matching the /transcripts filters (pagination totals)."""
        where, params = self._raw_where(frm, to, q, product)
        return self._count("raw_reports", where, params)

    def query_raw_reports(self, limit: int = 100, frm=None, to=None, q=None,
                          product=None, offset: int = 0) -> list[dict]:
        """Raw report docs matching the filters, most-recent-first (the /transcripts
        feed). Ties on captured_at break on id so paging is stable.
        """
        where, params = self._raw_where(frm, to, q, product)
        rows = self._query(
            f"SELECT payload FROM raw_reports {where} "
            f"ORDER BY captured_at DESC, id DESC LIMIT :lim OFFSET :off",
            lim=max(1, limit), off=max(0, offset), **params)
        return [_as_obj(r["payload"]) for r in rows]

    def raw_reports_since(self, since: str, limit: int, offset: int = 0) -> list[dict]:
        """Raw report docs with captured_at strictly after `since`, OLDEST-first —
        the watermark order /export pages losslessly.
        """
        rows = self._query(
            "SELECT payload FROM raw_reports WHERE captured_at > :s "
            "ORDER BY captured_at, id LIMIT :lim OFFSET :off",
            s=_parse_iso(since), lim=max(1, limit), off=max(0, offset))
        return [_as_obj(r["payload"]) for r in rows]

    def last_product_airing(self, product_type: str) -> str | None:
        """captured_at of the newest raw report of this product type — the last
        time the broadcast aired it, counting unchanged repeats that write no
        new structured row.
        """
        rows = self._query(
            "SELECT MAX(captured_at) AS at FROM raw_reports "
            "WHERE product_type = :p", p=product_type)
        return _ts(rows[0]["at"]) if rows[0]["at"] else None

    def recent_raw_reports(self, n: int) -> list[dict]:
        """The last n raw docs, returned OLDEST-first — for priming text-dedup on
        restart.
        """
        rows = self._query(
            "SELECT payload FROM (SELECT payload, captured_at, id FROM raw_reports "
            "ORDER BY captured_at DESC, id DESC LIMIT :n) t ORDER BY captured_at, id",
            n=max(1, n))
        return [_as_obj(r["payload"]) for r in rows]

    def iter_raw_reports(self) -> list[dict]:
        """Every raw doc in capture (vote) order — the full replay feed reprocess
        projects the structured tables from.
        """
        rows = self._query("SELECT payload FROM raw_reports ORDER BY captured_at, id")
        return [_as_obj(r["payload"]) for r in rows]

    def latest_almanac(self, min_sightings: int = 1) -> list[dict]:
        """Latest voted value for every almanac field (sightings-gated)."""
        rows = self._query(
            "SELECT field,value_num,value_text,votes,total,sightings,last_seen "
            "FROM almanac WHERE sightings >= :m ORDER BY field", m=min_sightings)
        return [_reading(r, "field", ts="last_seen") for r in rows]

    def latest_almanac_readings(self) -> list[dict]:
        """field -> value for priming the aggregator on restart."""
        rows = self._query("SELECT field, value_num, value_text FROM almanac")
        return [{"field": r["field"], "value": _value(r)} for r in rows]

    def almanac_since(self, since: str, limit: int, offset: int = 0) -> list[dict]:
        """Almanac observations captured after `since`, oldest first (sync feed)."""
        rows = self._query(
            "SELECT captured_at,field,value_num,value_text,votes,total "
            "FROM almanac_observations WHERE captured_at > :s "
            "ORDER BY captured_at LIMIT :lim OFFSET :off",
            s=_parse_iso(since), lim=limit, off=offset)
        return [_reading(r, "field") for r in rows]

    def alert_details_between(self, frm: str, to: str) -> list[dict]:
        """Structured spoken-alert details captured in [frm, to] (newest first)."""
        rows = self._query(
            "SELECT report_id,captured_at,product_type,until_text,motion,threats,"
            "locations,spotter_activation,text FROM alert_details "
            "WHERE captured_at >= :frm AND captured_at <= :to "
            "ORDER BY captured_at DESC", frm=_parse_iso(frm), to=_parse_iso(to),
        )
        return [self._hydrate_alert_detail(r) for r in rows]

    @staticmethod
    def _hydrate_alert_detail(r: dict) -> dict:
        """Decode one alert_details row (JSONB fields, timestamp, until rename)."""
        return {"report_id": r["report_id"], "captured_at": _ts(r["captured_at"]),
                "product_type": r["product_type"], "until": r["until_text"],
                "motion": _as_obj(r["motion"]), "threats": _as_obj(r["threats"]),
                "locations": _as_obj(r["locations"]),
                "spotter_activation": r["spotter_activation"], "text": r["text"]}

    # --- readers: conditions --------------------------------------------- #
    def list_conditions(self, min_sightings: int = 1) -> list[dict]:
        """Distinct condition names with data (the /conditions index)."""
        rows = self._query(
            "SELECT condition, COUNT(*) AS cities, MAX(last_seen) AS latest "
            "FROM city_conditions WHERE sightings >= :m GROUP BY condition ORDER BY condition",
            m=min_sightings,
        )
        return [{"condition": r["condition"], "cities": r["cities"], "latest": _ts(r["latest"])}
                for r in rows]

    def latest_for_condition(self, condition: str, min_sightings: int = 1) -> list[dict]:
        """Every city's latest voted value for one condition (sightings-gated)."""
        rows = self._query(
            "SELECT city, value_num, value_text, last_seen, votes, total, sightings "
            "FROM city_conditions WHERE condition=:c AND sightings >= :m "
            "ORDER BY city", c=condition, m=min_sightings,
        )
        return [_reading(r, "city", ts="last_seen") for r in rows]

    def _obs_where(self, condition: str, city: str | None,
                   frm: str | None, to: str | None) -> tuple[str, dict]:
        where, params = self._time_range(
            "WHERE condition=:c", {"c": condition}, "captured_at", frm, to)
        if city:
            where += " AND city=:city"; params["city"] = city
        return where, params

    def condition_history_count(self, condition: str, city: str | None,
                                frm: str | None, to: str | None) -> int:
        """Row count matching a conditions-history query (pagination totals)."""
        where, params = self._obs_where(condition, city, frm, to)
        return self._count("city_observations", where, params)

    def condition_history(self, condition: str, city: str | None,
                          frm: str | None, to: str | None,
                          limit: int = 1000, offset: int = 0) -> list[dict]:
        """Historical readings for a condition (optionally one city), newest first."""
        where, params = self._obs_where(condition, city, frm, to)
        rows = self._query(
            f"SELECT city,condition,value_num,value_text,captured_at,votes,total "
            f"FROM city_observations {where} ORDER BY captured_at DESC LIMIT :lim OFFSET :off",
            lim=limit, off=offset, **params)
        return [_reading(r, "city", "condition") for r in rows]

    # --- readers: forecast ----------------------------------------------- #
    def _with_window(self, row: dict, issued: datetime) -> dict:
        """Attach the derived (valid_from, valid_to) period window — computed from
        (period, issued), never stored (see the forecasts schema note).
        """
        vf, vt = period_window(row["period"], issued.astimezone(timezone.utc),
                               self._tz)
        row["valid_from"], row["valid_to"] = vf, vt
        return row

    def _hydrate_forecast(self, r: dict) -> dict:
        """Window + decoded issued_at/confidence on a full forecasts row."""
        self._with_window(r, r["issued_at"])
        r["issued_at"] = _ts(r["issued_at"])
        r["confidence"] = _as_obj(r["confidence"])
        return r

    def latest_forecasts(self) -> list[dict]:
        """Each city's most recent issuance with its period rows (one query, not
        one per city — the correlated subquery picks the max issued_at per city).
        """
        rows = self._query(
            "SELECT issued_at,city,period,high_f,low_f,precip_pct,sky,source,confidence "
            "FROM forecasts f WHERE issued_at="
            "(SELECT MAX(issued_at) FROM forecasts WHERE city=f.city) "
            "ORDER BY city, period")
        by_city: dict[str, dict] = {}
        for r in rows:
            issued, city = r.pop("issued_at"), r.pop("city")
            self._with_window(r, issued)
            r["confidence"] = _as_obj(r["confidence"])
            by_city.setdefault(
                city, {"city": city, "issued_at": _ts(issued), "periods": []}
            )["periods"].append(r)
        for entry in by_city.values():
            entry["periods"].sort(key=lambda r: (r["valid_from"] is None, str(r["valid_from"])))
        return list(by_city.values())

    def _fc_where(self, frm: str | None, to: str | None, city: str | None) -> tuple[str, dict]:
        where, params = self._time_range("WHERE 1=1", {}, "issued_at", frm, to)
        if city:
            where += " AND city=:city"; params["city"] = city
        return where, params

    def forecast_history_count(self, frm: str | None, to: str | None, city: str | None) -> int:
        """Issuance-row count matching a forecast-history query."""
        where, params = self._fc_where(frm, to, city)
        return self._count("forecasts", where, params)

    def forecast_history(self, frm: str | None, to: str | None, city: str | None,
                         limit: int = 1000, offset: int = 0) -> list[dict]:
        """Historical forecast rows with derived valid windows, newest first."""
        where, params = self._fc_where(frm, to, city)
        rows = self._query(
            f"SELECT issued_at,city,period,high_f,low_f,precip_pct,sky,confidence "
            f"FROM forecasts {where} ORDER BY issued_at DESC, city, period LIMIT :lim OFFSET :off",
            lim=limit, off=offset, **params)
        return [self._hydrate_forecast(r) for r in rows]

    # --- readers: alerts -------------------------------------------------- #
    @staticmethod
    def _hydrate_alert(d: dict) -> dict:
        """Decode the JSONB area/county lists, stamp timestamps, and resolve the
        derived event_label from `event` (the label is looked up, never stored).
        """
        d["event_label"] = event_label(d["event"])
        d["areas"] = _as_obj(d["areas"]) or []
        d["counties"] = _as_obj(d["counties"]) or []
        for k in ("captured_at", "expires_at"):
            d[k] = _ts(d[k])
        return d

    def get_active_alerts(self, now: str | None = None) -> list[dict]:
        """SAME alerts whose expires_at is still in the future."""
        now_dt = _parse_iso(now) or datetime.now(timezone.utc)
        rows = self._query(
            "SELECT * FROM alerts WHERE expires_at > :now ORDER BY captured_at DESC", now=now_dt,
        )
        return [self._hydrate_alert(d) for d in rows]

    def _alert_where(self, frm: str | None, to: str | None,
                     event: str | None) -> tuple[str, dict]:
        where, params = self._time_range("WHERE 1=1", {}, "captured_at", frm, to)
        if event:
            where += " AND event = :event"; params["event"] = event
        return where, params

    def alerts_history_count(self, frm: str | None, to: str | None,
                             event: str | None) -> int:
        """Alert count matching the history filters (pagination totals)."""
        where, params = self._alert_where(frm, to, event)
        return self._count("alerts", where, params)

    def alerts_history(self, frm: str | None, to: str | None, event: str | None,
                       limit: int = 1000, offset: int = 0) -> list[dict]:
        """All SAME alerts (active and expired), newest first, paginated."""
        where, params = self._alert_where(frm, to, event)
        rows = self._query(
            f"SELECT * FROM alerts {where} ORDER BY captured_at DESC LIMIT :lim OFFSET :off",
            lim=limit, off=offset, **params)
        return [self._hydrate_alert(d) for d in rows]

    # --- readers: per-city / index --------------------------------------- #
    def all_conditions_for_city(self, city: str, min_sightings: int = 1) -> list[dict]:
        """Every current condition for one city (the full local observation)."""
        rows = self._query(
            "SELECT condition, value_num, value_text, last_seen, votes, total, sightings "
            "FROM city_conditions WHERE LOWER(city)=LOWER(:c) AND sightings >= :m "
            "ORDER BY condition",
            c=city, m=min_sightings)
        return [_reading(r, "condition", ts="last_seen") for r in rows]

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
        """City observations captured after `since`, oldest first (sync feed)."""
        rows = self._query(
            "SELECT city,condition,value_num,value_text,captured_at,votes,total "
            "FROM city_observations WHERE captured_at > :s "
            "ORDER BY captured_at LIMIT :lim OFFSET :off",
            s=_parse_iso(since), lim=limit, off=offset)
        return [_reading(r, "city", "condition") for r in rows]

    def forecasts_since(self, since: str, limit: int, offset: int = 0) -> list[dict]:
        """Forecast rows issued after `since`, oldest first (sync feed)."""
        rows = self._query(
            "SELECT issued_at,city,period,high_f,low_f,precip_pct,sky,confidence "
            "FROM forecasts WHERE issued_at > :s ORDER BY issued_at LIMIT :lim OFFSET :off",
            s=_parse_iso(since), lim=limit, off=offset)
        return [self._hydrate_forecast(r) for r in rows]

    def alerts_since(self, since: str, limit: int, offset: int = 0) -> list[dict]:
        """SAME alerts captured after `since`, oldest first (sync feed)."""
        rows = self._query(
            "SELECT * FROM alerts WHERE captured_at > :s "
            "ORDER BY captured_at LIMIT :lim OFFSET :off",
            s=_parse_iso(since), lim=limit, off=offset)
        return [self._hydrate_alert(d) for d in rows]

    def alert_details_since(self, since: str, limit: int, offset: int = 0) -> list[dict]:
        """Spoken-alert details captured after `since`, oldest first (sync feed)."""
        rows = self._query(
            "SELECT report_id,captured_at,product_type,until_text,motion,threats,"
            "locations,spotter_activation,text FROM alert_details WHERE captured_at > :s "
            "ORDER BY captured_at LIMIT :lim OFFSET :off",
            s=_parse_iso(since), lim=limit, off=offset)
        return [self._hydrate_alert_detail(r) for r in rows]

    def latest_readings(self) -> list[dict]:
        """All latest (city, condition) readings — used to prime the aggregator."""
        rows = self._query("SELECT city, condition, value_num, value_text FROM city_conditions")
        return [{"city": r["city"], "condition": r["condition"], "value": _value(r)} for r in rows]

    def clear(self) -> None:
        """Wipe the STRUCTURED tables only — never raw_reports, so a reprocess
        can always rebuild what this removed.
        """
        self._run("TRUNCATE city_observations, city_conditions, forecasts, alerts, "
                  "alert_details, almanac, almanac_observations, pipeline_health")

    def close(self) -> None:
        """Close the underlying connection (idempotent)."""
        with self._lock:
            self._conn.close()
