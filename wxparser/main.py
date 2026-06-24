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

from .capture import stream_frames
from .config import CONFIG, Config
from .fingerprint import Fingerprinter, NoveltyGate
from .segment import Segment, segment_stream
from .store import append_report, build_report
from .stt import Transcript, is_blank, transcribe, transcribe_samples

_STOP = threading.Event()


def _handle_signal(signum, frame):  # pragma: no cover
    _STOP.set()


def _save(transcript: Transcript, cfg: Config, duration_s: float, fingerprint: str) -> None:
    report = build_report(transcript, cfg, duration_s=duration_s, fingerprint=fingerprint)
    path = append_report(report, cfg)
    print(
        f"[{report['captured_at']}] NEW  {report['product_type']:<28} "
        f"{duration_s:5.1f}s {len(transcript.segments):>2} seg -> {path}",
        flush=True,
    )
    print(f"    {transcript.text}", flush=True)


def run_file(wav_path: Path, cfg: Config) -> int:
    transcript = transcribe(wav_path, cfg)
    if is_blank(transcript):
        print("[blank audio — nothing to transcribe]", flush=True)
        return 0
    _save(transcript, cfg, duration_s=cfg.window_seconds, fingerprint="")
    return 0


def _stt_worker(q: "queue.Queue[tuple[Segment, str] | None]", cfg: Config, once: bool) -> None:
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
            _save(transcript, cfg, duration_s=seg.duration_s, fingerprint=digest)
            if once:
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
    q: "queue.Queue[tuple[Segment, str] | None]" = queue.Queue()
    worker = threading.Thread(target=_stt_worker, args=(q, cfg, once), daemon=True)
    worker.start()

    n_seg = n_new = n_repeat = 0
    try:
        for seg in segment_stream(stream_frames(cfg), cfg):
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
