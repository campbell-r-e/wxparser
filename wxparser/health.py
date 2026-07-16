"""Pipeline liveness heartbeat backing the fail-loud /health endpoint.

The capture/STT pipeline runs in the `wxparser` process; the query API runs in a
separate `wxparser-api` process and can't see its in-memory state. So the
producer/worker update a `Heartbeat` that is flushed on every segment, and the
API derives ok / degraded / down from the freshness of the signals — so a
monitor can alarm when the box goes deaf or the STT worker wedges, instead of
the failure being silent.

The flush is write-through to two places. The `pipeline_health` DB row is the
transport the API reads — it works when the API runs on a different machine
than the capture box. `out_dir/health.json` is kept for same-machine consumers
(the AGC timer reads the segment levels from it) and as the API's fallback when
no DB heartbeat exists. A DB hiccup must never take capture down, so the DB leg
swallows its own failures: the file keeps flushing, the DB row goes stale, and
/health reports `down` on staleness — which is the truth a monitor should see.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone

from .config import Config
from .timefmt import parse_iso_utc, utc_now_iso as _now


def _age_min(ts, now: datetime) -> float | None:
    if not ts:
        return None
    try:
        then = parse_iso_utc(ts)
    except ValueError:  # a corrupt timestamp in health.json must not crash /health
        return None
    return (now - then).total_seconds() / 60.0


class Heartbeat:
    """Thread-safe pipeline-liveness state, flushed to the DB and health.json."""

    def __init__(self, cfg: Config, db=None):
        self._path = cfg.out_dir / "health.json"
        self._station = cfg.station
        self._db = db  # anything with write_heartbeat(station, payload); None = file only
        self._lock = threading.Lock()
        self._d: dict = {
            "started_at": _now(),
            "last_segment_at": None,    # audio alive — a segment was produced
            "last_novel_at": None,      # novel content reached the STT queue
            "last_stt_ok_at": None,     # a transcription succeeded
            "last_extraction_at": None,  # a reading/forecast was written
            "segments": 0, "novel": 0, "repeat": 0,
            "stt_errors": 0, "capture_restarts": 0,
            "queue_depth": 0,
            "last_segment_dbfs": None,       # speech RMS level of the last segment
            "last_segment_peak_dbfs": None,  # peak level (clipping headroom) — AGC
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
        if self._db is not None:
            try:
                self._db.write_heartbeat(self._station, data)
            except Exception:
                # a DB outage must not crash capture; staleness of the DB row is
                # itself the down signal /health reports
                pass

    @staticmethod
    def read(cfg: Config) -> dict | None:
        """The health.json fallback — same-machine deployments and the AGC."""
        try:
            return json.loads((cfg.out_dir / "health.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None


def assess(hb: dict | None, cfg: Config, now: datetime | None = None,
           reading_at: str | None = None) -> dict:
    """Derive a fail-loud status from the heartbeat and the freshness of the data.

    down     — no/stale heartbeat: the capture process isn't flushing (likely dead).
    degraded — heartbeat fresh but audio silent (deaf radio), nothing novel in a
               long time (static/dead carrier), STT worker wedged, or conditions
               no longer reaching the store.
    ok       — segments flowing, novel content arriving, worker draining, and the
               store still being written.

    `reading_at` is when the station's own temperature last landed in the store —
    the ob's canary, since it airs every cycle. Every other signal here describes
    the *plumbing*; this one describes the *product*, and the plumbing can be
    immaculate while the product rots (see the check below).
    """
    now = now or datetime.now(timezone.utc)
    if hb is None:
        return {"status": "down", "checks": ["no heartbeat — pipeline not running?"]}

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
    # dead-but-not-silent radio: constant static/carrier passes the VAD gate, so
    # segments keep flowing but every one fingerprints as a repeat and nothing
    # novel reaches STT. The real broadcast always produces novel segments within
    # minutes (time announcements change each cycle), so a long novelty drought
    # means noise, not programming. Before the first novel segment, measure from
    # process start so a just-booted pipeline isn't flagged.
    novel_age = _age_min(hb.get("last_novel_at"), now)
    novel_ref_age = novel_age if novel_age is not None else _age_min(hb.get("started_at"), now)
    if novel_ref_age is None or novel_ref_age > cfg.health_novel_stale_min:
        status = "degraded" if status == "ok" else status
        checks.append(f"no novel speech ({_fmt(novel_age)}m > "
                      f"{cfg.health_novel_stale_min}m) — static or dead carrier?")
    # worker wedged: a REAL backlog is stuck, not just one segment that landed
    # after an idle stretch. On a looping broadcast the novelty gate idles STT for
    # many minutes, then a single novel segment queues while last_stt_ok is still
    # old — that's idle-then-busy and drains within a cycle, so require a queue
    # above the wedged floor AND a dedicated (looser) staleness window. Before the
    # first success, measure from process start so a just-booted worker (first STT
    # still running) isn't flagged.
    stt_ref_age = stt_age if stt_age is not None else _age_min(hb.get("started_at"), now)
    if hb.get("queue_depth", 0) > cfg.health_stt_wedged_queue and (
            stt_ref_age is None or stt_ref_age > cfg.health_stt_wedged_min):
        status = "degraded" if status == "ok" else status
        checks.append(f"STT worker may be wedged (q={hb.get('queue_depth')}, "
                      f"last ok {_fmt(stt_age)}m ago)")

    # Extraction flatlined: every signal above can read nominal while nothing
    # reaches the store, because they all watch the plumbing and none watch the
    # product. Seen 2026-07-16: /now served a 10.5h-old 75F while it was actually
    # 89.6F, and /health answered "all signals nominal" for three hours straight --
    # audio was flowing and STT was draining, but the novelty gate was dropping
    # every conditions re-read before STT, so nothing was ever extracted. The
    # station re-reads the ob roughly hourly, so a multi-hour gap is broken, not
    # quiet. reading_at None means nothing is stored yet (fresh box) -- not a fault.
    # The caller passes the primary city's temperature specifically: aggregating
    # over all conditions masks this, because one rarely-aired field landing resets
    # the clock while the ob itself stays hours dead.
    reading_age = _age_min(reading_at, now)
    if reading_age is not None and reading_age > cfg.health_readings_stale_min:
        status = "degraded" if status == "ok" else status
        checks.append(f"conditions not updating ({_fmt(reading_age)}m > "
                      f"{cfg.health_readings_stale_min}m) — extraction flatlined?")

    return {"status": status, "checks": checks or ["all signals nominal"],
            "readings_stale_min": _round(reading_age),
            "heartbeat_age_min": _round(hb_age), "audio_silent_min": _round(audio_age),
            "last_stt_ok_min": _round(stt_age), "last_novel_min": _round(novel_age),
            "pipeline": hb}


def _fmt(v) -> str:
    return "never" if v is None else f"{v:.1f}"


def _round(v):
    return None if v is None else round(v, 1)
