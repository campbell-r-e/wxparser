"""Report construction and persistence.

Each saved report is a self-contained JSON object (PLAN §5.1) appended to
`transcripts/reports.jsonl` — one object per line, trivial to tail or ingest.
"""

from __future__ import annotations

import hashlib
import json
from collections import deque
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .stt import Transcript

SCHEMA_VERSION = 1

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
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(report, ensure_ascii=False) + "\n")
    return path


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
