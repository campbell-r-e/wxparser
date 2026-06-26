"""Voice-activity segmentation (energy VAD).

Consumes the continuous frame stream and emits one `Segment` per speech burst,
split on inter-product / inter-sentence silence. NWR audio is deterministic TTS
with clean silences between products, so a simple RMS-threshold VAD segments it
reliably without pulling in a compiled VAD (webrtcvad) or a model (silero/torch).

A segment is emitted when, after speech, silence persists for
`vad_min_silence_s` — or when a single speech run hits `vad_max_segment_s`
(so continuous reads can't buffer unbounded).
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass

import numpy as np

from .config import Config


@dataclass
class Segment:
    samples: np.ndarray  # int16 mono
    start_s: float
    end_s: float

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s


def _frame_dbfs(frame: np.ndarray) -> float:
    if frame.size == 0:
        return -120.0
    x = frame.astype(np.float64) / 32768.0
    rms = float(np.sqrt(np.mean(x * x)))
    if rms <= 1e-9:
        return -120.0
    return 20.0 * np.log10(rms)


def segment_level_dbfs(samples: np.ndarray) -> tuple[float, float]:
    """(rms_dbfs, peak_dbfs) of an int16 PCM segment. Published per segment to the
    heartbeat so the AGC backstop can keep capture gain in the decoder's sweet
    spot (speech above the VAD floor, peaks below clipping)."""
    if samples.size == 0:
        return -120.0, -120.0
    x = np.abs(samples.astype(np.float64) / 32768.0)
    rms = float(np.sqrt(np.mean(x * x)))
    peak = float(np.max(x))
    rms_db = 20.0 * np.log10(rms) if rms > 1e-9 else -120.0
    peak_db = 20.0 * np.log10(peak) if peak > 1e-9 else -120.0
    return round(rms_db, 1), round(peak_db, 1)


def segment_stream(
    frames: Iterable[tuple[np.ndarray, float]], cfg: Config
) -> Iterator[Segment]:
    pad_frames = max(0, int(cfg.vad_pad_s / cfg.frame_seconds))
    min_silence_frames = max(1, int(cfg.vad_min_silence_s / cfg.frame_seconds))
    max_seg_frames = max(1, int(cfg.vad_max_segment_s / cfg.frame_seconds))

    buf: list[np.ndarray] = []        # frames in the current (speech) segment
    pre: list[np.ndarray] = []        # rolling pre-roll of recent silence frames
    seg_start_s = 0.0
    in_speech = False
    silence_run = 0

    for frame, t in frames:
        speech = _frame_dbfs(frame) > cfg.vad_threshold_dbfs

        if not in_speech:
            # maintain a short pre-roll so we don't clip the segment's onset
            pre.append(frame)
            if len(pre) > pad_frames:
                pre.pop(0)
            if speech:
                in_speech = True
                buf = list(pre)
                seg_start_s = max(0.0, t - len(pre) * cfg.frame_seconds)
                pre = []
                silence_run = 0
            continue

        # in speech
        buf.append(frame)
        silence_run = silence_run + 1 if not speech else 0

        end_now = silence_run >= min_silence_frames
        too_long = len(buf) >= max_seg_frames
        if end_now or too_long:
            end_s = t + cfg.frame_seconds
            seg = _finish(buf, seg_start_s, end_s, cfg)
            if seg is not None:
                yield seg
            in_speech = False
            buf = []
            pre = []
            silence_run = 0

    # stream ended (finite input / shutdown): flush any buffered speech
    if in_speech and buf:
        end_s = seg_start_s + len(buf) * cfg.frame_seconds
        seg = _finish(buf, seg_start_s, end_s, cfg)
        if seg is not None:
            yield seg


def _finish(
    buf: list[np.ndarray], start_s: float, end_s: float, cfg: Config
) -> Segment | None:
    if not buf:
        return None
    samples = np.concatenate(buf)
    duration = samples.size / cfg.sample_rate
    if duration < cfg.vad_min_speech_s:
        return None
    return Segment(samples=samples, start_s=round(start_s, 2), end_s=round(end_s, 2))
