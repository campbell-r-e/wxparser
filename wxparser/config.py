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


# Local place-name vocabulary for the whisper prompt (KJY93 / east-central
# Indiana coverage). Kept to ~50 tokens so it fits whisper_prompt_max_ctx without
# truncation. Prioritizes the county + town names that appear in *warnings* (the
# regional-roundup cities are already handled by data.place_names corrections).
_DEFAULT_STT_PROMPT = (
    "NOAA Weather Radio for east central Indiana. Counties: Delaware, Madison, "
    "Henry, Randolph, Jay, Blackford, Grant, Wayne, Tipton, Hamilton, Howard. "
    "Towns: Muncie, Anderson, Marion, Portland, Winchester, New Castle, Richmond, "
    "Yorktown, Albany, Dunkirk, Alexandria, Gas City, Hartford City."
)


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
    # Threshold sits between speech (~-22 dBFS) and the inter-sentence gaps, which
    # vary by product (~-37 to -40 dBFS) — -35 catches both. Max-segment is kept
    # low so any genuinely gap-less stretch still transcribes fast and the gate
    # isn't fed un-dedupable 30s blocks.
    vad_threshold_dbfs: float = float(_env("WX_VAD_DBFS", "-35"))
    # Each STT call carries a fixed ~3 s model-init overhead, so FEWER, LONGER
    # segments transcribe the same audio far faster than many short ones (six 2 s
    # segments ≈ 32 s of STT; one 28 s segment ≈ 33 s — half the wall-clock).
    # With base.en-q5_1 we coalesce to PRODUCT level: split only on the ~1 s
    # inter-product gaps, not the ~0.4 s inter-sentence ones. Bonus: whole-product
    # segments fingerprint-match across loop repeats far better than sentence
    # fragments, so the novelty gate drops more before STT ever runs.
    vad_min_silence_s: float = float(_env("WX_VAD_MIN_SILENCE", "1.0"))
    vad_min_speech_s: float = float(_env("WX_VAD_MIN_SPEECH", "1.0"))
    vad_max_segment_s: float = float(_env("WX_VAD_MAX_SEGMENT", "28"))
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
    # base.en quantized to q5_1: full-base.en accuracy at ~tiny speed on this CPU
    # (quantization cuts the memory bandwidth this old box is bottlenecked on).
    whisper_model: Path = Path(
        _env("WX_WHISPER_MODEL", str(Path.home() / "whisper.cpp/models/ggml-base.en-q5_1.bin"))
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
    # Vocabulary bias (whisper --prompt): seeds the decoder with the local place
    # names it would otherwise mangle (proper nouns — county/town names in
    # warnings). ON with base.en, which absorbs the prompt cleanly. (tiny.en
    # degenerated to "The the..." with ANY prompt — too small a text context — so
    # this MUST stay empty if WX_WHISPER_MODEL is pointed back at tiny.en.)
    whisper_prompt: str = _env("WX_STT_PROMPT", _DEFAULT_STT_PROMPT)
    # Carried-context cap used alongside the prompt: 0 (fast-decode default) caps
    # the repetition-loop pathology but also suppresses --prompt, so when a prompt
    # is set we keep a small bounded context instead.
    whisper_prompt_max_ctx: int = int(_env("WX_PROMPT_MAX_CTX", "64"))

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
    # fail-loud health thresholds (roadmap: watchdog). Segments arrive every
    # ~20-30s, so minutes of silence means the radio/capture has gone deaf; the
    # heartbeat file is rewritten each segment, so a stale one means the capture
    # process itself is down.
    health_audio_silent_min: int = int(_env("WX_HEALTH_AUDIO_SILENT_MIN", "5"))
    health_heartbeat_stale_min: int = int(_env("WX_HEALTH_HEARTBEAT_STALE_MIN", "3"))
    # Outbound push (roadmap). OFF by default so the box stays fully offline; set a
    # URL (e.g. a LAN mesh gateway) to POST each new SAME alert as JSON. The SSE
    # /stream endpoint needs nothing here — consumers connect inbound.
    webhook_url: str = _env("WX_WEBHOOK_URL", "")
    webhook_timeout_s: float = float(_env("WX_WEBHOOK_TIMEOUT", "5"))
    stream_poll_s: float = float(_env("WX_STREAM_POLL_S", "3"))
    # a current-conditions reading older than this is flagged stale: it's a radio
    # transcriber, so a value is only as fresh as the last time the broadcast
    # aired it (infrequently-named cities/fields drift from reality between
    # airings). Clients can override per request with ?stale_after=.
    condition_stale_after_min: int = int(_env("WX_STALE_AFTER_MIN", "60"))
    # a voted reading whose winning value holds less than this share of the recent
    # airings is flagged `uncertain` (the airings disagree — likely an STT mishear,
    # at risk of being off by a lot). Clients see the flag and can distrust it.
    confidence_min: float = float(_env("WX_CONFIDENCE_MIN", "0.6"))
    # when linking a SAME alert to its spoken-detail transcripts, also include
    # ones captured this many seconds before the digital burst (a heads-up can
    # precede the tones); the window runs to the alert's expiry.
    alert_link_pre_buffer_s: int = int(_env("WX_ALERT_LINK_PREBUFFER", "120"))
    # after a SAME burst fires, segments captured within this window are the
    # spoken warning narrative — jump them to the FRONT of the STT queue so the
    # warning transcribes ahead of routine forecast/conditions backlog.
    alert_priority_window_s: float = float(_env("WX_ALERT_PRIORITY_WINDOW", "120"))

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
