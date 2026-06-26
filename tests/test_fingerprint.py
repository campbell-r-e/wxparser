"""fingerprint.py: mel-spectral fingerprint + cosine novelty gate."""

from __future__ import annotations

import numpy as np

from wxparser.config import Config
from wxparser.fingerprint import Fingerprinter, NoveltyGate


def _audio(seed: int, n: int = 16000) -> np.ndarray:
    return (np.random.RandomState(seed).randn(n) * 5000).astype(np.int16)


def test_same_audio_is_identical_fingerprint():
    fp = Fingerprinter(Config())
    a = _audio(0)
    v1, d1 = fp.compute(a)
    v2, d2 = fp.compute(a.copy())
    assert d1 == d2
    assert abs(float(v1 @ v2) - 1.0) < 1e-6   # unit vectors, cosine == 1


def test_different_audio_not_identical():
    fp = Fingerprinter(Config())
    va, _ = fp.compute(_audio(1))
    vb, _ = fp.compute(_audio(2))
    assert float(va @ vb) < 0.999


def test_short_input_is_padded():
    fp = Fingerprinter(Config())
    v, d = fp.compute(np.ones(8, dtype=np.int16))   # < n_fft -> padded path
    assert v.size > 0 and len(d) == 16


def test_novelty_gate_history():
    cfg = Config()
    fp = Fingerprinter(cfg)
    g = NoveltyGate(cfg)
    v, _ = fp.compute(_audio(3))
    assert g.best_similarity(v) == 0.0 and g.is_novel(v)   # empty history
    g.add(v)
    assert g.best_similarity(v) > 0.99 and not g.is_novel(v)
