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
import queue
import signal
import sys
import threading
from pathlib import Path

from collections.abc import Iterator

from .capture import stream_frames
from .config import CONFIG, Config
from .db import Database
from .dedup import TextDeduper
from .extract import CityConditionsAggregator, ForecastAggregator, extract_alert_details
from .fingerprint import Fingerprinter, NoveltyGate
from .same import SAMEMessage, SAMEMonitor
from .segment import Segment, segment_stream
from .store import (
    ALERT_PRODUCTS,
    append_report,
    build_alert,
    build_report,
    load_recent_reports,
)
from .stt import Transcript, is_blank, transcribe, transcribe_samples
from .store import _utc_now_iso

_STOP = threading.Event()


def _handle_signal(signum, frame):  # pragma: no cover
    _STOP.set()


def _save(
    transcript: Transcript,
    cfg: Config,
    duration_s: float,
    fingerprint: str,
    deduper: TextDeduper | None = None,
) -> dict | None:
    """Build, text-dedup, and persist a report. Returns the saved report (or None
    if it was dropped as a duplicate)."""
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
    path = append_report(report, cfg)
    print(
        f"[{report['captured_at']}] {tag}  {report['product_type']:<28} "
        f"{duration_s:5.1f}s {len(transcript.segments):>2} seg -> {path}",
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
    append_report(record, cfg)
    if db is not None:
        db.write_alert(record)
    areas = ", ".join(msg.counties) if msg.counties else ", ".join(msg.areas)
    print(
        f"[{record['captured_at']}] ALERT {msg.event_label} ({msg.event}) "
        f"— {areas} — {msg.purge_minutes}min — {msg.station.strip()}",
        flush=True,
    )


def run_file(wav_path: Path, cfg: Config) -> int:
    transcript = transcribe(wav_path, cfg)
    if is_blank(transcript):
        print("[blank audio — nothing to transcribe]", flush=True)
        return 0
    _save(transcript, cfg, duration_s=cfg.window_seconds, fingerprint="")
    return 0


def _stt_worker(
    q: "queue.Queue[tuple[Segment, str] | None]",
    cfg: Config,
    once: bool,
    deduper: TextDeduper,
    aggregator: CityConditionsAggregator,
    forecast: ForecastAggregator,
    db: Database | None,
) -> None:
    while not _STOP.is_set():
        try:
            item = q.get(timeout=0.5)
        except queue.Empty:
            continue
        if item is None:  # poison pill
            break
        seg, digest = item
        try:
            transcript = transcribe_samples(seg.samples, cfg)
        except Exception as e:  # keep the service alive on a single bad segment
            print(f"  ! STT error: {e}", file=sys.stderr, flush=True)
            q.task_done()
            continue
        if is_blank(transcript):
            print(f"  . novel-but-blank {seg.duration_s:5.1f}s", flush=True)
        else:
            text = transcript.text
            now = _utc_now_iso()
            # vote BEFORE dedup so boundary-shifted repeats still contribute readings
            for r in aggregator.update(text):
                if db is not None:
                    db.record_reading(r, now)
                print(f"[{now}] OBS  {r['city']}: {r['condition']}={r['value']}", flush=True)
            if forecast.update(text) and db is not None:
                db.write_forecast(forecast.snapshot(), now, city=forecast.city)
            saved = _save(transcript, cfg, seg.duration_s, digest, deduper)
            if saved is not None and db is not None:
                # structure the spoken narrative of warnings/statements so it can
                # be linked to the SAME header at query time
                details = extract_alert_details(text)
                if details or saved["product_type"] in ALERT_PRODUCTS:
                    db.write_alert_detail(
                        saved["id"], now, saved["product_type"], details, text)
                    if details:
                        print(f"[{now}] ALERT-DETAIL {saved['product_type']}: {details}",
                              flush=True)
            if once and saved is not None:
                _STOP.set()
        q.task_done()


def run_live(cfg: Config, once: bool = False) -> int:
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
    recent = load_recent_reports(cfg, cfg.text_history)
    deduper.prime(recent)
    if recent:
        print(f"  (primed text-dedup with {len(recent)} recent reports)", flush=True)
    aggregator = CityConditionsAggregator(primary_city=cfg.primary_city)
    forecast = ForecastAggregator()
    db = Database(cfg)
    print(f"  (postgres store: {cfg.pg_user}@{cfg.pg_host}:{cfg.pg_port}/{cfg.pg_database})", flush=True)
    # prime aggregators from the store so a restart keeps state
    readings = db.latest_readings()
    if readings:
        aggregator.prime(readings)
    fcs = db.latest_forecasts()
    for fc in fcs:
        if fc["city"] == forecast.city:
            forecast.prime(fc["periods"])
            break
    if readings or fcs:
        print(f"  (primed: {len(readings)} city readings, {len(fcs)} forecast areas)", flush=True)
    q: "queue.Queue[tuple[Segment, str] | None]" = queue.Queue()
    worker = threading.Thread(
        target=_stt_worker, args=(q, cfg, once, deduper, aggregator, forecast, db), daemon=True
    )
    worker.start()

    monitor = SAMEMonitor(cfg, lambda m: _emit_alert(m, cfg, db)) if cfg.same_enabled else None
    if monitor is not None:
        print("  (SAME alert decoding enabled)", flush=True)

    n_seg = n_new = n_repeat = 0
    try:
        for seg in segment_stream(_tee_to_same(stream_frames(cfg), monitor), cfg):
            if _STOP.is_set():
                break
            n_seg += 1
            vec, digest = fp.compute(seg.samples)
            sim = gate.best_similarity(vec)
            if sim >= cfg.fp_similarity_threshold:
                n_repeat += 1
                print(
                    f"  . repeat {seg.duration_s:5.1f}s sim={sim:.3f} "
                    f"[{n_new} new / {n_repeat} repeat, q={q.qsize()}]",
                    flush=True,
                )
                continue
            gate.add(vec)
            n_new += 1
            q.put((seg, digest))
            print(
                f"  + novel  {seg.duration_s:5.1f}s sim={sim:.3f} -> queued "
                f"[{n_new} new / {n_repeat} repeat, q={q.qsize()}]",
                flush=True,
            )
    finally:
        _STOP.set()
        q.put(None)
        worker.join(timeout=5)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wxparser", description=__doc__)
    parser.add_argument("--once", action="store_true", help="stop after first novel save")
    parser.add_argument("--file", type=Path, help="transcribe an existing WAV instead of capturing")
    args = parser.parse_args(argv)

    cfg = CONFIG
    if args.file:
        return run_file(args.file, cfg)
    return run_live(cfg, once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())
