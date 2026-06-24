"""Runtime configuration for wxparser.

All values can be overridden via environment variables so the same code runs on
the dev box, a Raspberry Pi, or against recorded WAVs in tests. Defaults target
the deployment host (Fedora box, Reecom R-1630 on the ALC269 front-mic jack).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Config:
    # --- Station (KJY93 Muncie, IN) ---
    station: str = _env("WX_STATION", "KJY93")
    frequency_mhz: float = float(_env("WX_FREQ_MHZ", "162.425"))

    # --- Audio capture (ALSA via arecord subprocess) ---
    alsa_device: str = _env("WX_ALSA_DEVICE", "plughw:0,0")
    sample_rate: int = int(_env("WX_SAMPLE_RATE", "16000"))  # whisper-native
    channels: int = 1

    # Phase 1 fixed-window length (seconds). Phase 2 replaces this with VAD.
    window_seconds: float = float(_env("WX_WINDOW_SECONDS", "30"))

    # --- STT (whisper.cpp via whisper-cli subprocess) ---
    whisper_bin: Path = Path(
        _env("WX_WHISPER_BIN", str(Path.home() / "whisper.cpp/build/bin/whisper-cli"))
    )
    whisper_model: Path = Path(
        _env("WX_WHISPER_MODEL", str(Path.home() / "whisper.cpp/models/ggml-tiny.en.bin"))
    )
    whisper_threads: int = int(_env("WX_WHISPER_THREADS", "2"))
    whisper_engine_name: str = "whisper.cpp"

    # --- Output ---
    out_dir: Path = Path(_env("WX_OUT_DIR", "transcripts"))

    @property
    def reports_jsonl(self) -> Path:
        return self.out_dir / "reports.jsonl"

    @property
    def model_name(self) -> str:
        # e.g. ggml-tiny.en.bin -> tiny.en
        stem = self.whisper_model.stem
        return stem.replace("ggml-", "")


CONFIG = Config()
