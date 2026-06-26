"""Pipeline liveness heartbeat (roadmap: fail-loud health + watchdog).

The capture/STT pipeline runs in the `wxparser` process; the query API runs in a
separate `wxparser-api` process and can't see its in-memory state. So the
producer/worker update a `Heartbeat` that is flushed to `out_dir/health.json`,
and the API reads that file in `/health` and derives ok / degraded / down from
the freshness of the signals — so a monitor can alarm when the box goes deaf or
the STT worker wedges, instead of the failure being silent.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone

from .config import Config


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _age_min(ts, now: datetime) -> float | None:
    if not ts:
        return None
    then = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (now - then).total_seconds() / 60.0


class Heartbeat:
    """Thread-safe pipeline-liveness state, flushed atomically to health.json."""

    def __init__(self, cfg: Config):
        self._path = cfg.out_dir / "health.json"
        self._lock = threading.Lock()
        self._d: dict = {
            "started_at": _now(),
            "last_segment_at": None,    # audio alive — a segment was produced
            "last_novel_at": None,      # novel content reached the STT queue
            "last_stt_ok_at": None,     # a transcription succeeded
            "last_extraction_at": None, # a reading/forecast was written
            "segments": 0, "novel": 0, "repeat": 0,
            "stt_errors": 0, "capture_restarts": 0,
            "queue_depth": 0,
        }

    def set(self, **kw) -> None:
        with self._lock:
            self._d.update(kw)

    def touch(self, key: str) -> None:
        with self._lock:
            self._d[key] = _now()

    def incr(self, key: str, n: int = 1) -> None:
        with self._lock:
            self._d[key] = self._d.get(key, 0) + n

    def flush(self) -> None:
        with self._lock:
            data = dict(self._d, updated_at=_now())
        try:
            tmp = self._path.with_name(self._path.name + ".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError:  # pragma: no cover - defensive: health must never crash capture
            pass

    @staticmethod
    def read(cfg: Config) -> dict | None:
        try:
            return json.loads((cfg.out_dir / "health.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None


def assess(hb: dict | None, cfg: Config, now: datetime | None = None) -> dict:
    """Derive a fail-loud status from the heartbeat.

    down     — no/stale heartbeat: the capture process isn't flushing (likely dead).
    degraded — heartbeat fresh but audio silent (deaf radio) or STT worker wedged.
    ok       — segments flowing and the worker draining.
    """
    now = now or datetime.now(timezone.utc)
    if hb is None:
        return {"status": "down", "checks": ["no heartbeat file — pipeline not running?"]}

    hb_age = _age_min(hb.get("updated_at"), now)
    audio_age = _age_min(hb.get("last_segment_at"), now)
    stt_age = _age_min(hb.get("last_stt_ok_at"), now)
    checks: list[str] = []
    status = "ok"

    if hb_age is None or hb_age > cfg.health_heartbeat_stale_min:
        status = "down"
        checks.append(f"heartbeat stale ({_fmt(hb_age)}m > {cfg.health_heartbeat_stale_min}m)")
    if audio_age is None or audio_age > cfg.health_audio_silent_min:
        status = "degraded" if status == "ok" else status
        checks.append(f"audio silent ({_fmt(audio_age)}m) — possible deaf radio")
    # worker wedged: a backlog is queued but nothing has transcribed for a while.
    # Before the first success, measure from process start so a just-booted worker
    # (whose first STT is still running) isn't flagged.
    stt_ref_age = stt_age if stt_age is not None else _age_min(hb.get("started_at"), now)
    if hb.get("queue_depth", 0) > 0 and (stt_ref_age is None
                                         or stt_ref_age > cfg.health_audio_silent_min):
        status = "degraded" if status == "ok" else status
        checks.append(f"STT worker may be wedged (q={hb.get('queue_depth')}, "
                      f"last ok {_fmt(stt_age)}m ago)")

    return {"status": status, "checks": checks or ["all signals nominal"],
            "heartbeat_age_min": _round(hb_age), "audio_silent_min": _round(audio_age),
            "last_stt_ok_min": _round(stt_age), "pipeline": hb}


def _fmt(v) -> str:
    return "never" if v is None else f"{v:.1f}"


def _round(v):
    return None if v is None else round(v, 1)
