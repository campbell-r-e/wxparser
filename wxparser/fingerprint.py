"""Audio fingerprint + novelty gate (the dedup workhorse, PLAN §5).

Computes a cheap, fixed-size spectral fingerprint per segment using only numpy
(a small mel-style log-energy spectrogram pooled onto a fixed time grid, then
L2-normalised). Because NWR replays deterministic TTS, a repeated product yields
a near-identical fingerprint, so cosine similarity against recently-seen
fingerprints cleanly separates "repeat" (skip) from "novel" (transcribe) without
running STT.
"""

from __future__ import annotations

import hashlib
from collections import deque

import numpy as np

from .config import Config

_N_FFT = 512


def _hz_to_mel(hz: float) -> float:
    return 2595.0 * np.log10(1.0 + hz / 700.0)


def _mel_to_hz(mel: float) -> float:
    return 700.0 * (10.0 ** (mel / 2595.0) - 1.0)


def _mel_filterbank(sr: int, n_fft: int, n_mels: int) -> np.ndarray:
    """(n_mels, n_fft//2+1) triangular mel filterbank over 100..min(sr/2,4000) Hz."""
    f_min, f_max = 100.0, min(sr / 2.0, 4000.0)
    mels = np.linspace(_hz_to_mel(f_min), _hz_to_mel(f_max), n_mels + 2)
    hz = np.array([_mel_to_hz(m) for m in mels])
    bins = np.floor((n_fft + 1) * hz / sr).astype(int)
    bins = np.clip(bins, 0, n_fft // 2)
    fb = np.zeros((n_mels, n_fft // 2 + 1), dtype=np.float64)
    for m in range(1, n_mels + 1):
        lo, ctr, hi = bins[m - 1], bins[m], bins[m + 1]
        if ctr > lo:  # pragma: no branch - bins are non-degenerate with real sr/n_mels
            fb[m - 1, lo:ctr] = (np.arange(lo, ctr) - lo) / (ctr - lo)
        if hi > ctr:  # pragma: no branch
            fb[m - 1, ctr:hi] = (hi - np.arange(ctr, hi)) / (hi - ctr)
    return fb


class Fingerprinter:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.n_fft = _N_FFT
        self.hop = _N_FFT // 2
        self.fb = _mel_filterbank(cfg.sample_rate, self.n_fft, cfg.fp_n_mels)
        self.window = np.hanning(self.n_fft)

    def compute(self, samples: np.ndarray) -> tuple[np.ndarray, str]:
        """Return (L2-normalised fingerprint vector, short hex digest)."""
        x = samples.astype(np.float64) / 32768.0
        if x.size < self.n_fft:
            x = np.pad(x, (0, self.n_fft - x.size))

        # framed magnitude spectrogram -> mel log-energies (frames x n_mels)
        n_frames = 1 + (x.size - self.n_fft) // self.hop
        mel = np.empty((n_frames, self.cfg.fp_n_mels), dtype=np.float64)
        for i in range(n_frames):
            seg = x[i * self.hop:i * self.hop + self.n_fft] * self.window
            mag = np.abs(np.fft.rfft(seg, n=self.n_fft))
            mel[i] = np.log1p(self.fb @ (mag * mag))

        # pool time axis onto a fixed grid so length differences don't matter
        pooled = _pool_time(mel, self.cfg.fp_time_bins)  # (time_bins x n_mels)
        vec = pooled.reshape(-1).astype(np.float32)
        norm = np.linalg.norm(vec)
        if norm > 0:
            vec = vec / norm

        # digest: sign relative to the median -> bits -> sha1 (stable id for storage)
        bits = (vec > np.median(vec)).astype(np.uint8)
        digest = hashlib.sha1(np.packbits(bits).tobytes()).hexdigest()[:16]
        return vec, digest


def _pool_time(mel: np.ndarray, time_bins: int) -> np.ndarray:
    n = mel.shape[0]
    if n == 0:
        return np.zeros((time_bins, mel.shape[1]))
    idx = np.linspace(0, n, time_bins + 1).astype(int)
    out = np.empty((time_bins, mel.shape[1]))
    for b in range(time_bins):
        lo, hi = idx[b], max(idx[b] + 1, idx[b + 1])
        out[b] = mel[lo:hi].mean(axis=0)
    return out


class NoveltyGate:
    """Keeps recent fingerprints; the caller compares best_similarity() to the
    configured threshold to decide novelty (main.py's repeat/novel branch).
    """

    def __init__(self, cfg: Config):
        self.history: deque[np.ndarray] = deque(maxlen=cfg.gate_history)

    def best_similarity(self, vec: np.ndarray) -> float:
        if not self.history:
            return 0.0
        mat = np.stack(self.history)          # (h x d), rows already unit-norm
        return float(np.max(mat @ vec))       # cosine == dot for unit vectors

    def add(self, vec: np.ndarray) -> None:
        self.history.append(vec)
