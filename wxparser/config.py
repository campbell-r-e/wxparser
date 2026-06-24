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
    # the station's home city — standalone "the temperature was N" sentences (no
    # "at <City>") attach here, even when the spectrally-identical city header
    # ("At Muncie, it was ...") gets skipped by the novelty gate.
    primary_city: str = _env("WX_PRIMARY_CITY", "Muncie")

    # --- Audio capture (ALSA via arecord subprocess) ---
    alsa_device: str = _env("WX_ALSA_DEVICE", "plughw:0,0")
    sample_rate: int = int(_env("WX_SAMPLE_RATE", "16000"))  # whisper-native
    channels: int = 1

    # Phase 1 fixed-window length (seconds). Phase 2 replaces this with VAD.
    window_seconds: float = float(_env("WX_WINDOW_SECONDS", "30"))

    # Capture resilience: arecord can briefly fail to open the device (e.g. a
    # restart race where the prior process hasn't released it). Retry instead of
    # crashing the service.
    capture_max_retries: int = int(_env("WX_CAPTURE_RETRIES", "12"))
    capture_retry_backoff_s: float = float(_env("WX_CAPTURE_BACKOFF", "1.5"))

    # --- Phase 2: segmentation (energy VAD) ---
    frame_seconds: float = 0.02  # 20 ms analysis frames
    vad_threshold_dbfs: float = float(_env("WX_VAD_DBFS", "-40"))
    vad_min_silence_s: float = float(_env("WX_VAD_MIN_SILENCE", "0.5"))
    vad_min_speech_s: float = float(_env("WX_VAD_MIN_SPEECH", "1.0"))
    vad_max_segment_s: float = float(_env("WX_VAD_MAX_SEGMENT", "30"))
    vad_pad_s: float = 0.2  # keep a little audio either side of speech

    # --- Phase 2: audio fingerprint + novelty gate ---
    fp_n_mels: int = 32
    fp_time_bins: int = 32
    # Conservative: only near-identical audio is a "repeat", so novel content is
    # never wrongly dropped. Repeats that slip through are caught by Phase 3 text
    # dedup (PLAN §5 "second-line guard"). Distinct sentences top out ~0.96.
    fp_similarity_threshold: float = float(_env("WX_FP_SIMILARITY", "0.97"))
    gate_history: int = int(_env("WX_GATE_HISTORY", "400"))

    # --- STT (whisper.cpp via whisper-cli subprocess) ---
    whisper_bin: Path = Path(
        _env("WX_WHISPER_BIN", str(Path.home() / "whisper.cpp/build/bin/whisper-cli"))
    )
    whisper_model: Path = Path(
        _env("WX_WHISPER_MODEL", str(Path.home() / "whisper.cpp/models/ggml-tiny.en.bin"))
    )
    whisper_threads: int = int(_env("WX_WHISPER_THREADS", "2"))
    whisper_engine_name: str = "whisper.cpp"
    # Size the whisper encoder context to each segment instead of the fixed 30 s
    # (1500 frames). The encoder is the STT bottleneck and its cost scales with
    # this, so short segments transcribe ~2-3x faster with negligible accuracy
    # loss. 50 frames/sec of audio + 20% margin, floored so we never starve it.
    whisper_dynamic_audio_ctx: bool = _env("WX_AUDIO_CTX", "1") == "1"
    whisper_audio_ctx_min: int = 256
    whisper_audio_ctx_max: int = 1500
    # Fast greedy decode: beam_size=1, best_of=1, no temperature-fallback retries,
    # no cross-segment prompt. ~1.6x faster than the beam-5 default AND it caps the
    # decoder repetition-loop pathology that can otherwise wedge one short segment
    # for minutes. Per-segment STT errors are cleaned up by repeat-voting (§8).
    whisper_fast_decode: bool = _env("WX_FAST_DECODE", "1") == "1"

    # --- Phase 3: text dedup (second-line guard) ---
    # High, so only near-exact repeats are dropped. On short templated forecasts a
    # changed number ("80"->"82") only drops similarity to ~0.95, and must survive
    # as an update rather than being swallowed as a duplicate.
    text_dup_threshold: float = float(_env("WX_TEXT_DUP", "0.97"))     # >= -> duplicate, drop
    text_update_threshold: float = float(_env("WX_TEXT_UPDATE", "0.75"))  # >= same-type -> update
    text_history: int = int(_env("WX_TEXT_HISTORY", "100"))

    # --- Phase 6: LAN query API ---
    api_host: str = _env("WX_API_HOST", "0.0.0.0")
    api_port: int = int(_env("WX_API_PORT", "8080"))
    # only surface a city once heard this many times (filters one-off STT garbage);
    # clients can override per request with ?min=
    api_min_sightings: int = int(_env("WX_MIN_SIGHTINGS", "2"))

    # --- Phase 4: SAME alert decoding ---
    same_enabled: bool = _env("WX_SAME", "1") == "1"
    same_buffer_s: float = 13.0      # spans the 3 repeated header bursts
    same_detect_s: float = 0.4       # window for the tone-concentration check
    same_silence_s: float = 1.5      # quiet after a burst before we decode
    same_band_ratio: float = 0.35    # min in-band energy fraction to call it SAME

    # --- Output ---
    out_dir: Path = Path(_env("WX_OUT_DIR", "transcripts"))

    # --- PostgreSQL store (pg8000 / BSD driver; local trust auth) ---
    pg_host: str = _env("WX_PG_HOST", "127.0.0.1")
    pg_port: int = int(_env("WX_PG_PORT", "5432"))
    pg_database: str = _env("WX_PG_DATABASE", "wxparser")
    pg_user: str = _env("WX_PG_USER", "wxparser")
    pg_password: str = _env("WX_PG_PASSWORD", "")  # empty -> trust auth (no password)

    @property
    def reports_jsonl(self) -> Path:
        return self.out_dir / "reports.jsonl"

    @property
    def model_name(self) -> str:
        # e.g. ggml-tiny.en.bin -> tiny.en
        stem = self.whisper_model.stem
        return stem.replace("ggml-", "")


CONFIG = Config()
