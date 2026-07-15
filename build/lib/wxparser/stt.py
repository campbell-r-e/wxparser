"""Speech-to-text via whisper.cpp.

Runs the `whisper-cli` binary (MIT) as a subprocess against a WAV file and parses
its JSON output into segments. whisper.cpp emits per-segment millisecond offsets,
which we normalise to seconds. Everything is local — no network, ever (PLAN §2.1).

We request the *full* JSON (`-ojf`) so each segment carries its per-token decode
probabilities. Averaging those gives a real STT confidence per segment/transcript
(previously it came back a hard-coded 0), which the store persists and the
repetition guard and downstream trust layer can lean on.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .config import Config
from .data.stt_terms import correct_terms


@dataclass
class TranscriptSegment:
    start_s: float
    end_s: float
    text: str
    confidence: float = 0.0  # mean whisper token probability for this segment


@dataclass
class Transcript:
    text: str
    segments: list[TranscriptSegment]
    language: str
    avg_confidence: float = 0.0  # token-weighted mean confidence across segments


class STTError(RuntimeError):
    pass


_ENC_FRAMES_PER_S = 50  # whisper encoder frames per second of audio
_CTX_MARGIN = 1.2        # headroom so a boundary-trimmed segment isn't starved
_CTX_MIN_FRAMES = 24     # floor so a very short segment still decodes


def _audio_ctx_for(duration_s: float, cfg: Config) -> int:
    frames = int(duration_s * _ENC_FRAMES_PER_S * _CTX_MARGIN) + _CTX_MIN_FRAMES
    return max(cfg.whisper_audio_ctx_min, min(cfg.whisper_audio_ctx_max, frames))


def _wav_duration_s(wav_path: Path) -> float:
    import wave

    with wave.open(str(wav_path), "rb") as w:
        return w.getnframes() / float(w.getframerate() or 1)


# whisper's special/control tokens ([_BEG_], [_EOT_], [_TT_355], ...) are wrapped
# in [_ ... _] and carry a decode probability that isn't a transcription-quality
# signal, so they're skipped when averaging token confidence.
_SPECIAL_TOKEN = re.compile(r"^\[_.*_\]$")


def _segment_confidence(tokens: list[dict]) -> tuple[float, int]:
    """Mean token probability over real (non-special) tokens, and their count.
    Returns (0.0, 0) when the JSON has no usable per-token probabilities — e.g.
    an older whisper build, or output produced without -ojf."""
    probs = [
        t["p"]
        for t in tokens
        if isinstance(t.get("p"), (int, float))
        and not _SPECIAL_TOKEN.match((t.get("text") or "").strip())
    ]
    if not probs:
        return 0.0, 0
    return sum(probs) / len(probs), len(probs)


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
        "-ojf",           # ...with per-token probabilities (for confidence)
        "-of", str(out_base),
    ]
    if cfg.whisper_dynamic_audio_ctx:
        cmd += ["-ac", str(_audio_ctx_for(_wav_duration_s(wav_path), cfg))]
    if cfg.whisper_fast_decode:
        cmd += ["-bs", "1", "-bo", "1", "-nf"]
        # -mc 0 caps the repetition loop but also drops the --prompt tokens, so
        # keep a small bounded context when a vocabulary prompt is configured.
        cmd += ["-mc", str(cfg.whisper_prompt_max_ctx) if cfg.whisper_prompt else "0"]
    if cfg.whisper_prompt:
        cmd += ["--prompt", cfg.whisper_prompt]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise STTError(f"whisper-cli failed ({proc.returncode}): {proc.stderr.strip()}")
    try:
        data = json.loads(json_path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise STTError(f"could not read whisper JSON at {json_path}: {e}") from e

    segments: list[TranscriptSegment] = []
    conf_sum = 0.0  # token-weighted, so long segments count proportionally
    conf_n = 0
    for seg in data.get("transcription", []):
        # correct known STT word mis-hearings (e.g. "Pies"->"Highs") so stored
        # transcripts and downstream extraction both see the right terms
        text = correct_terms(seg.get("text", "").strip())
        if not text:
            continue
        conf, ntok = _segment_confidence(seg.get("tokens", []))
        conf_sum += conf * ntok
        conf_n += ntok
        off = seg.get("offsets", {})
        segments.append(
            TranscriptSegment(
                start_s=round(off.get("from", 0) / 1000.0, 3),
                end_s=round(off.get("to", 0) / 1000.0, 3),
                text=text,
                confidence=round(conf, 4),
            )
        )
    full_text = " ".join(s.text for s in segments).strip()
    language = data.get("result", {}).get("language", "en")
    avg_confidence = round(conf_sum / conf_n, 4) if conf_n else 0.0
    return Transcript(
        text=full_text, segments=segments, language=language,
        avg_confidence=avg_confidence,
    )


def transcribe_samples(samples: np.ndarray, cfg: Config) -> Transcript:
    """Transcribe an int16 mono numpy segment by staging it to a temp WAV."""
    from .capture import write_wav  # local import avoids a circular import at load

    if cfg.stt_enhance:
        from .enhance import enhance  # optional mild DSP pre-clean (off by default)

        samples = enhance(samples, cfg)
    with tempfile.TemporaryDirectory(prefix="wxparser-stt-") as tmp:
        wav_path = Path(tmp) / "segment.wav"
        write_wav(wav_path, samples, cfg)
        return transcribe(wav_path, cfg)


# whisper hallucinates these stock phrases on non-speech audio (silence, music,
# tones between announcements) — they never appear in an NWR broadcast, so a
# transcript that is *only* one of them is treated as blank and dropped.
_HALLUCINATIONS = {
    "i hate it", "thank you", "thanks for watching", "please subscribe",
    "subscribe", "bye", "you",
}
# whisper's literal non-speech markers (matched whole, plus any "[blank..."
# prefix in is_blank) — kept beside _HALLUCINATIONS so all known junk
# transcripts live in one place.
_NON_SPEECH_MARKERS = {"[blank_audio]", "(dramatic music)"}

# Repetition-loop defaults. The greedy decoder occasionally wedges into a
# degenerate loop on noisy/near-silent audio and emits a token or short phrase
# hundreds of times ("Michigan, Michigan, ...", "It was It was ...", "the the
# the ..."). Such a transcript is pure garbage: dropping it here (as non-speech)
# keeps it out of both the stored transcript stream AND the product classifier,
# which was mislabelling ~1-2% of these as current_conditions / zone_forecast and
# feeding them to extraction. Overridable via config (WX_REP_*).
_REP_MAX_RUN = 6        # >= this many identical tokens in a row -> loop
_REP_MIN_WORDS = 12     # only apply the diversity test to longer transcripts
_REP_UNIQUE_RATIO = 0.35  # unique/total below this on a long transcript -> loop


def is_repetitive(
    text: str,
    *,
    max_run: int = _REP_MAX_RUN,
    min_words: int = _REP_MIN_WORDS,
    unique_ratio: float = _REP_UNIQUE_RATIO,
) -> bool:
    """True for a degenerate decoder repetition loop. Two signals, either fires:
    a long run of one identical token (single-word loop), or very low lexical
    diversity over a long transcript (short-phrase loop). Real NWR narration —
    even templated multi-day forecasts — stays well clear of both."""
    words = text.split()
    n = len(words)
    if n < 3:
        return False
    low = [w.lower() for w in words]
    run = best = 1
    for i in range(1, n):
        if low[i] == low[i - 1]:
            run += 1
            best = max(best, run)
        else:
            run = 1
    if best >= max_run:
        return True
    return n >= min_words and (len(set(low)) / n) < unique_ratio


def is_blank(transcript: Transcript, cfg: Config | None = None) -> bool:
    """True for non-speech windows: whisper's '[BLANK_AUDIO]', a punctuation-only
    transcript, a lone known hallucination phrase, or a decoder repetition loop."""
    t = transcript.text.strip().lower()
    if not t:
        return True
    if t in _NON_SPEECH_MARKERS or t.startswith("[blank"):
        return True
    core = re.sub(r"[^a-z0-9 ]", "", t).strip()
    if not core or core in _HALLUCINATIONS:
        return True
    if cfg is not None:
        return is_repetitive(
            transcript.text,
            max_run=cfg.repetition_max_run,
            min_words=cfg.repetition_min_words,
            unique_ratio=cfg.repetition_unique_ratio,
        )
    return is_repetitive(transcript.text)
