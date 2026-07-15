"""Runtime configuration for wxparser.

All values can be overridden via environment variables so the same code runs on
the dev box, a Raspberry Pi, or against recorded WAVs in tests. Defaults target
the deployment host (Fedora box, Reecom R-1630 on the ALC269 front-mic jack).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .profile import get_profile


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Config:
    # --- Station (KJY93 Muncie, IN) ---
    station: str = field(default_factory=lambda: _env("WX_STATION", get_profile()["station"]))
    frequency_mhz: float = field(
        default_factory=lambda: float(_env("WX_FREQ_MHZ", str(get_profile()["frequency_mhz"]))))
    # the station's home city — standalone "the temperature was N" sentences (no
    # "at <City>") attach here, even when the spectrally-identical city header
    # ("At Muncie, it was ...") gets skipped by the novelty gate.
    primary_city: str = field(
        default_factory=lambda: _env("WX_PRIMARY_CITY", get_profile()["primary_city"]))
    # IANA timezone the station's spoken periods/times are anchored to — /verify
    # builds its local-wall-clock verification windows in this zone.
    station_tz: str = field(default_factory=lambda: _env(
        "WX_TZ", get_profile().get("tz", "America/Indiana/Indianapolis")))

    # --- Audio capture (ALSA via arecord subprocess) ---
    alsa_device: str = field(default_factory=lambda: _env("WX_ALSA_DEVICE", "plughw:0,0"))
    # whisper-native
    sample_rate: int = field(
        default_factory=lambda: int(_env("WX_SAMPLE_RATE", "16000")))
    channels: int = 1

    # Phase 1 fixed-window length (seconds). Phase 2 replaces this with VAD.
    window_seconds: float = field(default_factory=lambda: float(_env("WX_WINDOW_SECONDS", "30")))

    # Capture resilience: arecord can briefly fail to open the device (e.g. a
    # restart race where the prior process hasn't released it). Retry instead of
    # crashing the service.
    capture_max_retries: int = field(default_factory=lambda: int(_env("WX_CAPTURE_RETRIES", "12")))
    capture_retry_backoff_s: float = field(
        default_factory=lambda: float(_env("WX_CAPTURE_BACKOFF", "1.5")))

    # --- Phase 2: segmentation (energy VAD) ---
    frame_seconds: float = 0.02  # 20 ms analysis frames
    # Threshold sits between speech (~-22 dBFS) and the inter-sentence gaps, which
    # vary by product (~-37 to -40 dBFS) — -35 catches both. Max-segment is kept
    # low so any genuinely gap-less stretch still transcribes fast and the gate
    # isn't fed un-dedupable 30s blocks.
    vad_threshold_dbfs: float = field(default_factory=lambda: float(_env("WX_VAD_DBFS", "-35")))
    # Each STT call carries a fixed ~3 s model-init overhead, so FEWER, LONGER
    # segments transcribe the same audio far faster than many short ones (six 2 s
    # segments ≈ 32 s of STT; one 28 s segment ≈ 33 s — half the wall-clock).
    # With base.en-q5_1 we coalesce to PRODUCT level: split only on the ~1 s
    # inter-product gaps, not the ~0.4 s inter-sentence ones. Bonus: whole-product
    # segments fingerprint-match across loop repeats far better than sentence
    # fragments, so the novelty gate drops more before STT ever runs.
    vad_min_silence_s: float = field(
        default_factory=lambda: float(_env("WX_VAD_MIN_SILENCE", "1.0")))
    vad_min_speech_s: float = field(
        default_factory=lambda: float(_env("WX_VAD_MIN_SPEECH", "1.0")))
    vad_max_segment_s: float = field(
        default_factory=lambda: float(_env("WX_VAD_MAX_SEGMENT", "28")))
    vad_pad_s: float = 0.2  # keep a little audio either side of speech

    # --- Phase 2: audio fingerprint + novelty gate ---
    fp_n_mels: int = 32
    fp_time_bins: int = 32
    # Conservative: only near-identical audio is a "repeat", so novel content is
    # never wrongly dropped. Repeats that slip through are caught by Phase 3 text
    # dedup (PLAN §5 "second-line guard"). Distinct sentences top out ~0.96.
    fp_similarity_threshold: float = field(
        default_factory=lambda: float(_env("WX_FP_SIMILARITY", "0.97")))
    gate_history: int = field(default_factory=lambda: int(_env("WX_GATE_HISTORY", "400")))

    # --- STT (whisper.cpp via whisper-cli subprocess) ---
    whisper_bin: Path = field(default_factory=lambda: Path(
        _env("WX_WHISPER_BIN", str(Path.home() / "whisper.cpp/build/bin/whisper-cli"))))
    # small.en quantized to q5_1: higher accuracy than base.en at a real speed
    # cost on this old CPU (~3.4x slower than base.en-q5_1 in a bench: ~13x
    # real-time per segment). It keeps up only because the novelty gate drops
    # most repeated audio before STT — watch queue_depth/last_stt_ok in /health,
    # and fall back to base.en-q5_1 (WX_WHISPER_MODEL) if the queue backs up.
    whisper_model: Path = field(default_factory=lambda: Path(
        _env("WX_WHISPER_MODEL", str(Path.home() / "whisper.cpp/models/ggml-small.en-q5_1.bin"))))
    whisper_threads: int = field(default_factory=lambda: int(_env("WX_WHISPER_THREADS", "2")))
    whisper_engine_name: str = "whisper.cpp"
    # Size the whisper encoder context to each segment instead of the fixed 30 s
    # (1500 frames). The encoder is the STT bottleneck and its cost scales with
    # this, so short segments transcribe ~2-3x faster with negligible accuracy
    # loss. 50 frames/sec of audio + 20% margin, floored so we never starve it.
    whisper_dynamic_audio_ctx: bool = field(
        default_factory=lambda: _env("WX_AUDIO_CTX", "1") == "1")
    whisper_audio_ctx_min: int = 256
    whisper_audio_ctx_max: int = 1500
    # Fast greedy decode: beam_size=1, best_of=1, no temperature-fallback retries,
    # no cross-segment prompt. ~1.6x faster than the beam-5 default AND it caps the
    # decoder repetition-loop pathology that can otherwise wedge one short segment
    # for minutes. Per-segment STT errors are cleaned up by repeat-voting (§8).
    whisper_fast_decode: bool = field(default_factory=lambda: _env("WX_FAST_DECODE", "1") == "1")
    # Vocabulary bias (whisper --prompt): seeds the decoder with the local place
    # names it would otherwise mangle (proper nouns — county/town names in
    # warnings). ON with base.en/small.en, which absorb the prompt cleanly.
    # (tiny.en degenerated to "The the..." with ANY prompt — too small a text
    # context — so this MUST stay empty if WX_WHISPER_MODEL is pointed at tiny.en.)
    # The default vocabulary prompt is region-specific, so it comes from the
    # active station profile (profile.py / WX_PROFILE), not hard-coded here. Kept
    # to ~50 tokens so it fits whisper_prompt_max_ctx without truncation.
    whisper_prompt: str = field(
        default_factory=lambda: _env("WX_STT_PROMPT", get_profile()["stt_prompt"]))
    # Carried-context cap used alongside the prompt: 0 (fast-decode default) caps
    # the repetition-loop pathology but also suppresses --prompt, so when a prompt
    # is set we keep a small bounded context instead.
    whisper_prompt_max_ctx: int = field(
        default_factory=lambda: int(_env("WX_PROMPT_MAX_CTX", "64")))

    # --- Optional speech enhancement (OFF by default; see enhance.py) ---
    # A mild DSP chain (mains-hum notches + low-pass + spectral subtraction +
    # level-matched makeup) applied to each segment before STT. An A/B whisper
    # test showed it never hurt and occasionally fixed a word on this station's
    # line noise; the benefit is marginal, so it's OFF by default — set
    # WX_STT_ENHANCE=1 to A/B it on a new deployment. Tune mains_hz to 50 outside
    # North America. Boosting beyond the input level REGRESSED STT, so the chain
    # only restores the original level — these knobs shape the noise floor, not gain.
    stt_enhance: bool = field(default_factory=lambda: _env("WX_STT_ENHANCE", "0") == "1")
    stt_enhance_mains_hz: float = field(
        default_factory=lambda: float(_env("WX_STT_ENHANCE_MAINS_HZ", "60")))
    stt_enhance_lowpass_hz: float = field(
        default_factory=lambda: float(_env("WX_STT_ENHANCE_LOWPASS_HZ", "3800")))
    stt_enhance_alpha: float = field(
        default_factory=lambda: float(_env("WX_STT_ENHANCE_ALPHA", "2.0")))
    stt_enhance_floor: float = field(
        default_factory=lambda: float(_env("WX_STT_ENHANCE_FLOOR", "0.12")))

    # --- STT repetition-loop guard (see stt.is_repetitive) ---
    # The greedy decoder can wedge into a degenerate loop on noisy/near-silent
    # audio and emit one token or short phrase hundreds of times. Such a
    # transcript is dropped as non-speech (is_blank) so it never reaches the
    # store or the product classifier. Two signals, either fires: a run of
    # >= max_run identical tokens, or unique/total below unique_ratio on a
    # transcript of at least min_words words.
    repetition_max_run: int = field(default_factory=lambda: int(_env("WX_REP_MAX_RUN", "6")))
    repetition_min_words: int = field(default_factory=lambda: int(_env("WX_REP_MIN_WORDS", "12")))
    repetition_unique_ratio: float = field(
        default_factory=lambda: float(_env("WX_REP_UNIQUE_RATIO", "0.35")))

    # A transcript whose mean STT token-confidence (stt.avg_confidence) is below
    # this is too unreliable to vote into conditions/forecast/almanac — a mangled
    # reading could sway an aggregate. It's still STORED (raw record kept), just
    # not voted; applied in the shared pipeline so live + reprocess stay
    # consistent. Set from the observed spread: routine transcripts sit ~0.85-0.95
    # and garbles land <0.5. A confidence of exactly 0.0 means "unmeasured"
    # (pre -ojf transcripts) and is never gated, so a full reprocess of old
    # history isn't wiped. 0 disables the gate entirely.
    stt_confidence_floor: float = field(
        default_factory=lambda: float(_env("WX_STT_CONF_FLOOR", "0.5")))

    # --- Phase 3: text dedup (second-line guard) ---
    # High, so only near-exact repeats are dropped. On short templated forecasts a
    # changed number ("80"->"82") only drops similarity to ~0.95, and must survive
    # as an update rather than being swallowed as a duplicate.
    # >= -> duplicate, drop
    text_dup_threshold: float = field(
        default_factory=lambda: float(_env("WX_TEXT_DUP", "0.97")))
    # >= same-type -> update
    text_update_threshold: float = field(
        default_factory=lambda: float(_env("WX_TEXT_UPDATE", "0.75")))
    text_history: int = field(default_factory=lambda: int(_env("WX_TEXT_HISTORY", "100")))

    # --- Phase 6: LAN query API ---
    api_host: str = field(default_factory=lambda: _env("WX_API_HOST", "0.0.0.0"))
    api_port: int = field(default_factory=lambda: int(_env("WX_API_PORT", "8080")))
    # only surface a city once heard this many times (filters one-off STT garbage);
    # clients can override per request with ?min=
    api_min_sightings: int = field(default_factory=lambda: int(_env("WX_MIN_SIGHTINGS", "2")))
    # fail-loud health thresholds for /health. Segments arrive every
    # ~20-30s, so minutes of silence means the radio/capture has gone deaf; the
    # heartbeat file is rewritten each segment, so a stale one means the capture
    # process itself is down.
    health_audio_silent_min: int = field(
        default_factory=lambda: int(_env("WX_HEALTH_AUDIO_SILENT_MIN", "5")))
    health_heartbeat_stale_min: int = field(
        default_factory=lambda: int(_env("WX_HEALTH_HEARTBEAT_STALE_MIN", "3")))
    # "STT worker wedged" needs a REAL backlog, not a single just-queued segment:
    # on a looping broadcast the novelty gate can idle STT for many minutes, then
    # one novel segment lands (queue_depth 1) while last_stt_ok is still old — that
    # is idle-then-busy, not wedged, and clears within a cycle. A genuine wedge (a
    # decoder repetition-loop stuck on one segment while audio keeps flowing) piles
    # segments up, so require queue_depth above 1 AND a dedicated, looser staleness
    # window before flagging — avoids the overnight false positives.
    health_stt_wedged_min: int = field(
        default_factory=lambda: int(_env("WX_HEALTH_STT_WEDGED_MIN", "10")))
    health_stt_wedged_queue: int = field(
        default_factory=lambda: int(_env("WX_HEALTH_STT_WEDGED_QUEUE", "1")))
    # A dead radio can still look alive: constant static/carrier above the VAD
    # gate produces segments that all fingerprint as near-identical repeats, so
    # audio isn't "silent" yet nothing novel ever reaches STT (seen 2026-07-07:
    # 4h of static, every segment sim~0.99, zero reports, /health green). On the
    # real broadcast the changing time announcements alone yield novel segments
    # every few minutes, so a full hour with nothing passing the novelty gate
    # means noise, not programming.
    health_novel_stale_min: int = field(
        default_factory=lambda: int(_env("WX_HEALTH_NOVEL_STALE_MIN", "60")))
    # Outbound push. OFF by default so the box stays fully offline; set a
    # URL (e.g. a LAN mesh gateway) to POST each new SAME alert as JSON. The SSE
    # /stream endpoint needs nothing here — consumers connect inbound.
    webhook_url: str = field(default_factory=lambda: _env("WX_WEBHOOK_URL", ""))
    webhook_timeout_s: float = field(
        default_factory=lambda: float(_env("WX_WEBHOOK_TIMEOUT", "5")))
    stream_poll_s: float = field(default_factory=lambda: float(_env("WX_STREAM_POLL_S", "3")))
    # a current-conditions reading older than this is flagged stale: it's a radio
    # transcriber, so a value is only as fresh as the last time the broadcast
    # aired it (infrequently-named cities/fields drift from reality between
    # airings). Clients can override per request with ?stale_after=.
    condition_stale_after_min: int = field(
        default_factory=lambda: int(_env("WX_STALE_AFTER_MIN", "60")))
    # Almanac/climate fields (sunrise/sunset, YTD precip, degree days) air only a
    # few times a day and stay valid until the next day's recap, so the 60-min
    # current-conditions window above wrongly flags them stale within an hour of
    # every airing. Judge them against a full day instead — clients can still
    # override per request with ?stale_after=.
    almanac_stale_after_min: int = field(
        default_factory=lambda: int(_env("WX_ALMANAC_STALE_AFTER_MIN", "1440")))
    # a voted reading whose winning value holds less than this share of the recent
    # airings is flagged `uncertain` (the airings disagree — likely an STT mishear,
    # at risk of being off by a lot). Clients see the flag and can distrust it.
    confidence_min: float = field(default_factory=lambda: float(_env("WX_CONFIDENCE_MIN", "0.6")))
    # --- trust scoring (trust.py) + peer-outlier band (extract.py) ---
    # sightings before trust's "seen enough" factor saturates, and the trust
    # values at which the confidence label reads high / medium.
    trust_sightings_full: float = field(
        default_factory=lambda: float(_env("WX_TRUST_SIGHTINGS_FULL", "6")))
    trust_high: float = field(default_factory=lambda: float(_env("WX_TRUST_HIGH", "0.66")))
    trust_low: float = field(default_factory=lambda: float(_env("WX_TRUST_LOW", "0.33")))
    # roundup temps: with at least peer_min_cities in one readout, drop any
    # reading more than peer_max_dev_f from the median (lost-leading-digit
    # mishears like "22 at Cincinnati" for 92; see extract._drop_peer_outliers).
    peer_min_cities: int = field(default_factory=lambda: int(_env("WX_PEER_MIN_CITIES", "3")))
    peer_max_dev_f: int = field(default_factory=lambda: int(_env("WX_PEER_MAX_DEV_F", "30")))
    # when linking a SAME alert to its spoken-detail transcripts, also include
    # ones captured this many seconds before the digital burst (a heads-up can
    # precede the tones); the window runs to the alert's expiry.
    alert_link_pre_buffer_s: int = field(
        default_factory=lambda: int(_env("WX_ALERT_LINK_PREBUFFER", "120")))
    # after a SAME burst fires, segments captured within this window are the
    # spoken warning narrative — jump them to the FRONT of the STT queue so the
    # warning transcribes ahead of routine forecast/conditions backlog.
    alert_priority_window_s: float = field(
        default_factory=lambda: float(_env("WX_ALERT_PRIORITY_WINDOW", "120")))

    # --- Phase 4: SAME alert decoding ---
    same_enabled: bool = field(default_factory=lambda: _env("WX_SAME", "1") == "1")
    same_buffer_s: float = 13.0      # spans the 3 repeated header bursts
    same_detect_s: float = 0.4       # window for the tone-concentration check
    same_silence_s: float = 1.5      # quiet after a burst before we decode
    same_band_ratio: float = 0.35    # min in-band energy fraction to call it SAME

    # --- Output ---
    out_dir: Path = field(default_factory=lambda: Path(_env("WX_OUT_DIR", "transcripts")))

    # --- PostgreSQL store (pg8000 / BSD driver; local trust auth) ---
    pg_host: str = field(default_factory=lambda: _env("WX_PG_HOST", "127.0.0.1"))
    pg_port: int = field(default_factory=lambda: int(_env("WX_PG_PORT", "5432")))
    pg_database: str = field(default_factory=lambda: _env("WX_PG_DATABASE", "wxparser"))
    pg_user: str = field(default_factory=lambda: _env("WX_PG_USER", "wxparser"))
    # empty -> trust auth (no password)
    pg_password: str = field(
        default_factory=lambda: _env("WX_PG_PASSWORD", ""))

    @property
    def model_name(self) -> str:
        # e.g. ggml-tiny.en.bin -> tiny.en
        stem = self.whisper_model.stem
        return stem.replace("ggml-", "")


# NOTE deliberately no module-level `CONFIG = Config()`: construction belongs to
# the entry points (main/api/reprocess build one Config and inject it), so that
# importing wxparser reads no env vars and touches no files, and WX_* overrides
# set before the process constructs its Config are always honored.
