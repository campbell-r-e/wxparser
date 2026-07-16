"""wxparser entry point.

Phase 2 — novelty-gated pipeline (PLAN §5):

    [producer thread]  capture frames -> VAD segments -> fingerprint -> gate
                           repeat -> drop (no STT)
                           novel  -> enqueue
    [STT worker thread]    dequeue -> whisper.cpp -> append JSON report

Capture and STT run on separate threads so the slower-than-real-time transcriber
never stalls capture: novel segments queue up during a fresh product and drain
during the long stretches where the broadcast loop just repeats.

Usage:
  wxparser                 # live gated capture loop (Ctrl-C to stop)
  wxparser --once          # run until the first NOVEL segment is saved, then exit
  wxparser --file a.wav    # transcribe an existing WAV (no capture/gating) — tests
"""

from __future__ import annotations

import argparse
import itertools
import os
import queue
import signal
import sys
import threading
import time
import traceback
from collections.abc import Iterator
from pathlib import Path

from .capture import stream_frames
from .config import Config
from .db import Database
from .dedup import TextDeduper
from .extract import (
    AlmanacAggregator,
    CityConditionsAggregator,
    ForecastAggregator,
)
from .fingerprint import dump_vector, Fingerprinter, NoveltyGate
from .health import Heartbeat
from .notify import post_webhook
from .pipeline import PipelineState, apply_readings, write_alert_detail_if_any
from .same import SAMEMessage, SAMEMonitor
from .segment import segment_level_dbfs, segment_stream
from .store import build_alert, build_report
from .stt import Transcript, is_blank, transcribe, transcribe_samples
from .timefmt import utc_now_iso as _utc_now_iso

# STT queue priorities (lower = transcribed first). Spoken warning narratives
# captured just after a SAME burst jump ahead of routine forecast/conditions.
_PRIO_ALERT = 0
_PRIO_NORMAL = 10

_STOP = threading.Event()


def _handle_signal(signum, frame):  # pragma: no cover
    _STOP.set()


def _save(
    transcript: Transcript,
    cfg: Config,
    duration_s: float,
    fingerprint: str,
    deduper: TextDeduper | None = None,
    db: Database | None = None,
) -> dict | None:
    """Build, text-dedup, and land a report in the raw transcript store. Returns
    the saved report (or None if it was dropped as a duplicate).
    """
    report = build_report(transcript, cfg, duration_s=duration_s, fingerprint=fingerprint)
    tag = "NEW"
    if deduper is not None:
        res = deduper.consider(report)
        if res.kind == "duplicate":
            print(
                f"  . text-dup skip {duration_s:5.1f}s ({report['product_type']})",
                flush=True,
            )
            return None
        report["supersedes"] = res.supersedes
        tag = "UPD" if res.kind == "update" else "NEW"
    if db is not None:
        db.insert_raw_report(report)
    dest = f"pg:{cfg.pg_database}/raw_reports" if db is not None else "(not persisted)"
    print(
        f"[{report['captured_at']}] {tag}  {report['product_type']:<28} "
        f"{duration_s:5.1f}s {len(transcript.segments):>2} seg -> {dest}",
        flush=True,
    )
    if report.get("supersedes"):
        print(f"    (supersedes {report['supersedes']})", flush=True)
    print(f"    {transcript.text}", flush=True)
    return report


def _tee_to_same(
    frames: Iterator[tuple], monitor: SAMEMonitor | None
) -> Iterator[tuple]:
    """Pass frames through to the segmenter while also feeding the SAME monitor."""
    for frame, t in frames:
        if monitor is not None:
            monitor.feed(frame, t)
        yield frame, t


def _emit_alert(msg: SAMEMessage, cfg: Config, db: Database | None) -> None:
    record = build_alert(msg.to_record(), cfg)
    if db is not None:
        db.insert_raw_report(record)
        db.write_alert(record)
    # opt-in outbound push: tell a configured endpoint immediately (no-op if unset)
    post_webhook(cfg, "alert", {"id": record.get("id"),
                                "captured_at": record.get("captured_at"),
                                **record.get("alert", {})})
    areas = ", ".join(msg.counties) if msg.counties else ", ".join(msg.areas)
    print(
        f"[{record['captured_at']}] ALERT {msg.event_label} ({msg.event}) "
        f"— {areas} — {msg.purge_minutes}min — {msg.station.strip()}",
        flush=True,
    )


def run_file(wav_path: Path, cfg: Config) -> int:
    transcript = transcribe(wav_path, cfg)
    if is_blank(transcript, cfg):
        print("[blank audio — nothing to transcribe]", flush=True)
        return 0
    db = Database(cfg)
    try:
        _save(transcript, cfg, duration_s=cfg.window_seconds, fingerprint="", db=db)
    finally:
        db.close()
    return 0


