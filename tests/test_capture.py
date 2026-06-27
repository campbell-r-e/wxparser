"""capture.py: arecord command, WAV writing, framed streaming (subprocess mocked)."""

from __future__ import annotations

import itertools
import wave

import numpy as np

import wxparser.capture as capture
from wxparser.config import Config


def test_arecord_cmd():
    cfg = Config(alsa_device="plughw:9,0")
    cmd = capture._arecord_cmd(cfg)
    assert cmd[0] == "arecord" and "plughw:9,0" in cmd and str(cfg.sample_rate) in cmd


def test_write_wav_roundtrip(tmp_path):
    cfg = Config()
    p = tmp_path / "o.wav"
    capture.write_wav(p, np.zeros(320, dtype=np.int16), cfg)
    with wave.open(str(p)) as w:
        assert w.getframerate() == cfg.sample_rate and w.getsampwidth() == 2


def test_read_exact_short_close():
    class S:
        def __init__(self): self.left = [b"ab", b""]
        def read(self, n): return self.left.pop(0)
    assert capture._read_exact(S(), 10) is None      # stream closes early


class _Stdout:
    def __init__(self, chunks): self.chunks = list(chunks)
    def read(self, n): return self.chunks.pop(0) if self.chunks else b""


class _Proc:
    def __init__(self, stdout): self.stdout = stdout
    def poll(self): return 1
    def terminate(self): pass
    def wait(self, timeout=None): pass
    def kill(self): pass


def test_stream_frames_yields_and_counts_retries(monkeypatch):
    cfg = Config(capture_retry_backoff_s=0.0)
    fb = int(cfg.frame_seconds * cfg.sample_rate) * 2 * cfg.channels  # bytes/frame
    # each spawned arecord yields 2 frames then EOF -> a retry
    monkeypatch.setattr(capture.subprocess, "Popen",
                        lambda *a, **k: _Proc(_Stdout([b"\x00" * fb, b"\x01" * fb])))
    retries = []
    frames = list(itertools.islice(
        capture.stream_frames(cfg, on_retry=lambda: retries.append(1)), 3))
    assert len(frames) == 3
    assert frames[0][0].shape[0] == fb // 2          # int16 samples per frame
    assert len(retries) >= 1                          # crossed an EOF -> respawn


def test_stream_frames_stops_immediately_without_spawning(monkeypatch):
    cfg = Config()
    spawned = []
    monkeypatch.setattr(capture.subprocess, "Popen",
                        lambda *a, **k: spawned.append(1) or _Proc(_Stdout([])))
    frames = list(capture.stream_frames(cfg, should_stop=lambda: True))
    assert frames == [] and spawned == []            # never even opened arecord


def test_stream_frames_stops_mid_stream(monkeypatch):
    cfg = Config()
    fb = int(cfg.frame_seconds * cfg.sample_rate) * 2 * cfg.channels
    monkeypatch.setattr(capture.subprocess, "Popen",
                        lambda *a, **k: _Proc(_Stdout([b"\x00" * fb] * 5)))
    calls = {"n": 0}

    def should_stop():
        calls["n"] += 1
        return calls["n"] >= 3                        # stop after the 2nd yielded frame
    frames = list(capture.stream_frames(cfg, should_stop=should_stop))
    assert len(frames) == 2                           # unwound promptly, not forever


def test_stream_frames_stops_at_retry_without_backoff(monkeypatch):
    cfg = Config(capture_retry_backoff_s=5.0)         # long backoff that must NOT block
    monkeypatch.setattr(capture.subprocess, "Popen",
                        lambda *a, **k: _Proc(_Stdout([])))   # immediate EOF -> retry path
    calls = {"n": 0}

    def should_stop():
        calls["n"] += 1
        return calls["n"] > 1                         # false at loop top, true before retry sleep
    frames = list(capture.stream_frames(cfg, should_stop=should_stop))
    assert frames == []                               # exited at the retry check, no 5s sleep


def test_sleep_unless():
    capture._sleep_unless(5.0, lambda: True)          # stop already set -> returns at once
    capture._sleep_unless(0.0, lambda: False)         # zero duration -> no loop
    capture._sleep_unless(0.25, lambda: False)        # actually sleeps a slice or two
