"""enhance.py: the optional mild speech-enhancement chain (off by default)."""

from __future__ import annotations

import dataclasses

import numpy as np

from wxparser.config import Config
from wxparser.enhance import _hum_harmonics, enhance

SR = 16000


def _sig(seconds=1.0, hum_amp=4000.0):
    """A speech-band tone (700 Hz) plus a strong 120 Hz mains-hum harmonic."""
    t = np.arange(int(seconds * SR)) / SR
    speech = 6000 * np.sin(2 * np.pi * 700 * t)
    hum = hum_amp * np.sin(2 * np.pi * 120 * t)
    return np.clip(speech + hum, -32768, 32767).astype(np.int16)


def _tone_energy(samples, hz, half=4):
    x = samples.astype(np.float64)
    sp = np.abs(np.fft.rfft(x))
    f = np.fft.rfftfreq(len(x), 1 / SR)
    return sp[(f >= hz - half) & (f <= hz + half)].max()


def test_enhance_attenuates_hum_keeps_speech():
    cfg = Config()
    raw = _sig()
    out = enhance(raw, cfg)
    assert out.dtype == np.dtype("<i2") and out.shape == raw.shape
    # the 120 Hz hum harmonic is notched well down...
    assert _tone_energy(out, 120) < 0.25 * _tone_energy(raw, 120)
    # ...while the 700 Hz speech tone is largely preserved
    assert _tone_energy(out, 700) > 0.5 * _tone_energy(raw, 700)


def test_enhance_makeup_restores_level_not_above():
    # level-matched makeup: output rms must not exceed the input rms (no boosting)
    cfg = Config()
    raw = _sig()
    out = enhance(raw, cfg)
    in_rms = np.sqrt((raw.astype(np.float64) ** 2).mean())
    out_rms = np.sqrt((out.astype(np.float64) ** 2).mean())
    assert out_rms <= in_rms * 1.05


def test_enhance_noop_on_too_short_segment():
    raw = np.ones(100, dtype=np.int16)          # < 2*NFFT -> returned unchanged
    out = enhance(raw, Config())
    assert out is raw


def test_enhance_noop_when_too_few_frames():
    raw = np.ones(2000, dtype=np.int16)         # >= 2*NFFT but < 2 full 100ms frames
    out = enhance(raw, Config())
    assert out is raw


def test_enhance_handles_silence():
    out = enhance(np.zeros(4000, dtype=np.int16), Config())  # rms 0, must not divide-by-zero
    assert out.shape == (4000,) and not np.any(out)


def test_hum_harmonics_clipped_at_nyquist():
    # below 350 Hz Nyquist only the 2nd..5th harmonics fit; the 6th (360) drops
    assert _hum_harmonics(60, 700) == [120, 180, 240, 300]
    # at the real sample rate all five harmonics are kept
    assert _hum_harmonics(60, SR) == [120, 180, 240, 300, 360]


def test_mains_hz_configurable():
    cfg = dataclasses.replace(Config(), stt_enhance_mains_hz=50.0)
    out = enhance(_sig(hum_amp=4000.0), cfg)    # notches move to 50Hz series
    assert out.shape == (SR,)