def _die() -> None:
    """Hard-exit the whole process (not just the calling thread). A worker
    thread dying inside an otherwise-live process is invisible to systemd's
    Restart= — os._exit makes the failure a process death it can act on.
    """
    os._exit(1)


def _stt_worker(
    q: "queue.PriorityQueue",
    cfg: Config,
    once: bool,
    state: PipelineState,
) -> None:
    hb = state.hb
    while not _STOP.is_set():
        try:
            prio, _seq, payload = q.get(timeout=0.5)
        except queue.Empty:
            continue
        if payload is None:  # poison pill
            break
        seg, digest = payload
        if prio == _PRIO_ALERT:
            print(f"  >> PRIORITY (alert narrative) {seg.duration_s:5.1f}s", flush=True)
        try:
            transcript = transcribe_samples(seg.samples, cfg)
        except Exception as e:  # keep the service alive on a single bad segment
            print(f"  ! STT error: {e}", file=sys.stderr, flush=True)
            if hb is not None:
                hb.incr("stt_errors"); hb.set(queue_depth=q.qsize()); hb.flush()
            q.task_done()
            continue
        if hb is not None:
            hb.touch("last_stt_ok_at")
        if is_blank(transcript, cfg):
            print(f"  . novel-but-blank {seg.duration_s:5.1f}s", flush=True)
        else:
            try:
                saved = _handle_transcript(transcript, seg, digest, cfg, state)
            except Exception:
                # A persistent failure here (DB outage outliving the driver's
                # reconnect, a projection bug) would otherwise kill only THIS
                # thread: the process stays "running", systemd never restarts
                # it, and the producer queues audio unbounded while nothing is
                # stored. Fail loud instead — take the whole process down so
                # Restart=always brings the pipeline back (dedup + aggregators
                # re-prime from the store on boot).
                traceback.print_exc()
                print("  !! STT worker: unrecoverable error — exiting so systemd "
                      "restarts the pipeline", file=sys.stderr, flush=True)
                _STOP.set()
                _die()
                return  # unreachable live (_die never returns); keeps tests sane
            if once and saved is not None:
                _STOP.set()
        if hb is not None:
            hb.set(queue_depth=q.qsize()); hb.flush()
        q.task_done()


def _handle_transcript(
    transcript,
    seg,
    digest: str,
    cfg: Config,
    state: PipelineState,
) -> dict | None:
    """Vote, log, and store one non-blank transcript; returns the saved report
    (None when text-dedup dropped it as a repeat).
    """
    text = transcript.text
    now = _utc_now_iso()
    # vote BEFORE dedup so boundary-shifted repeats still contribute readings.
    # apply_readings is the SAME step reprocess replays over the stored
    # transcripts, so the DB stays a re-derivable projection of them.
    # Skip voting on low-confidence transcripts (still stored, just not
    # voted) so a mangled reading can't sway the aggregates.
    summary = apply_readings(text, now, state,
                             confidence=transcript.avg_confidence,
                             confidence_floor=cfg.stt_confidence_floor)
    if summary.get("low_confidence"):
        print(f"  . low-conf {transcript.avg_confidence:.2f} — stored, not voted",
              flush=True)
    for r in summary["readings"]:
        print(f"[{now}] OBS  {r['city']}: {r['condition']}={r['value']}", flush=True)
    for r in summary["almanac"]:
        print(f"[{now}] ALM  {r['field']}={r['value']}", flush=True)
    saved = _save(transcript, cfg, seg.duration_s, digest, state.deduper, state.db)
    if saved is not None and state.db is not None:
        details = write_alert_detail_if_any(
            text, now, saved["id"], saved["product_type"], state.db)
        if details:
            print(f"[{now}] ALERT-DETAIL {saved['product_type']}: {details}", flush=True)
    return saved


