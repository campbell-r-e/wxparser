"""Current-conditions extraction + repeat-voting tests."""

from __future__ import annotations

from wxparser.extract import ConditionsAggregator, extract_observation, words_to_int

# Real transcript captured from KJY93 (current-conditions product).
REAL = (
    "At Muncie, it was clear. The temperature was 61 degrees, the 2.49, and the "
    "relative humidity 64%. The wind was calm. The barometric pressure was 30.16 "
    "inches and falling."
)


def test_words_to_int():
    assert words_to_int("72") == 72
    assert words_to_int("sixty one") == 61
    assert words_to_int("forty-five") == 45
    assert words_to_int("banana") is None


def test_extract_real():
    o = extract_observation(REAL)
    assert o["temperature_f"] == 61
    assert o["humidity_pct"] == 64
    assert o["pressure_in"] == 30.16
    assert o["pressure_trend"] == "falling"
    assert o["wind"] == "calm" and o["wind_speed_mph"] == 0
    assert o["sky"] == "clear"


def test_range_check_rejects_garbage():
    o = extract_observation("The temperature was 999 degrees.")
    assert "temperature_f" not in o


def test_repeat_voting_stabilizes_numbers():
    agg = ConditionsAggregator(maxlen=10)
    # 61 heard 3x, a one-off STT slip to 67 once -> vote must land on 61
    for t in ["temperature was 61 degrees", "temperature was 61 degrees",
              "temperature was 67 degrees", "temperature was 61 degrees"]:
        agg.update(t)
    snap = agg.snapshot()
    assert snap["temperature_f"]["value"] == 61
    assert snap["temperature_f"]["votes"] == 3 and snap["temperature_f"]["total"] == 4


def _run():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("all extract tests passed")


if __name__ == "__main__":
    _run()
