"""Current-conditions extraction + repeat-voting tests."""

from __future__ import annotations

from wxparser.extract import (
    CityConditionsAggregator,
    ConditionsAggregator,
    ForecastAggregator,
    extract_alert_details,
    extract_observation,
    parse_temp_value,
    period_header,
    words_to_int,
)

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


def test_forecast_sky_does_not_pollute_conditions():
    # forecast phrasing must NOT register as a current-conditions sky reading
    assert "sky" not in extract_observation("Saturday, partly cloudy. Highs around 80.")
    # but real current-conditions framing must
    assert extract_observation("At Muncie, it was clear.")["sky"] == "clear"


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


def test_parse_temp_value():
    assert parse_temp_value("around 80") == 80
    assert parse_temp_value("in the mid 60s") == 65
    assert parse_temp_value("in the lower 80s") == 81
    assert parse_temp_value("near 75") == 75


def test_period_header():
    assert period_header("Tonight, partly cloudy.") == "Tonight"
    assert period_header("Saturday night, clear.") == "Saturday Night"
    assert period_header("Lows in the 60s.") is None


def test_forecast_aggregator_builds_periods():
    fc = ForecastAggregator()
    for seg in [
        "Tonight, partly cloudy with a chance of showers.",
        "Lows in the lower 60s.",
        "Chance of rain 70 percent.",
        "Saturday, mostly sunny.",
        "Highs around 80.",
    ]:
        fc.update(seg)
    periods = {p["period"]: p for p in fc.snapshot()}
    assert periods["Tonight"]["low_f"] == 61
    assert periods["Tonight"]["precip_pct"] == 70
    assert periods["Saturday"]["high_f"] == 80
    assert periods["Saturday"]["sky"] == "mostly sunny"
    # a high mentioned under Tonight must not leak into Saturday and vice-versa
    assert "high_f" not in periods["Tonight"]


def test_prime_conditions_from_snapshot():
    agg = ConditionsAggregator()
    agg.prime({"temperature_f": {"value": 59}, "humidity_pct": {"value": 67}})
    snap = agg.snapshot()
    assert snap["temperature_f"]["value"] == 59 and snap["humidity_pct"]["value"] == 67
    # a fresh live reading still votes on top
    agg.update("The temperature was 60 degrees.")
    assert agg.snapshot()["temperature_f"]["value"] in (59, 60)


def test_prime_forecast_from_periods():
    fc = ForecastAggregator()
    fc.prime([{"period": "Tonight", "low_f": 61, "precip_pct": 70, "sky": "partly cloudy"}])
    periods = {p["period"]: p for p in fc.snapshot()}
    assert periods["Tonight"]["low_f"] == 61 and periods["Tonight"]["precip_pct"] == 70


def test_city_conditions_primary_and_nearby():
    agg = CityConditionsAggregator()
    agg.update("At Muncie, it was clear.")                 # sets active city
    changed = agg.update("The temperature was 61 degrees.")  # attaches to Muncie
    m = {(r["city"], r["condition"]): r["value"] for r in changed}
    assert m[("Muncie", "temperature_f")] == 61
    near = agg.update("Nearby, with a temperature of 56 at Anderson, 63 at Portland.")
    nm = {(r["city"], r["condition"]): r["value"] for r in near}
    assert nm[("Anderson", "temperature_f")] == 56
    assert nm[("Portland", "temperature_f")] == 63
    # a nearby temp must NOT be attributed to the primary city
    assert ("Muncie", "temperature_f") not in nm


def test_forecast_area_detection():
    fc = ForecastAggregator()
    fc.update("Here is the forecast for the Indianapolis area.")
    assert fc.city == "Indianapolis"


def _run():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("all extract tests passed")


if __name__ == "__main__":
    _run()


def test_extract_alert_details_tornado():
    d = extract_alert_details(
        "Tornado warning for Delaware County until 6:15 PM. A tornado was located "
        "near Yorktown, moving northeast at 40 mph. Weather spotters are activated."
    )
    assert d["until"] == "6:15 PM"
    assert d["motion"] == {"direction": "northeast", "mph": 40}
    assert "tornado" in d["threats"]
    assert "Yorktown" in d["locations"]
    assert d["spotter_activation"] is True


def test_extract_alert_details_svr_hail_wind():
    d = extract_alert_details(
        "Severe thunderstorm warning until 245 PM. Wind gusts up to 70 mph and "
        "quarter size hail. The storm was over Albany moving east at 35 miles per hour."
    )
    assert d["until"] == "245 PM"
    assert any("wind 70" in t for t in d["threats"])
    assert any("hail" in t for t in d["threats"])
    assert "Albany" in d["locations"]


def test_extract_alert_details_none_on_plain_conditions():
    assert extract_alert_details("At Muncie, it was clear with a temperature of 73 degrees.") == {}


def test_norm_city_autocorrects_stt_mishearings():
    from wxparser.extract import _norm_city
    assert _norm_city("Monthsy") == "Muncie"
    assert _norm_city("terrell") == "Terre Haute"
    assert _norm_city("Lyle") == "Lima"
    assert _norm_city("South End") == "South Bend"
    assert _norm_city("Deepan") == "Dayton"
    # an unrecognized name is just title-cased, not mangled
    assert _norm_city("anderson") == "Anderson"


def test_nearby_list_corrected_before_store():
    agg = CityConditionsAggregator()
    out = agg.update("Nearby, 64 at Deepan, 73 at Terrell, 55 at Monthsy.")
    cities = {r["city"] for r in out}
    assert cities == {"Dayton", "Terre Haute", "Muncie"}


def test_correct_terms_pies_to_highs():
    from wxparser.data.stt_terms import correct_terms
    assert correct_terms("Pies around 80.") == "Highs around 80."
    assert correct_terms("with pies in the lower 90s") == "with highs in the lower 90s"
    # forecast high is extracted only after the correction
    fc = ForecastAggregator()
    fc.update("Saturday, mostly sunny.")
    fc.update(correct_terms("Pies around 80."))
    assert {p["period"]: p for p in fc.snapshot()}["Saturday"]["high_f"] == 80
