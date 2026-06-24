"""Speech-to-text via whisper.cpp.

Runs the `whisper-cli` binary (MIT) as a subprocess against a WAV file and parses
its JSON output into segments. whisper.cpp emits per-segment millisecond offsets,
which we normalise to seconds. Everything is local — no network, ever (PLAN §2.1).
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .config import Config


@dataclass
class Segment:
    start_s: float
    end_s: float
    text: str


@dataclass
class Transcript:
    text: str
    segments: list[Segment]
    language: str


class STTError(RuntimeError):
    pass


def _audio_ctx_for(duration_s: float, cfg: Config) -> int:
    frames = int(duration_s * 50 * 1.2) + 24  # ~50 encoder frames/sec + margin
    return max(cfg.whisper_audio_ctx_min, min(cfg.whisper_audio_ctx_max, frames))


def _wav_duration_s(wav_path: Path) -> float:
    import wave

    with wave.open(str(wav_path), "rb") as w:
        return w.getnframes() / float(w.getframerate() or 1)


def transcribe(wav_path: Path, cfg: Config) -> Transcript:
    out_base = wav_path.with_suffix("")  # whisper writes <out_base>.json
    json_path = out_base.with_suffix(".json")
    cmd = [
        str(cfg.whisper_bin),
        "-m", str(cfg.whisper_model),
        "-f", str(wav_path),
        "-t", str(cfg.whisper_threads),
        "-np",            # no progress prints
        "-oj",            # JSON output
        "-of", str(out_base),
    ]
    if cfg.whisper_dynamic_audio_ctx:
        cmd += ["-ac", str(_audio_ctx_for(_wav_duration_s(wav_path), cfg))]
    if cfg.whisper_fast_decode:
        cmd += ["-bs", "1", "-bo", "1", "-nf", "-mc", "0"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise STTError(f"whisper-cli failed ({proc.returncode}): {proc.stderr.strip()}")
    try:
        data = json.loads(json_path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise STTError(f"could not read whisper JSON at {json_path}: {e}") from e

    segments: list[Segment] = []
    for seg in data.get("transcription", []):
        text = seg.get("text", "").strip()
        if not text:
            continue
        off = seg.get("offsets", {})
        segments.append(
            Segment(
                start_s=round(off.get("from", 0) / 1000.0, 3),
                end_s=round(off.get("to", 0) / 1000.0, 3),
                text=text,
            )
        )
    full_text = " ".join(s.text for s in segments).strip()
    language = data.get("result", {}).get("language", "en")
    return Transcript(text=full_text, segments=segments, language=language)


def transcribe_samples(samples: np.ndarray, cfg: Config) -> Transcript:
    """Transcribe an int16 mono numpy segment by staging it to a temp WAV."""
    from .capture import write_wav  # local import avoids a circular import at load

    with tempfile.TemporaryDirectory(prefix="wxparser-stt-") as tmp:
        wav_path = Path(tmp) / "segment.wav"
        write_wav(wav_path, samples, cfg)
        return transcribe(wav_path, cfg)


def is_blank(transcript: Transcript) -> bool:
    """whisper emits '[BLANK_AUDIO]' (and similar) for non-speech windows."""
    t = transcript.text.strip().lower()
    if not t:
        return True
    return t in {"[blank_audio]", "(dramatic music)"} or t.startswith("[blank")
