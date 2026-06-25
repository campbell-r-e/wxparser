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

# Best-effort product typing via cheap keyword matching (a hint, not authoritative;
# authoritative typing comes later from SAME decoding — PLAN §5.1, §8).
_PRODUCT_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("tornado_warning", ("tornado warning",)),
    ("severe_thunderstorm_warning", ("severe thunderstorm warning",)),
    ("flash_flood_warning", ("flash flood warning",)),
    ("special_weather_statement", ("special weather statement",)),
    ("hazardous_weather_outlook", ("hazardous weather outlook",)),
    ("zone_forecast", ("zone forecast", "tonight", "highs", "lows", "chance of rain")),
    ("current_conditions", ("current conditions", "at the", "relative humidity", "dewpoint")),
]


# product_types that carry an alert narrative worth structuring + linking to a
# SAME header (see extract.extract_alert_details / db.alert_details).
ALERT_PRODUCTS = frozenset({
    "tornado_warning", "severe_thunderstorm_warning", "flash_flood_warning",
    "special_weather_statement", "hazardous_weather_outlook",
})


def classify(text: str) -> str:
    low = text.lower()
    for product_type, keywords in _PRODUCT_KEYWORDS:
        if any(k in low for k in keywords):
            return product_type
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
