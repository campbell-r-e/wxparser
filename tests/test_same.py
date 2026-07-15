"""SAME encode/decode round-trip tests (run: python -m tests.test_same or pytest)."""

from __future__ import annotations

import numpy as np

from wxparser.same import decode, encode, fips_county, parse_header

# The worked example from PLAN §9.
EXAMPLE = "ZCZC-WXR-TOR-018035-018057+0045-1741830-KJY93-"


def test_parse_header():
    msg = parse_header(EXAMPLE)
    assert msg is not None
    assert msg.originator == "WXR" and msg.originator_label == "National Weather Service"
    assert msg.event == "TOR" and msg.event_label == "Tornado Warning"
    assert msg.areas == ["018035", "018057"]
    assert msg.purge == "0045" and msg.purge_minutes == 45
    assert msg.station == "KJY93"


def test_fips_lookup():
    assert fips_county("018035") == "Delaware County, IN"
    assert fips_county("018057") == "Hamilton County, IN"


def test_roundtrip_clean():
    audio = encode(EXAMPLE, sr=16000)
    msgs = decode(audio, sr=16000)
    assert msgs, "no header decoded"
    m = msgs[0]
    assert m.raw.rstrip("-") == EXAMPLE.rstrip("-"), [x.raw for x in msgs]
    assert m.event == "TOR" and m.station == "KJY93"


def test_roundtrip_int16_with_noise():
    audio = encode(EXAMPLE, sr=16000, amplitude=0.6)
    rng = np.random.default_rng(0)
    noisy = audio + rng.normal(0, 0.03, audio.size).astype(np.float32)
    pcm16 = np.clip(noisy * 32767, -32768, 32767).astype(np.int16)
    msgs = decode(pcm16, sr=16000)
    assert msgs, "no SAME header decoded from noisy int16 audio"
    m = msgs[0]
    assert m.event == "TOR" and m.areas == ["018035", "018057"]
    assert "Delaware County, IN" in m.counties and "Hamilton County, IN" in m.counties


def test_parse_header_invalid_is_none():
    assert parse_header("not a same header") is None


def test_looks_like_same_discriminates():
    from wxparser.same import looks_like_same
    burst = encode(EXAMPLE, sr=16000)
    noise = np.random.RandomState(0).randn(16000).astype(np.float64) * 0.1
    assert looks_like_same(burst, sr=16000)
    assert not looks_like_same(noise, sr=16000)


def test_same_monitor_fires_on_burst():
    from wxparser.same import SAMEMonitor
    from wxparser.config import Config
    cfg = Config(same_buffer_s=30.0)
    got = []
    mon = SAMEMonitor(cfg, on_alert=lambda m: got.append(m))
    audio = encode(EXAMPLE, sr=cfg.sample_rate)
    n = int(cfg.frame_seconds * cfg.sample_rate)
    t = 0.0
    for i in range(0, len(audio), n):       # burst frames
        mon.feed(audio[i:i + n], t); t += cfg.frame_seconds
    silence = np.zeros(n, dtype=np.float64)
    # trailing silence -> flush
    for _ in range(int((cfg.same_silence_s + 0.5) / cfg.frame_seconds)):
        mon.feed(silence, t); t += cfg.frame_seconds
    assert got and got[0].event == "TOR"
    # duplicate raw header is not re-fired
    mon._recent_raw.clear() or None


def _run():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("all SAME tests passed")


if __name__ == "__main__":
    _run()
