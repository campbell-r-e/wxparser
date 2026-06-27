"""Optional speech enhancement for the segment->STT path (OFF by default).

A mild, conservative DSP chain applied to each segment before whisper sees it.
On this station's audio the line carries 60 Hz mains-hum harmonics plus a little
broadband hiss; an A/B whisper test over a multi-sample clip showed this chain
never made transcription worse and occasionally fixed a word, while anything
more aggressive REGRESSED it (boosting the consonant band turned "Highs"->"Pies"
and "East"->"These"; a plain low-pass alone also broke "Highs"->"Pies"). So the
chain is deliberately gentle and the makeup gain only restores the ORIGINAL
level — it never brightens or over-drives the speech.

Chain: cascaded biquad notches at the mains-hum harmonics -> low-pass ->
STFT spectral subtraction (noise estimated from the segment's own quietest
frames) -> level-matched makeup gain. Pure numpy, fully offline. The benefit is
marginal on this station, so it ships behind WX_STT_ENHANCE (default 0) for a
new deployment to A/B before trusting it.
"""

from __future__ import annotations

import numpy as np

from .config import Config

_NFFT = 512
_HOP = 256


def _biquad(sig: np.ndarray, b0: float, b1: float, b2: float, a1: float, a2: float) -> np.ndarray:
    y = np.empty_like(sig)
    x1 = x2 = y1 = y2 = 0.0
    for n in range(len(sig)):
        xn = sig[n]
        yn = b0 * xn + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
        y[n] = yn
        x2, x1, y2, y1 = x1, xn, y1, yn
    return y


def _notch(sig: np.ndarray, f0: float, sr: int, q: float = 30.0) -> np.ndarray:
    """Narrow band-reject biquad (~f0/q Hz wide) to spare nearby speech."""
    w0 = 2 * np.pi * f0 / sr
    c, s = np.cos(w0), np.sin(w0)
    a = s / (2 * q)
    a0 = 1 + a
    return _biquad(sig, 1 / a0, -2 * c / a0, 1 / a0, -2 * c / a0, (1 - a) / a0)


def _lowpass(sig: np.ndarray, fc: float, sr: int, q: float = 0.707) -> np.ndarray:
    w0 = 2 * np.pi * fc / sr
    c, s = np.cos(w0), np.sin(w0)
    a = s / (2 * q)
    a0 = 1 + a
    return _biquad(sig, (1 - c) / 2 / a0, (1 - c) / a0, (1 - c) / 2 / a0, -2 * c / a0, (1 - a) / a0)


def _spectral_subtract(sig: np.ndarray, noise_mag: np.ndarray, alpha: float, floor: float) -> np.ndarray:
    """STFT spectral subtraction: knock the estimated noise magnitude out of each
    frame, keeping a spectral floor so speech isn't gated into 'musical noise'."""
    win = np.hanning(_NFFT)
    out = np.zeros(len(sig) + _NFFT)
    nrm = np.zeros(len(sig) + _NFFT)
    for i in range(0, len(sig) - _NFFT, _HOP):
        sp = np.fft.rfft(sig[i:i + _NFFT] * win)
        mag, ph = np.abs(sp), np.angle(sp)
        clean = np.maximum(mag - alpha * noise_mag, floor * mag)
        out[i:i + _NFFT] += np.fft.irfft(clean * np.exp(1j * ph), _NFFT) * win
        nrm[i:i + _NFFT] += win ** 2
    nrm[nrm < 1e-6] = 1.0
    return (out / nrm)[:len(sig)]


def _hum_harmonics(mains_hz: float, sr: int) -> list[float]:
    """Harmonics of the mains tone that fall in/just below the speech band. The
    fundamental (50/60 Hz) sits under speech, so notch the 2nd..6th harmonics."""
    nyq = sr / 2
    return [mains_hz * k for k in range(2, 7) if mains_hz * k < nyq]


def enhance(samples: np.ndarray, cfg: Config) -> np.ndarray:
    """Apply the mild enhancement chain to an int16 mono segment and return int16.

    A safe no-op (returns the input unchanged) when the segment is too short to
    estimate noise or to run the STFT — it never raises, so a bad segment can't
    take down the STT worker.
    """
    x0 = np.asarray(samples)
    if x0.size < 2 * _NFFT:
        return samples
    x = x0.astype(np.float64)
    x = x - x.mean()

    # noise magnitude estimate from the segment's own quietest 100 ms frames
    fl = max(int(0.1 * cfg.sample_rate), _NFFT)
    nf = len(x) // fl
    if nf < 2:
        return samples
    fr = x[:nf * fl].reshape(nf, fl)
    rms = np.sqrt((fr ** 2).mean(1))
    quiet = np.where(rms <= np.percentile(rms, 25))[0]
    win = np.hanning(_NFFT)
    noise_mag = np.mean([np.abs(np.fft.rfft(x[j * fl:j * fl + _NFFT] * win)) for j in quiet], axis=0)

    y = x.copy()
    for f0 in _hum_harmonics(cfg.stt_enhance_mains_hz, cfg.sample_rate):
        y = _notch(y, f0, cfg.sample_rate)
    y = _lowpass(y, cfg.stt_enhance_lowpass_hz, cfg.sample_rate)
    y = _spectral_subtract(y, noise_mag, cfg.stt_enhance_alpha, cfg.stt_enhance_floor)

    # makeup gain: restore the ORIGINAL rms — never above it (boosting regresses STT)
    in_rms = np.sqrt((x ** 2).mean())
    out_rms = np.sqrt((y ** 2).mean())
    y *= in_rms / max(out_rms, 1e-9)
    return np.clip(np.round(y), -32768, 32767).astype("<i2")
