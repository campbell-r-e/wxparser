"""stt.py: whisper-cli wrapper with the subprocess mocked."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

import wxparser.stt as stt
from wxparser.config import Config
from wxparser.stt import Segment, Transcript, _audio_ctx_for, is_blank


def test_audio_ctx_clamped():
    cfg = Config()
    assert _audio_ctx_for(0.0, cfg) >= cfg.whisper_audio_ctx_min
    assert _audio_ctx_for(10_000, cfg) == cfg.whisper_audio_ctx_max


def test_is_blank():
    assert is_blank(Transcript(text="", segments=[], language="en"))
    assert is_blank(Transcript(text="[BLANK_AUDIO]", segments=[], language="en"))
    assert not is_blank(Transcript(text="Highs around 80", segments=[], language="en"))


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
