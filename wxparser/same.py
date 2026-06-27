"""SAME (Specific Area Message Encoding) decoder — pure numpy, offline, MIT.

SAME is the AFSK data burst EAS/NWR sends before a spoken alert (PLAN §9). It
rides in the same continuous audio we already capture, so no extra hardware is
needed. We demodulate it ourselves rather than shelling out to a GPL decoder:
that keeps the stack MIT, fully offline, and testable with a synthesized signal.

Wire format (per NWS): continuous-phase AFSK, 520.83 bit/s, mark (1) = 2083.3 Hz,
space (0) = 1562.5 Hz, 8-bit ASCII bytes sent LSB-first, each header preceded by
16 bytes of 0xAB preamble and transmitted three times for 2-of-3 voting:

    ZCZC-ORG-EEE-PSSCCC-PSSCCC...+TTTT-JJJHHMM-LLLLLLLL-
"""

from __future__ import annotations

import json
import re
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import numpy as np

from .data.same_events import event_label, originator_label

BAUD = 520.833
MARK_HZ = 2083.333
SPACE_HZ = 1562.5
PREAMBLE_BYTE = 0xAB
PREAMBLE_LEN = 16

_HEADER_RE = re.compile(
    r"ZCZC-([A-Z]{3})-([A-Z0-9]{3})((?:-\d{6})+)\+(\d{4})-(\d{7})-([\w/ ]{1,8})-?"
)


@lru_cache(maxsize=1)
def _fips_table() -> dict:
    path = Path(__file__).parent / "data" / "fips.json"
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):  # pragma: no cover - bundled data file always present/valid
        return {}


def fips_county(loc6: str) -> str | None:
    """SAME 6-digit PSSCCC -> 'County, ST' (P = part-of-county indicator)."""
    if len(loc6) != 6:
        return None
    return _fips_table().get(loc6[1:])  # drop the part-of-county digit


@dataclass
class SAMEMessage:
    raw: str
    originator: str
    event: str
    areas: list[str]                       # raw 6-digit location codes
    purge: str                             # TTTT (HHMM duration)
    issued: str                            # JJJHHMM (Julian day + UTC time)
    station: str
    counties: list[str] = field(default_factory=list)

    @property
    def originator_label(self) -> str:
        return originator_label(self.originator)

    @property
    def event_label(self) -> str:
        return event_label(self.event)

    @property
    def purge_minutes(self) -> int:
        return int(self.purge[:2]) * 60 + int(self.purge[2:])

    def to_record(self) -> dict:
        return {
            "type": "same_alert",
            "originator": self.originator,
            "originator_label": self.originator_label,
            "event": self.event,
            "event_label": self.event_label,
            "areas": self.areas,
            "counties": self.counties,
            "purge_minutes": self.purge_minutes,
            "issued_raw": self.issued,
            "station": self.station.strip(),
            "raw": self.raw,
        }


def parse_header(text: str) -> SAMEMessage | None:
    m = _HEADER_RE.search(text)
    if not m:
        return None
    org, event, locs, purge, issued, station = m.groups()
    areas = [a for a in locs.split("-") if a]
    counties = [c for c in (fips_county(a) for a in areas) if c]
    return SAMEMessage(
        raw=m.group(0),
        originator=org,
        event=event,
        areas=areas,
        purge=purge,
        issued=issued,
        station=station,
        counties=counties,
    )


# --------------------------------------------------------------------------- #
# Demodulation
# --------------------------------------------------------------------------- #
def _boxcar(x: np.ndarray, win: int) -> np.ndarray:
    return np.convolve(x, np.ones(win) / win, mode="same")


def _soft_decision(audio: np.ndarray, sr: int) -> tuple[np.ndarray, float]:
    """Noncoherent FSK detector: mark-energy minus space-energy per sample."""
    x = audio.astype(np.float64)
    x /= (np.max(np.abs(x)) or 1.0)
    n = np.arange(x.size)
    spb = sr / BAUD
    win = max(4, int(round(spb)))

    def energy(freq: float) -> np.ndarray:
        w = 2 * np.pi * freq / sr
        i = _boxcar(x * np.cos(w * n), win)
        q = _boxcar(x * np.sin(w * n), win)
        return i * i + q * q

    return energy(MARK_HZ) - energy(SPACE_HZ), spb


def _bits_to_text(bits: np.ndarray, bit_offset: int) -> str:
    chars = []
    for i in range(bit_offset, bits.size - 7, 8):
        byte = 0
        for j in range(8):  # LSB first
            byte |= int(bits[i + j]) << j
        chars.append(chr(byte) if 32 <= byte < 127 else "\x00")
    return "".join(chars)


