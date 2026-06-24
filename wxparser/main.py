"""wxparser entry point — Phase 1: transcribe everything.

Continuously captures fixed windows of NWR audio, transcribes each with
whisper.cpp, and appends every non-blank window as a JSON report. Novelty gating
(audio fingerprint dedup) and text dedup arrive in Phase 2/3; for now this is the
"transcribe everything" milestone that proves the live pipeline.

Usage:
  wxparser                 # live capture loop (Ctrl-C to stop)
  wxparser --once          # capture and transcribe a single window, then exit
  wxparser --file a.wav    # transcribe an existing WAV (no capture) — for tests
"""

from __future__ import annotations

import argparse
import signal
import sys
import tempfile
from pathlib import Path

from .capture import stream_windows
from .config import CONFIG, Config
from .store import append_report, build_report
from .stt import Transcript, is_blank, transcribe

_RUNNING = True


def _handle_sigterm(signum, frame):  # pragma: no cover
    global _RUNNING
    _RUNNING = False


def _emit(transcript: Transcript, cfg: Config, duration_s: float) -> None:
    report = build_report(transcript, cfg, duration_s=duration_s)
    path = append_report(report, cfg)
    print(
        f"[{report['captured_at']}] {report['product_type']:<28} "
        f"{len(transcript.segments):>2} seg  -> {path}",
        flush=True,
    )
    print(f"    {transcript.text}", flush=True)


def run_file(wav_path: Path, cfg: Config) -> int:
    transcript = transcribe(wav_path, cfg)
    if is_blank(transcript):
        print("[blank audio — nothing to transcribe]", flush=True)
        return 0
    _emit(transcript, cfg, duration_s=cfg.window_seconds)
    return 0


def run_live(cfg: Config, once: bool = False) -> int:
    signal.signal(signal.SIGTERM, _handle_sigterm)
    print(
        f"wxparser: capturing {cfg.station} ({cfg.frequency_mhz} MHz) from "
        f"{cfg.alsa_device} in {cfg.window_seconds:.0f}s windows; model={cfg.model_name}",
        flush=True,
    )
    with tempfile.TemporaryDirectory(prefix="wxparser-") as tmp:
        for wav_path in stream_windows(cfg, Path(tmp)):
            try:
                transcript = transcribe(wav_path, cfg)
            except Exception as e:  # keep the service alive on a single bad window
                print(f"  ! transcription error: {e}", file=sys.stderr, flush=True)
                if once:
                    return 1
                continue
            if is_blank(transcript):
                print("  . (silence/no speech)", flush=True)
            else:
                _emit(transcript, cfg, duration_s=cfg.window_seconds)
            if once or not _RUNNING:
                break
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="wxparser", description=__doc__)
    parser.add_argument("--once", action="store_true", help="one window then exit")
    parser.add_argument("--file", type=Path, help="transcribe an existing WAV instead of capturing")
    args = parser.parse_args(argv)

    cfg = CONFIG
    if args.file:
        return run_file(args.file, cfg)
    try:
        return run_live(cfg, once=args.once)
    except KeyboardInterrupt:  # pragma: no cover
        print("\nstopped.", flush=True)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
