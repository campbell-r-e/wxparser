"""Report construction and persistence.

Each saved report is a self-contained JSON object (PLAN §5.1) appended to
`transcripts/reports.jsonl` — one object per line, trivial to tail or ingest.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections import deque
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .stt import Transcript

SCHEMA_VERSION = 1

# Reports and SAME alerts are appended from different threads; serialize writes.
_append_lock = threading.Lock()

import re

from .extract import (
    _RE_PERIOD_HDR,
    extract_forecast_fields,
    extract_observation,
)

# Explicit, authoritative product names (a literal warning/statement title beats
# any structural guess). The routine loop products — forecast and conditions —
# are typed below from the same structured extraction the rest of the pipeline
# runs, because VAD chops them into mid-narrative fragments that seldom contain a
# product name (which left ~73% of reports "unknown").
_PRODUCT_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("tornado_warning", ("tornado warning",)),
    ("severe_thunderstorm_warning", ("severe thunderstorm warning",)),
    ("flash_flood_warning", ("flash flood warning",)),
    ("special_weather_statement", ("special weather statement",)),
    ("hazardous_weather_outlook", ("hazardous weather outlook",)),
]
# Forecast-narrative phrasing that carries no extractable number but is
# unmistakably the zone forecast (the connective tissue between the labelled
# highs/lows lines). Conditions never use these.
_RE_FORECASTY = re.compile(
    r"\bchance of (?:showers|thunderstorms?|rain|snow|flurries|precipitation)\b"
    r"|\b(?:north|south|east|west|northeast|northwest|southeast|southwest)(?:erly)?\s+winds?\b"
    r"|\bwinds?\s+(?:around|near|becoming|light|calm|up to|\d)"
    r"|\bbecoming\s+(?:mostly |partly )?(?:cloudy|clear|sunny|fair|windy)"
    r"|\b(?:toward|through the late)\s+(?:daybreak|overnight|morning|afternoon|evening)"
    r"|\bshowers likely\b|\bslight chance\b|\bchance of a\b"
    r"|\bof (?:showers|thunderstorms?)\b|\bthunderstorms? (?:likely|possible)\b"
    # a forecast temp band "in the lower/mid/upper <decade>" — catches lines
    # whose high/low label STT garbled ("Close/Pines in the mid-eighties").
    r"|\bin the (?:lower|mid|middle|upper)[\s-]+(?:\d{1,3}s?|\w+ies)\b"
    r"|\bforecast for the\b|\bextended forecast\b", re.I)
# Conditions-only fields. "sky" here comes from the observation-framed regex
# ("it was clear"), which a forecast ("Tonight, clear") never uses, so it's a
# safe conditions signal. A forecast also never reports pressure / humidity /
# dewpoint / "the wind was <dir> at N".
_COND_FIELDS = ("pressure_in", "temperature_f", "dewpoint_f", "humidity_pct", "wind", "sky")
# The nearby-city temperature roundup ("... 74 at Marion, 76 at Anderson ...") is
# part of the current-conditions product; a forecast never lists "<temp> at City".
_RE_ROUNDUP = re.compile(r"\b-?\d{2,3}\s+(?:degrees?\s+)?at\s+[A-Z][a-z]+", re.I)


# product_types that carry an alert narrative worth structuring + linking to a
# SAME header (see extract.extract_alert_details / db.alert_details).
ALERT_PRODUCTS = frozenset({
    "tornado_warning", "severe_thunderstorm_warning", "flash_flood_warning",
    "special_weather_statement", "hazardous_weather_outlook",
})


def classify(text: str) -> str:
    low = text.lower()
    # 1) explicit product name wins (warnings / statements / outlook).
    for product_type, keywords in _PRODUCT_KEYWORDS:
        if any(k in low for k in keywords):
            return product_type
    # 2) conditions: any conditions-only observation field present.
    obs = extract_observation(text)
    if any(obs.get(k) is not None for k in _COND_FIELDS) or _RE_ROUNDUP.search(text):
        return "current_conditions"
    # 3) forecast: a period header, an extracted high/low/precip, or the
    #    unmistakable forecast-narrative phrasing between the labelled lines.
    if _RE_PERIOD_HDR.search(text) is not None:
        return "zone_forecast"
    fc = extract_forecast_fields(text)
    if any(fc.get(k) is not None for k in ("high_f", "low_f", "precip_pct", "steady_f")):
        return "zone_forecast"
    if _RE_FORECASTY.search(text):
        return "zone_forecast"
    return "unknown"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_report(
    transcript: Transcript,
    cfg: Config,
    duration_s: float,
    fingerprint: str = "",
    supersedes: str | None = None,
    captured_at: str | None = None,
) -> dict:
    captured_at = captured_at or _utc_now_iso()
    short = hashlib.sha1(transcript.text.encode("utf-8")).hexdigest()[:6]
    return {
        "schema_version": SCHEMA_VERSION,
        "id": f"{captured_at}-{short}",
        "station": cfg.station,
        "frequency_mhz": cfg.frequency_mhz,
        "captured_at": captured_at,
        "duration_s": round(duration_s, 1),
        "product_type": classify(transcript.text),
        "text": transcript.text,
        "segments": [asdict(s) for s in transcript.segments],
        "stt": {
            "engine": cfg.whisper_engine_name,
            "model": cfg.model_name,
            "avg_confidence": 0.0,  # whisper.cpp does not expose this yet
        },
        "fingerprint": fingerprint,
        "supersedes": supersedes,
    }


def append_report(report: dict, cfg: Config) -> Path:
    cfg.out_dir.mkdir(parents=True, exist_ok=True)
    path = cfg.reports_jsonl
    with _append_lock:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(report, ensure_ascii=False) + "\n")
    return path


def build_alert(alert: dict, cfg: Config, captured_at: str | None = None) -> dict:
    """Wrap a decoded SAME alert (same.SAMEMessage.to_record()) in an envelope."""
    captured_at = captured_at or _utc_now_iso()
    short = hashlib.sha1(alert.get("raw", "").encode("utf-8")).hexdigest()[:6]
    return {
        "schema_version": SCHEMA_VERSION,
        "type": "same_alert",
        "id": f"{captured_at}-{alert.get('event', 'XXX')}-{short}",
        "station": cfg.station,
        "frequency_mhz": cfg.frequency_mhz,
        "captured_at": captured_at,
        "alert": alert,
    }


def build_observation(fields: dict, cfg: Config, captured_at: str | None = None) -> dict:
    """Wrap a voted current-conditions snapshot (extract.ConditionsAggregator)."""
    captured_at = captured_at or _utc_now_iso()
    key = "|".join(f"{k}={v.get('value')}" for k, v in sorted(fields.items()))
    short = hashlib.sha1(key.encode("utf-8")).hexdigest()[:6]
    return {
        "schema_version": SCHEMA_VERSION,
        "type": "observation",
        "id": f"{captured_at}-obs-{short}",
        "station": cfg.station,
        "frequency_mhz": cfg.frequency_mhz,
        "captured_at": captured_at,
        "fields": fields,
    }


def load_recent_reports(cfg: Config, n: int) -> list[dict]:
    """Return the last n saved reports (oldest-first) for dedup priming."""
    path = cfg.reports_jsonl
    if not path.exists():
        return []
    tail: deque[str] = deque(maxlen=n)
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                tail.append(line)
    out: list[dict] = []
    for line in tail:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def query_reports(
    cfg: Config,
    limit: int = 100,
    frm: str | None = None,
    to: str | None = None,
    q: str | None = None,
    product: str | None = None,
    max_scan: int = 20000,
) -> list[dict]:
    """Raw transcript records from reports.jsonl, most-recent-first.

    Scans at most the last `max_scan` lines (the file is append-only and may grow
    unbounded), then filters. `frm`/`to` are inclusive ISO-8601 strings compared
    lexically — safe because every captured_at uses the same fixed Z format.
    `q` is a case-insensitive substring match on the transcript text; `product`
    matches product_type exactly.
    """
    path = cfg.reports_jsonl
    if not path.exists():
        return []
    tail: deque[str] = deque(maxlen=max_scan)
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                tail.append(line)
    ql = q.lower() if q else None
    out: list[dict] = []
    for line in tail:
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        ca = rec.get("captured_at", "")
        if frm and ca < frm:
            continue
        if to and ca > to:
            continue
        if product and rec.get("product_type") != product:
            continue
        if ql and ql not in (rec.get("text") or "").lower():
            continue
        out.append(rec)
    out.reverse()  # newest first
    return out[: max(1, limit)]