def decode(audio: np.ndarray, sr: int = 16000) -> list[SAMEMessage]:
    """Decode any SAME headers present in an int16/float audio buffer.

    Brute-forces bit phase and byte alignment (cheap), collects every candidate
    that contains a parseable ZCZC header, and returns the de-duplicated set.
    """
    if audio.size < sr // 4:
        return []
    soft, spb = _soft_decision(audio, sr)
    n_bits = int(soft.size / spb)
    if n_bits < 8:  # pragma: no cover - defensive: not enough bits to be a header
        return []

    seen: dict[str, SAMEMessage] = {}
    # try a handful of sub-bit phases for robustness
    for frac in (0.5, 0.4, 0.6, 0.3, 0.7):
        idx = np.round((np.arange(n_bits) + frac) * spb).astype(int)
        idx = idx[idx < soft.size]
        bits = (soft[idx] > 0).astype(np.uint8)
        for bit_offset in range(8):
            text = _bits_to_text(bits, bit_offset)
            if "ZCZC" not in text:
                continue
            msg = parse_header(text)
            if msg is not None:  # pragma: no branch - "ZCZC" present implies a parseable header
                seen.setdefault(msg.raw, msg)
    return list(seen.values())


def looks_like_same(audio: np.ndarray, sr: int = 16000, band_ratio: float = 0.35) -> bool:
    """Cheap gate: is most of the energy concentrated in the two SAME tones?

    SAME is pure FSK at 1562.5/2083.3 Hz, so nearly all energy sits in those two
    narrow bins; voice/TTS spreads energy across the spectrum. The concentration
    ratio cleanly separates a data burst from speech.
    """
    if audio.size < sr // 8:
        return False
    x = audio.astype(np.float64)
    if not np.any(x):
        return False
    x = x * np.hanning(x.size)
    spec = np.abs(np.fft.rfft(x)) ** 2
    freqs = np.fft.rfftfreq(x.size, 1.0 / sr)
    total = float(spec.sum()) + 1e-12
    in_band = (np.abs(freqs - MARK_HZ) < 60) | (np.abs(freqs - SPACE_HZ) < 60)
    return (float(spec[in_band].sum()) / total) > band_ratio


# --------------------------------------------------------------------------- #
# Live monitor — taps the continuous frame stream, decodes SAME bursts
# --------------------------------------------------------------------------- #
class SAMEMonitor:
    """Detects a SAME burst in the live audio and decodes it once it ends.

    Fed the same frames as the VAD segmenter. It buffers recent audio, watches
    for the tone-concentrated burst, and when the burst ends runs `decode()` on
    the buffer (which spans the 3 repeated transmissions). Each distinct header
    fires `on_alert(SAMEMessage)` once.
    """

    def __init__(self, cfg, on_alert: Callable[["SAMEMessage"], None]):
        self.cfg = cfg
        self.on_alert = on_alert
        self.sr = cfg.sample_rate
        self._buf: deque[np.ndarray] = deque(maxlen=int(cfg.same_buffer_s / cfg.frame_seconds))
        self._detect_frames = max(1, int(cfg.same_detect_s / cfg.frame_seconds))
        self._silence_needed = cfg.same_silence_s
        self._capturing = False
        self._quiet_s = 0.0
        self._recent_raw: deque[str] = deque(maxlen=32)

    def feed(self, frame: np.ndarray, t: float) -> None:
        self._buf.append(frame)
        if len(self._buf) < self._detect_frames:
            return
        window = np.concatenate(list(self._buf)[-self._detect_frames:])
        burst = looks_like_same(window, self.sr, self.cfg.same_band_ratio)
        if burst:
            self._capturing = True
            self._quiet_s = 0.0
        elif self._capturing:
            self._quiet_s += self.cfg.frame_seconds
            if self._quiet_s >= self._silence_needed:
                self._flush()
                self._capturing = False

    def _flush(self) -> None:
        audio = np.concatenate(list(self._buf))
        for msg in decode(audio, self.sr):
            if msg.raw in self._recent_raw:
                continue
            self._recent_raw.append(msg.raw)
            self.on_alert(msg)


# --------------------------------------------------------------------------- #
# Encoder (test/diagnostic only)
# --------------------------------------------------------------------------- #
def encode(header: str, sr: int = 16000, amplitude: float = 0.7) -> np.ndarray:
    """Synthesize the AFSK audio for a SAME header (with 0xAB preamble).

    Returns float32 mono. Used by tests and for offline diagnostics — the live
    pipeline only ever decodes.
    """
    data = bytes([PREAMBLE_BYTE] * PREAMBLE_LEN) + header.encode("ascii")
    bits: list[int] = []
    for byte in data:
        bits.extend((byte >> j) & 1 for j in range(8))  # LSB first

    spb = sr / BAUD
    total = int(round(len(bits) * spb))
    phase = 0.0
    out = np.empty(total, dtype=np.float64)
    for s in range(total):
        bit_index = min(len(bits) - 1, int(s / spb))
        freq = MARK_HZ if bits[bit_index] else SPACE_HZ
        phase += 2 * np.pi * freq / sr
        out[s] = amplitude * np.sin(phase)
    return out.astype(np.float32)
