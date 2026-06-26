"""Audio capture.

A single long-lived `arecord` process streams raw signed-16-bit mono PCM to
stdout; we slice that stream into fixed-length windows in Python and hand each
window to the caller as a finished WAV file. Using one persistent process (vs.
re-spawning arecord per window) avoids the gap that device open/close would
introduce between windows.

`arecord` (alsa-utils) is GPL, but it is invoked across a process boundary
(subprocess + pipe), so the MIT core never links it — see PLAN §2.2.
"""

from __future__ import annotations

import subprocess
import sys
import time
import wave
from collections.abc import Iterator
from pathlib import Path

import numpy as np

from .config import Config


def _arecord_cmd(cfg: Config) -> list[str]:
    return [
        "arecord",
        "-D", cfg.alsa_device,
        "-f", "S16_LE",
        "-c", str(cfg.channels),
        "-r", str(cfg.sample_rate),
        "-t", "raw",
        "--quiet",
    ]


def _write_wav(path: Path, pcm: bytes, cfg: Config) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(cfg.channels)
        w.setsampwidth(2)  # S16_LE
        w.setframerate(cfg.sample_rate)
        w.writeframes(pcm)


def stream_windows(cfg: Config, work_dir: Path) -> Iterator[Path]:  # pragma: no cover - Phase-1 path, superseded by stream_frames
    """Yield successive WAV files, each `cfg.window_seconds` long, forever.

    The yielded path is overwritten on the next iteration; consumers must finish
    with it (transcribe) before requesting the next window.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    bytes_per_window = int(cfg.window_seconds * cfg.sample_rate) * 2 * cfg.channels
    wav_path = work_dir / "window.wav"

    proc = subprocess.Popen(_arecord_cmd(cfg), stdout=subprocess.PIPE)
    if proc.stdout is None:  # pragma: no cover - defensive
        raise RuntimeError("arecord produced no stdout pipe")
    try:
        while True:
            pcm = _read_exact(proc.stdout, bytes_per_window)
            if pcm is None:
                returncode = proc.poll()
                raise RuntimeError(f"arecord stream ended (returncode={returncode})")
            _write_wav(wav_path, pcm, cfg)
            yield wav_path
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()


def stream_frames(cfg: Config, on_retry=None) -> Iterator[tuple[np.ndarray, float]]:
    """Yield (int16 mono frame, frame_start_seconds) continuously from the radio.

    Frames are `cfg.frame_seconds` long and feed the streaming segmenter. The
    monotonically increasing timestamp lets the segmenter assign wall-clock-ish
    offsets to detected speech segments. `on_retry` (if given) is called once per
    arecord re-spawn so the watchdog can count capture restarts.
    """
    frame_samples = max(1, int(cfg.frame_seconds * cfg.sample_rate))
    frame_bytes = frame_samples * 2 * cfg.channels
    sample_index = 0
    failures = 0

    while True:
        proc = subprocess.Popen(_arecord_cmd(cfg), stdout=subprocess.PIPE)
        if proc.stdout is None:  # pragma: no cover - defensive
            raise RuntimeError("arecord produced no stdout pipe")
        try:
            while True:
                raw = _read_exact(proc.stdout, frame_bytes)
                if raw is None:
                    break  # arecord exited; fall through to retry
                failures = 0  # a healthy read resets the failure budget
                frame = np.frombuffer(raw, dtype="<i2")
                yield frame, sample_index / cfg.sample_rate
                sample_index += frame_samples
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:  # pragma: no cover
                proc.kill()

        failures += 1
        if on_retry is not None:
            on_retry()
        if failures > cfg.capture_max_retries:
            raise RuntimeError(
                f"arecord failed {failures} times (last returncode={proc.poll()})"
            )
        print(
            f"  ! arecord ended (rc={proc.poll()}); retry {failures}/{cfg.capture_max_retries}"
            f" in {cfg.capture_retry_backoff_s}s",
            file=sys.stderr, flush=True,
        )
        time.sleep(cfg.capture_retry_backoff_s)


def write_wav(path: Path, pcm_int16: np.ndarray, cfg: Config) -> None:
    """Write an int16 numpy array to a WAV file (used to hand segments to STT)."""
    _write_wav(path, np.asarray(pcm_int16, dtype="<i2").tobytes(), cfg)


def _read_exact(stream, n: int) -> bytes | None:
    """Read exactly n bytes; return None if the stream closes early."""
    buf = bytearray()
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)
