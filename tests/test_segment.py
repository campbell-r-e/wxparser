"""segment.py: energy-VAD segmentation over synthetic frames."""

from __future__ import annotations

import numpy as np

from wxparser.config import Config
from wxparser.segment import _frame_dbfs, segment_level_dbfs, segment_stream


def _frames(pattern: str, cfg: Config):
    n = int(cfg.frame_seconds * cfg.sample_rate)
    t = 0.0
    for ch in pattern:
        amp = 8000 if ch == "S" else 0
        yield np.full(n, amp, dtype=np.int16), t
        t += cfg.frame_seconds


def test_frame_dbfs():
    assert _frame_dbfs(np.array([], dtype=np.int16)) == -120.0
    assert _frame_dbfs(np.zeros(64, dtype=np.int16)) == -120.0
    assert _frame_dbfs(np.full(64, 8000, dtype=np.int16)) > -35.0


def test_segment_level_dbfs():
    assert segment_level_dbfs(np.array([], dtype=np.int16)) == (-120.0, -120.0)
    assert segment_level_dbfs(np.zeros(64, dtype=np.int16)) == (-120.0, -120.0)
    rms_db, peak_db = segment_level_dbfs(np.full(64, 16384, dtype=np.int16))
    assert peak_db >= rms_db and -7.0 < peak_db < -5.0   # 16384/32768 = -6.0 dBFS


def test_yields_one_speech_segment():
    cfg = Config()  # frame 20ms; min_speech/min_silence = 1.0s = 50 frames
    pattern = "s" + "S" * 70 + "s" * 60      # pre-roll, 1.4s speech, 1.2s silence (closes)
    segs = list(segment_stream(_frames(pattern, cfg), cfg))
    assert len(segs) == 1 and segs[0].samples.size > 0 and segs[0].duration_s > 1.0


def test_too_short_speech_is_dropped():
    cfg = Config()
    pattern = "S" * 10                        # 0.2s speech, stream ends -> < 1.0s, dropped
    assert list(segment_stream(_frames(pattern, cfg), cfg)) == []


def test_flush_open_segment_at_stream_end():
    cfg = Config()
    pattern = "s" + "S" * 70                  # never sees closing silence -> flushed
    assert len(list(segment_stream(_frames(pattern, cfg), cfg))) == 1


def test_max_segment_length_forces_cut():
    cfg = Config(vad_max_segment_s=1.5)       # 75 frames max (> 1.0s min_speech)
    pattern = "S" * 90                        # exceeds max -> cut at 1.5s, survives
    assert len(list(segment_stream(_frames(pattern, cfg), cfg))) == 1
