"""stt.py: whisper-cli wrapper with the subprocess mocked."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import numpy as np

import wxparser.stt as stt
from wxparser.config import Config
from wxparser.stt import (
    TranscriptSegment, Transcript, _audio_ctx_for, is_blank, is_repetitive,
)


def test_audio_ctx_clamped():
    cfg = Config()
    assert _audio_ctx_for(0.0, cfg) >= cfg.whisper_audio_ctx_min
    assert _audio_ctx_for(10_000, cfg) == cfg.whisper_audio_ctx_max


def test_is_blank():
    def t(s):
        return Transcript(text=s, segments=[], language="en")
    assert is_blank(t(""))
    assert is_blank(t("[BLANK_AUDIO]"))
    assert is_blank(t("(dramatic music)"))
    assert is_blank(t("I hate it."))            # whisper hallucination
    assert is_blank(t("Thank you"))
    assert is_blank(t("you"))
    assert is_blank(t(","))                     # punctuation-only noise
    assert is_blank(t("Michigan, " * 40))       # single-token repetition loop
    assert is_blank(t("It was " * 30))          # short-phrase repetition loop
    assert not is_blank(t("Highs around 80"))
    assert not is_blank(t("At Muncie it was 78 degrees"))


def test_is_repetitive():
    # single-token run (>= max_run identical in a row)
    assert is_repetitive("the the the the the the the")
    assert is_repetitive("Fortnite, " * 20)
    # short-phrase loop caught by the lexical-diversity signal
    assert is_repetitive("It was " * 25)
    assert is_repetitive("the morning, " * 20)
    # real narration is never flagged
    assert not is_repetitive("A chance of showers and thunderstorms late in the afternoon.")
    assert not is_repetitive(
        "Rest of today sunny with a high near 95. Tonight partly cloudy with a "
        "low near 71. Friday mostly sunny. Saturday cooler with a high near 78."
    )
    assert not is_repetitive("At Muncie it was 92 degrees and sunny, wind southwest at 8.")
    # too short to judge -> never repetitive
    assert not is_repetitive("high near 80")
    assert not is_repetitive("Muncie")          # under the 3-word floor
    assert not is_repetitive("you you")


def test_transcribe_parses_token_confidence(monkeypatch):
    # -ojf gives per-token probabilities; special tokens ([_...]) are skipped and
    # avg_confidence is the token-weighted mean of the real tokens.
    payload = {"transcription": [
        {"text": " Highs around 80.", "offsets": {"from": 0, "to": 2000},
         "tokens": [
             {"text": "[_BEG_]", "p": 0.99},          # special -> ignored
             {"text": " Highs", "p": 0.8},
             {"text": " around", "p": 0.9},
             {"text": " 80.", "p": 0.7},
         ]},
    ], "result": {"language": "en"}}
    monkeypatch.setattr(stt.subprocess, "run", _fake_run(payload))
    t = stt.transcribe_samples(np.zeros(16000, dtype=np.int16), Config())
    assert abs(t.avg_confidence - 0.8) < 1e-6      # mean(0.8,0.9,0.7), [_BEG_] excluded
    assert abs(t.segments[0].confidence - 0.8) < 1e-6


def test_transcribe_no_tokens_confidence_zero(monkeypatch):
    # older whisper / no -ojf: no per-token probabilities -> confidence 0.0
    payload = {"transcription": [
        {"text": " Highs around 80.", "offsets": {"from": 0, "to": 1000}},
    ], "result": {"language": "en"}}
    monkeypatch.setattr(stt.subprocess, "run", _fake_run(payload))
    t = stt.transcribe_samples(np.zeros(16000, dtype=np.int16), Config())
    assert t.avg_confidence == 0.0


class _Proc:
    def __init__(self, rc):
        self.returncode = rc
        self.stderr = "boom" if rc else ""


def _fake_run(payload, rc=0, write=True):
    def run(cmd, capture_output=True, text=True):
        if write and rc == 0:
            of = Path(cmd[cmd.index("-of") + 1])
            of.with_suffix(".json").write_text(json.dumps(payload), encoding="utf-8")
        return _Proc(rc)
    return run


def test_transcribe_parses_and_corrects(monkeypatch):
    payload = {"transcription": [
        {"text": " Pies around 80.", "offsets": {"from": 0, "to": 1000}},
        {"text": "   ", "offsets": {"from": 1000, "to": 1100}},   # blank seg -> skipped
    ], "result": {"language": "en"}}
    monkeypatch.setattr(stt.subprocess, "run", _fake_run(payload))
    t = stt.transcribe_samples(np.zeros(16000, dtype=np.int16), Config())
    assert t.text == "Highs around 80."          # "Pies"->"Highs" correction applied
    assert len(t.segments) == 1 and t.language == "en"


def test_transcribe_with_enhance_enabled(monkeypatch):
    # stt_enhance=True routes the segment through enhance.py before whisper
    payload = {"transcription": [
        {"text": " Highs around 80.", "offsets": {"from": 0, "to": 1000}},
    ], "result": {"language": "en"}}
    monkeypatch.setattr(stt.subprocess, "run", _fake_run(payload))
    cfg = dataclasses.replace(Config(), stt_enhance=True)
    samples = (3000 * np.sin(2 * np.pi * 700 * np.arange(8000) / 16000)).astype(np.int16)
    t = stt.transcribe_samples(samples, cfg)
    assert t.text == "Highs around 80."


def test_transcribe_nonzero_exit_raises(monkeypatch):
    monkeypatch.setattr(stt.subprocess, "run", _fake_run({}, rc=1, write=False))
    try:
        stt.transcribe_samples(np.zeros(16000, dtype=np.int16), Config())
        assert False, "expected STTError"
    except stt.STTError as e:
        assert "whisper-cli failed" in str(e)


def test_transcribe_missing_json_raises(monkeypatch):
    monkeypatch.setattr(stt.subprocess, "run", _fake_run({}, rc=0, write=False))
    try:
        stt.transcribe_samples(np.zeros(16000, dtype=np.int16), Config())
        assert False, "expected STTError"
    except stt.STTError as e:
        assert "could not read whisper JSON" in str(e)