def run_live(cfg: Config, once: bool = False) -> int:
    _STOP.clear()  # reset the module-global stop flag so a re-run isn't a no-op
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    print(
        f"wxparser: gated capture of {cfg.station} ({cfg.frequency_mhz} MHz) from "
        f"{cfg.alsa_device}; model={cfg.model_name} "
        f"(VAD>{cfg.vad_threshold_dbfs:.0f}dBFS, sim>{cfg.fp_similarity_threshold})",
        flush=True,
    )
    fp = Fingerprinter(cfg)
    gate = NoveltyGate(cfg)
    deduper = TextDeduper(cfg)
    aggregator = CityConditionsAggregator(primary_city=cfg.primary_city,
                                          stale_sec=cfg.vote_stale_min * 60,
                                          peer_min=cfg.peer_min_cities,
                                          peer_max_dev=cfg.peer_max_dev_f)
    forecast = ForecastAggregator()
    almanac = AlmanacAggregator()
    db = Database(cfg)
    print(f"  (postgres store: {cfg.pg_user}@{cfg.pg_host}:{cfg.pg_port}/{cfg.pg_database})",
          flush=True)
    # prime text-dedup from the raw transcript store so a restart keeps state
    recent = db.recent_raw_reports(cfg.text_history)
    deduper.prime(recent)
    if recent:
        print(f"  (primed text-dedup with {len(recent)} recent reports)", flush=True)
    # prime aggregators from the store so a restart keeps state
    readings = db.latest_readings()
    if readings:
        aggregator.prime(readings)
    fcs = db.latest_forecasts()
    for fc in fcs:
        if fc["city"] == forecast.city:
            forecast.prime(fc["periods"])
            break
    alm = db.latest_almanac_readings()
    if alm:
        almanac.prime(alm)
    if readings or fcs or alm:
        print(f"  (primed: {len(readings)} city readings, {len(fcs)} forecast areas, "
              f"{len(alm)} almanac fields)", flush=True)
    hb = Heartbeat(cfg, db)
    hb.flush()  # publish "starting" immediately so /health isn't down on boot
    state = PipelineState(aggregator=aggregator, forecast=forecast, almanac=almanac,
                          deduper=deduper, db=db, hb=hb)
    q: "queue.PriorityQueue" = queue.PriorityQueue()
    seq = itertools.count()  # tie-breaker so PriorityQueue never compares payloads
    worker = threading.Thread(target=_stt_worker, args=(q, cfg, once, state), daemon=True)
    worker.start()

    # SAME decode runs here on the producer thread; when a burst fires, open a
    # priority window so the spoken narrative that follows is transcribed first.
    alert_until = [0.0]

    def _on_alert(m: SAMEMessage) -> None:  # pragma: no cover - fired only by a live SAME burst
        _emit_alert(m, cfg, db)
        alert_until[0] = time.monotonic() + cfg.alert_priority_window_s
        print(f"  >> alert priority window open ({cfg.alert_priority_window_s:.0f}s)", flush=True)

    monitor = SAMEMonitor(cfg, _on_alert) if cfg.same_enabled else None
    if monitor is not None:
        print("  (SAME alert decoding enabled)", flush=True)

    frames = stream_frames(cfg, on_retry=lambda: hb.incr("capture_restarts"),
                           should_stop=_STOP.is_set)
    n_seg = n_new = n_repeat = 0
    seg = sim = None  # bound per segment below; _publish reads the current ones

    def _publish(label: str, tail: str = "") -> None:
        """Flush the segment counters to the heartbeat and print the status line
        both novelty branches share.
        """
        hb.set(segments=n_seg, novel=n_new, repeat=n_repeat, queue_depth=q.qsize())
        hb.flush()
        print(f"  {label} {seg.duration_s:5.1f}s sim={sim:.3f}{tail} "
              f"[{n_new} new / {n_repeat} repeat, q={q.qsize()}]", flush=True)

    try:
        for seg in segment_stream(_tee_to_same(frames, monitor), cfg):
            if _STOP.is_set():
                break
            n_seg += 1
            hb.touch("last_segment_at")  # a segment means the radio/capture is alive
            rms_db, peak_db = segment_level_dbfs(seg.samples)
            hb.set(last_segment_dbfs=rms_db, last_segment_peak_dbfs=peak_db)
            vec, digest = fp.compute(seg.samples)
            if cfg.fp_dump_path:   # diagnostic; records every segment, gated or not
                dump_vector(cfg.fp_dump_path, _utc_now_iso(), vec)
            seen_at = time.monotonic()
            sim = gate.best_similarity(vec, seen_at)
            if sim >= cfg.fp_similarity_threshold:
                n_repeat += 1
                _publish(". repeat")
                continue
            gate.add(vec, seen_at)
            n_new += 1
            hb.touch("last_novel_at")
            prio = _PRIO_ALERT if time.monotonic() < alert_until[0] else _PRIO_NORMAL
            q.put((prio, next(seq), (seg, digest)))
            _publish("+ novel ",
                     " -> queued" + (" PRIORITY" if prio == _PRIO_ALERT else ""))
    finally:
        _STOP.set()
        q.put((-1, next(seq), None))  # highest-priority poison pill -> prompt exit
        worker.join(timeout=5)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wxparser", description=__doc__)
    parser.add_argument("--once", action="store_true", help="stop after first novel save")
    parser.add_argument("--file", type=Path,
                        help="transcribe an existing WAV instead of capturing")
    args = parser.parse_args(argv)

    cfg = Config()
    if args.file:
        return run_file(args.file, cfg)
    return run_live(cfg, once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())
