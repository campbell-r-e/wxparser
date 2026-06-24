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


def _run():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("all SAME tests passed")


if __name__ == "__main__":
    _run()
