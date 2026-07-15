"""Current-conditions extraction + repeat-voting tests."""

from __future__ import annotations

from wxparser.extract import (
    AlmanacAggregator,
    CityConditionsAggregator,
    ForecastAggregator,
    _FieldVoter,
    extract_alert_details,
    extract_almanac,
    extract_forecast_fields,
    extract_observation,
    parse_temp_value,
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


def test_parse_temp_value():
    assert parse_temp_value("around 80") == 80
    assert parse_temp_value("in the mid 60s") == 65
    assert parse_temp_value("in the lower 80s") == 81
    assert parse_temp_value("near 75") == 75
    # spelled-out decades (STT renders "lower 60s" as "lower sixties")
    assert parse_temp_value("in the lower sixties") == 61
    assert parse_temp_value("mid eighties") == 85
    assert parse_temp_value("upper seventies") == 78


def test_forecast_segment_spanning_periods_does_not_leak():
    # one VAD segment routinely spans periods; a later period's high must NOT
    # land on an earlier night period (the "Overnight ... high 78" bug).
    fc = ForecastAggregator()
    fc.update("for Saturday night. Partly cloudy, lows in the lower sixties, "
              "Sunday, partly cloudy. Highs in the mid 80s.")
    s = {p["period"]: p for p in fc.snapshot()}
    assert s["Saturday Night"].get("low_f") == 61
    assert "high_f" not in s["Saturday Night"]          # no leak from Sunday
    assert s["Sunday"].get("high_f") == 85


def test_forecast_header_detected_after_lead_in():
    # "...forecast for the Muncie area. This afternoon, ..." — header is not at
    # the segment start, but must still open the period.
    fc = ForecastAggregator()
    fc.update("And now we look at the forecast for the Muncie area. This afternoon, "
              "mostly cloudy. Highs in the upper 70s.")
    s = {p["period"]: p for p in fc.snapshot()}
    assert s["This Afternoon"]["high_f"] == 78
    assert s["This Afternoon"]["sky"] == "mostly cloudy"


def test_forecast_skips_climate_outlook():
    # the 8-14 day outlook is not a daily forecast; parsing it invents bogus
    # far-future periods and phantom highs.
    fc = ForecastAggregator()
    changed = fc.update("The 8 to 14 day outlook for Thursday, July 2nd through "
                        "Wednesday, July 8th calls for temperatures above normal. "
                        "Normal high is 85.")
    assert changed is False
    assert fc.snapshot() == []


def test_day_period_never_gets_a_low():
    fc = ForecastAggregator()
    fc.update("Saturday, partly cloudy. Highs in the mid 80s. Lows tonight in the 60s.")
    s = {p["period"]: p for p in fc.snapshot()}
    assert s["Saturday"]["high_f"] == 85
    assert "low_f" not in s["Saturday"]


def test_new_forecast_pass_resets_carryover():
    # a stale "This Afternoon" must not absorb the lead text of a fresh pass.
    fc = ForecastAggregator()
    fc.update("This afternoon, mostly cloudy. Highs in the upper 70s.")
    fc.update("Taking a look at your 3-7 day forecast for the Muncie area. "
              "Clear, hot, highs in the mid-90s. Saturday night, partly cloudy. "
              "Lows in the lower 60s.")
    s = {p["period"]: p for p in fc.snapshot()}
    assert s["This Afternoon"]["high_f"] == 78        # not polluted to 95
    assert "low_f" not in s["This Afternoon"]
    assert s["Saturday Night"]["low_f"] == 61


def test_recap_lead_in_does_not_leak_precip_onto_today():
    # "...chance of rain 20%. Once again, the forecast for today..." — the trailing
    # PoP of the extended block must not ride onto the recapped Today (Today itself
    # states no chance of rain here).
    fc = ForecastAggregator()
    fc.update("Once again, the forecast for today. Partly cloudy. "
              "Highs in the mid 80s. North winds around 5 miles an hour.")
    fc.update("Highs in the upper 80s. Chance of rain 20%. Once again, the "
              "forecast for today. Partly cloudy. Highs in the mid 80s.")
    t = {p["period"]: p for p in fc.snapshot()}["Today"]
    assert t["high_f"] == 85
    assert "precip_pct" not in t


def test_stale_carryover_does_not_absorb_next_periods_precip():
    # a carry-over left from an earlier pass ("Wednesday") must not swallow the
    # trailing PoP of the day aired just before this segment's first header.
    fc = ForecastAggregator()
    fc.update("Wednesday, partly cloudy. Highs in the mid 80s.")
    fc.update("Highs in the mid 80s. Chance of rain 70%. Friday night, "
              "mostly cloudy. Lows in the upper 60s.")
    s = {p["period"]: p for p in fc.snapshot()}
    assert "precip_pct" not in s["Wednesday"]      # the 70% was Friday's
    assert s["Friday Night"]["low_f"] == 68


def test_same_day_carryover_keeps_its_precip():
    # the flip side: a genuine chop "Friday. ...Highs..." then "[chop] Chance of
    # rain 70%. Friday night..." — Friday IS Friday-Night's predecessor, so its own
    # trailing PoP must still attach (the leak fix must not over-reach).
    fc = ForecastAggregator()
    fc.update("Friday, cloudy. Highs in the mid 80s.")
    fc.update("Chance of rain 70%. Friday night, mostly cloudy. Lows in the upper 60s.")
    s = {p["period"]: p for p in fc.snapshot()}
    assert s["Friday"]["precip_pct"] == 70
    assert s["Friday Night"]["low_f"] == 68


def test_for_misheard_as_four_still_opens_period():
    # ". For Thursday night" is routinely transcribed "Four Thursday night"; the
    # header must still open so its PoP attaches to Thursday Night, not the
    # carry-over Today.
    fc = ForecastAggregator()
    fc.update("Today, partly cloudy. Highs in the mid 80s.")
    fc.update("Four Thursday night, partly cloudy with a chance of showers. "
              "Lows in the upper 60s. Chance of rain 70%.")
    s = {p["period"]: p for p in fc.snapshot()}
    assert "precip_pct" not in s["Today"]
    assert s["Thursday Night"]["low_f"] == 68
    assert s["Thursday Night"]["precip_pct"] == 70


def test_stale_carryover_dropped_before_weekday_day_header():
    # first header is a bare weekday ("...Chance of rain 70%. Friday. A chance of
    # showers..."); Friday's predecessor is Thursday Night, so a stale "Wednesday"
    # carry-over must not absorb the trailing PoP that actually closed Thursday
    # Night (the real "...Friday. A chance of showers..." airing leak).
    fc = ForecastAggregator()
    fc.update("Wednesday, sunny. Highs in the mid 80s.")
    fc.update("Lows in the upper 60s. Chance of rain 70%. Friday. "
              "A chance of showers. Highs in the mid 80s.")
    s = {p["period"]: p for p in fc.snapshot()}
    assert "precip_pct" not in s["Wednesday"]
    assert s["Friday"]["high_f"] == 85


def test_near_term_header_garbles_normalized():
    # "Rest of"->"West of" and ".Tonight"/"For tonight"->"Port tonight" are routine
    # STT garbles; without normalizing them the near-term headers never open and
    # Today/Tonight freeze on a stale value.
    fc = ForecastAggregator()
    fc.update("West of today, partly cloudy. Highs in the mid 80s.")
    fc.update("Port tonight, partly cloudy. Lows in the mid 60s.")
    s = {p["period"]: p for p in fc.snapshot()}
    assert s["Rest Of Today"]["high_f"] == 85
    assert s["Tonight"]["low_f"] == 65


def test_stale_period_evicted_from_snapshot():
    # a period the broadcast stops airing (superseded time-of-day relabel or a
    # dropped extended day) must age out instead of serving a frozen value.
    fc = ForecastAggregator(stale_passes=2)
    fc.update("Forecast for the Muncie area. Today, sunny. Highs in the mid 80s.")
    for _ in range(3):   # three readouts that no longer mention Today
        fc.update("Forecast for the Muncie area. Saturday, sunny. Highs in the mid 80s.")
    s = {p["period"]: p for p in fc.snapshot()}
    assert "Today" not in s
    assert s["Saturday"]["high_f"] == 85


def test_cleared_precip_ages_out_while_period_stays():
    # when a period's chance of rain drops to nil the broadcast just stops saying
    # "chance of rain" (no vote), so the precip field must age out on its own even
    # though the period keeps airing its high/sky.
    fc = ForecastAggregator(stale_passes=2)
    fc.update("Forecast for the Muncie area. Today, cloudy. Highs in the mid 80s. "
              "Chance of rain 70%.")
    for _ in range(3):
        fc.update("Forecast for the Muncie area. Today, sunny. Highs in the mid 80s.")
    t = {p["period"]: p for p in fc.snapshot()}["Today"]
    assert t["high_f"] == 85
    assert "precip_pct" not in t


def test_night_period_never_gets_a_high():
    # grouped extended phrasing "Sunday night through Wednesday ... highs in the
    # lower 90s" must not put that daytime high on the night period.
    fc = ForecastAggregator()
    fc.update("for Sunday night through Wednesday, mostly clear, hot. "
              "Highs in the lower 90s. Lows in the upper 60s.")
    s = {p["period"]: p for p in fc.snapshot()}
    assert "high_f" not in s["Sunday Night"]
    assert s["Sunday Night"]["low_f"] == 68


def test_forecast_votes_reject_oneoff_garble():
    # the consensus must win: a single garbled airing (Sunday high heard as "lower
    # 70s" once) can't overwrite the well-voted value ("mid 80s" five times). This
    # is the bug the audit found — latest-airing-wins served 71 over a voted 85.
    fc = ForecastAggregator()
    for _ in range(5):
        fc.update("Sunday, mostly sunny. Highs in the mid 80s.")
    fc.update("Sunday, mostly sunny. Highs in the lower 70s.")
    assert {p["period"]: p for p in fc.snapshot()}["Sunday"]["high_f"] == 85


def test_forecast_low_mid_eighties_is_mishear():
    # Real airing (2026-07-07): "Wednesday night, mostly clear, lows in the
    # mid-eighties." — parallel airings said mid-SIXTIES. A forecast low of 85+
    # never verifies in this climate, so extraction refuses to store it.
    assert "low_f" not in extract_forecast_fields("Lows in the mid-eighties.")
    fc = ForecastAggregator()
    fc.update("The forecast for the Muncie area. Wednesday night, mostly clear, "
              "lows in the mid-eighties.")
    snap = {p["period"]: p for p in fc.snapshot()}
    assert snap["Wednesday Night"].get("low_f") is None
    assert snap["Wednesday Night"]["sky"] == "clear"  # rest of the period kept


def test_forecast_steady_temp_respects_night_low_cap():
    # an unlabeled steady temp routed onto a night period must clear the same
    # low cap — "near steady temperature in the upper 80s" is the same mishear
    fc = ForecastAggregator()
    fc.update("For Wednesday night, mostly clear, with a near steady "
              "temperature in the upper 80s.")
    snap = {p["period"]: p for p in fc.snapshot()}
    assert snap["Wednesday Night"].get("low_f") is None
    # a plausible steady temp still routes to the night low as before
    fc2 = ForecastAggregator()
    fc2.update("For Wednesday night, mostly clear, with a near steady "
               "temperature in the upper 60s.")
    assert {p["period"]: p for p in fc2.snapshot()}["Wednesday Night"]["low_f"] == 68


def test_forecast_revision_wins_once_it_dominates():
    # a genuine revision still takes over once it dominates the recent window.
    fc = ForecastAggregator()
    for _ in range(3):
        fc.update("Saturday, sunny. Highs in the upper 80s.")   # 88
    for _ in range(10):
        fc.update("Saturday, cloudy. Highs in the lower 70s.")  # revised to 71
    assert {p["period"]: p for p in fc.snapshot()}["Saturday"]["high_f"] == 71


def test_forecast_confidence_flags_contested_value():
    # vote agreement is exposed per field; a contested low (heard two ways nearly
    # equally) lands below the uncertain threshold so consumers can distrust it.
    fc = ForecastAggregator()
    for _ in range(8):
        fc.update("Tonight, clear. Lows in the lower 60s.")   # 61 x8
    for _ in range(7):
        fc.update("Tonight, clear. Lows in the mid 70s.")     # 75 x7
    p = {x["period"]: x for x in fc.snapshot()}["Tonight"]
    assert p["low_f"] == 61 and p["confidence"]["low_f"] < 0.6


def test_forecast_update_dedupes_unchanged():
    # update() returns True only when a voted value actually changed, so the store
    # isn't rewritten a full issuance per airing.
    fc = ForecastAggregator()
    assert fc.update("Tonight, clear. Lows in the lower 60s.") is True    # 61: new value
    assert fc.update("Tonight, clear. Lows in the lower 60s.") is False   # identical -> no write
    assert fc.update("Saturday, sunny. Highs in the mid 80s.") is True    # new period -> write


def test_forecast_confidence_full_when_consistent():
    fc = ForecastAggregator()
    for _ in range(5):
        fc.update("Saturday, sunny. Highs in the mid 80s.")   # 85 x5, unanimous
    p = {x["period"]: x for x in fc.snapshot()}["Saturday"]
    assert p["high_f"] == 85 and p["confidence"]["high_f"] == 1.0


def test_precip_word_number_and_comma():
    assert extract_forecast_fields("Chance of rain eighty percent.")["precip_pct"] == 80
    assert extract_forecast_fields("Chance of rain, 80%.")["precip_pct"] == 80


def test_garbled_decade_words():
    # STT mangles "nineties"->"naddies", "eighties"->"aidies" in "highs in..."
    assert extract_forecast_fields("Hot with highs in the lower naddies.")["high_f"] == 91
    assert extract_forecast_fields("cloudy, highs in the mid-aidies.")["high_f"] == 85


def test_steady_temperature_routes_by_period():
    # "near steady temperature in the X" has no high/low label -> high by day,
    # low by night, and conditions "temperature was N degrees" stays out.
    fc = ForecastAggregator()
    fc.update("Rest of today, partly cloudy. Near steady temperature in the upper 70s.")
    assert {p["period"]: p for p in fc.snapshot()}["Rest Of Today"]["high_f"] == 78
    fc2 = ForecastAggregator()
    fc2.update("Tonight, clear. Near steady temperature in the lower 60s.")
    assert {p["period"]: p for p in fc2.snapshot()}["Tonight"]["low_f"] == 61
    assert extract_forecast_fields("The temperature was 72 degrees.") == {}


def test_forecast_3_to_7_day_is_not_skipped_as_outlook():
    # the "3-7 day forecast" is a real extended forecast (Saturday/Sunday highs),
    # NOT the "8-14 day outlook" climate product — it must still parse.
    fc = ForecastAggregator()
    fc.update("Taking a look at your 3-7 day forecast for the Muncie area for "
              "Saturday night. Partly cloudy, lows in the lower 60s, Sunday, "
              "partly cloudy. Highs in the mid 80s.")
    s = {p["period"]: p for p in fc.snapshot()}
    assert s["Saturday Night"]["low_f"] == 61
    assert s["Sunday"]["high_f"] == 85
    assert "high_f" not in s["Saturday Night"]



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


def test_precip_accepts_percent_sign_and_word():
    # whisper emits both "40%" and "40 percent" across runs; extract both.
    assert extract_forecast_fields("Chance of rain 40%.")["precip_pct"] == 40
    assert extract_forecast_fields("Chance of rain 40 percent.")["precip_pct"] == 40
    assert extract_forecast_fields("Chance of precipitation 90%.")["precip_pct"] == 90


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


def test_primary_obs_with_trailing_roundup_in_same_segment():
    # regression: when one segment carries BOTH the home-station observation and a
    # trailing roundup temp, the whole Muncie obs used to be dropped (only the
    # nearby "Marion 74" was recorded), freezing /current for hours.
    agg = CityConditionsAggregator()
    out = agg.update(
        "At noon, at Muncie, it was partly sunny. The temperature was 76 degrees, "
        "and the relative humidity 68%. The wind was southwest at 8 miles an hour. "
        "The barometric pressure was 29.97 inches and falling. Nearby, 74 at Marion."
    )
    m = {(r["city"], r["condition"]): r["value"] for r in out}
    assert m[("Muncie", "temperature_f")] == 76
    assert m[("Muncie", "wind")] == "southwest at 8"
    assert m[("Muncie", "wind_speed_mph")] == 8      # derived from the voted phrase
    assert m[("Muncie", "sky")] == "partly sunny"
    assert m[("Muncie", "pressure_in")] == 29.97
    assert m[("Muncie", "pressure_trend")] == "falling"
    assert m[("Marion", "temperature_f")] == 74


def test_wind_speed_follows_voted_direction():
    # regression: wind (phrase) and wind_speed_mph were voted as independent
    # fields over the same window, so a tie could break the speed to 0 while the
    # phrase won "west at 8" — surfacing wind="west at 8" with wind_speed_mph=0.
    # wind_speed_mph is now derived from the winning phrase, always consistent.
    agg = CityConditionsAggregator()
    for _ in range(3):
        agg.update("At Muncie, it was sunny. The wind was calm.")
    out = []
    for _ in range(5):
        out = agg.update("At Muncie, it was sunny. The wind was west at 8 miles an hour.")
    m = {(r["city"], r["condition"]): r["value"] for r in out}
    assert m[("Muncie", "wind")] == "west at 8"
    assert m[("Muncie", "wind_speed_mph")] == 8      # follows the phrase, not a 0 tie-break

    # when calm wins the vote, the derived speed is 0
    calm = CityConditionsAggregator().update("At Muncie, it was clear. The wind was calm.")
    cm = {(r["city"], r["condition"]): r["value"] for r in calm}
    assert cm[("Muncie", "wind")] == "calm"
    assert cm[("Muncie", "wind_speed_mph")] == 0


def test_home_header_tolerates_at_misheard_as_it():
    # STT hears "At Muncie, it was..." as "it Muncie, it was..."; the home obs
    # must still be recognized even with a trailing roundup in the same segment.
    agg = CityConditionsAggregator()
    out = agg.update(
        "At 1 p.m., it Muncie, it was mostly sunny. The temperature was 77 degrees, "
        "and the relative humidity 68%. The barometric pressure was 29.97 inches and "
        "steady. Nearby, with a temperature of 74 at Indianapolis, 76 at Marion."
    )
    m = {(r["city"], r["condition"]): r["value"] for r in out}
    assert m[("Muncie", "temperature_f")] == 77
    assert m[("Muncie", "sky")] == "mostly sunny"
    assert m[("Marion", "temperature_f")] == 76
    assert ("Indianapolis", "temperature_f") in m


def test_home_header_was_reported_singular_with_roundup():
    # live regression (2026-06-27): the home ob airs as "<sky> WAS reported" (singular
    # subject, e.g. "fog was reported"), but the header only accepted "were reported".
    # With a roundup in the same segment, Muncie's whole home block (humidity/wind/
    # pressure — the home-only fields) was dropped, freezing them for hours while temp
    # stayed fresh via the roundup. The header must accept "was reported" too.
    agg = CityConditionsAggregator()
    out = agg.update(
        "At 6 a.m., it Muncie, fog was reported. The temperature was 68 degrees, "
        "and the relative humidity 93%. The wind was east at 5 miles an hour. "
        "Elsewhere across Indiana, fog was reported with a temperature of 70 at "
        "Terre Haute and 67 at Portland."
    )
    m = {(r["city"], r["condition"]): r["value"] for r in out}
    assert m[("Muncie", "temperature_f")] == 68
    assert m[("Muncie", "humidity_pct")] == 93      # the home-only field that was frozen
    assert m[("Muncie", "wind")] == "east at 5"
    assert m[("Terre Haute", "temperature_f")] == 70  # roundup still works
    assert m[("Portland", "temperature_f")] == 67


def test_recap_it_was_n_degrees_extracts_temp():
    # the 1 p.m. recap "...it Muncie, it was 76 degrees..." lacks the word
    # "temperature"; it must still yield the home temp, but "it was mostly sunny"
    # must not be read as a temperature.
    assert extract_observation("it was 76 degrees with partly sunny skies")["temperature_f"] == 76
    assert "temperature_f" not in extract_observation("it was mostly sunny.")
    out = CityConditionsAggregator().update("Once again, it Muncie, it was 76 degrees.")
    assert {(r["city"], r["condition"]): r["value"] for r in out}[("Muncie", "temperature_f")] == 76


def test_conditions_wind_direction_extracted():
    # regression: the alert wind-speed regex shadowed _RE_WIND, so current-
    # conditions wind direction never extracted.
    o = extract_observation("The wind was northwest at 12 miles an hour.")
    assert o["wind"] == "northwest at 12"
    assert o["wind_speed_mph"] == 12


def test_field_voter_stale_window_excludes_old_samples():
    # a low-frequency field: four hours-old sightings and one fresh one. The stale
    # window bounds the vote to the fresh sighting so the old majority can't win.
    v = _FieldVoter(maxlen=15, stale=5)
    for _ in range(4):
        v.add(93, 0)
    v.add(64, 20)                 # gap 20 > 5 -> the 93s fall outside the window
    assert v.best().value == 64
    # without a window the stale majority wins (the pre-fix behavior)
    v2 = _FieldVoter(maxlen=15)
    for _ in range(4):
        v2.add(93)
    v2.add(64)
    assert v2.best().value == 93


def test_conditions_low_frequency_field_ages_out():
    # humidity aired several times in the morning (93), then a long run of temp-only
    # airings advances the tick, then the current 64 airs once. The stale morning
    # value must not out-vote the fresh one (the "humidity 93% at 2pm" bug).
    agg = CityConditionsAggregator(stale_ticks=3)
    for _ in range(4):
        agg.update("At Muncie, it was sunny. The temperature was 80 degrees "
                   "and the relative humidity 93%.")
    for _ in range(5):   # temp-only airings; humidity not restated
        agg.update("At Muncie, it was 80 degrees.")
    agg.update("At Muncie, it was sunny. The temperature was 80 degrees "
               "and the relative humidity 64%.")
    assert agg.voters[("Muncie", "humidity_pct")].best().value == 64


def test_conditions_temperature_tracks_recent_over_long_window():
    # temperature airs ~2x/cycle and legitimately changes hour to hour, so a long
    # majority window trails the real value by hours. It votes over a short window,
    # so a morning run at 67 gives way to the current cycle's 69 instead of 67
    # winning on sheer count — the noon "67 while it read 69" lag.
    agg = CityConditionsAggregator()
    for _ in range(8):
        agg.update("At Muncie, it was clear. The temperature was 67 degrees.")
    for _ in range(3):
        agg.update("At Muncie, it was clear. The temperature was 69 degrees.")
    assert agg.voters[("Muncie", "temperature_f")].best().value == 69
    # temperature gets the short window; a slow field (sky) keeps the long one, so
    # the same run of airings leaves sky voting over all 11 sightings.
    assert agg.voters[("Muncie", "temperature_f")].samples.maxlen == 5
    assert agg.voters[("Muncie", "sky")].samples.maxlen == 15
    # control: over the long window the eight stale 67s would still out-vote the 69s
    long = _FieldVoter(maxlen=15)
    for _ in range(8):
        long.add(67)
    for _ in range(3):
        long.add(69)
    assert long.best().value == 67


def test_conditions_temperature_rejects_lone_stt_outlier():
    # the short window is still deep enough that a single garbled reading loses to
    # its neighbours (the reason it's a vote and not just latest-wins).
    agg = CityConditionsAggregator()
    for _ in range(2):
        agg.update("At Muncie, it was 68 degrees.")
    agg.update("At Muncie, it was 97 degrees.")   # lone mishear
    agg.update("At Muncie, it was 68 degrees.")
    assert agg.voters[("Muncie", "temperature_f")].best().value == 68


def test_conditions_prime_seeds_value_until_fresh_airing():
    # a restart primes from the stored latest reading; the primed value serves
    # (seeded at the current tick, so not instantly stale) until a live airing votes.
    agg = CityConditionsAggregator()
    agg.prime([{"city": "Muncie", "condition": "temperature_f", "value": 72}])
    assert agg.voters[("Muncie", "temperature_f")].best().value == 72
    agg.update("At Muncie, it was 75 degrees.")
    assert agg.voters[("Muncie", "temperature_f")].best().value in (72, 75)


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
    assert _norm_city("Terrell Hook") == "Terre Haute"   # small.en garble variant
    assert _norm_city("Lyle") == "Lima"
    assert _norm_city("South End") == "South Bend"
    assert _norm_city("Soft End") == "South Bend"        # small.en garble variant
    assert _norm_city("Deepan") == "Dayton"
    # an unrecognized name is just title-cased, not mangled
    assert _norm_city("anderson") == "Anderson"


def test_nearby_list_corrected_before_store():
    agg = CityConditionsAggregator()
    out = agg.update("Nearby, 64 at Deepan, 73 at Terrell, 55 at Monthsy.")
    cities = {r["city"] for r in out}
    assert cities == {"Dayton", "Terre Haute", "Muncie"}


def _temps(text):
    return {r["city"]: r["value"] for r in CityConditionsAggregator().update(text)
            if r["condition"] == "temperature_f"}


def test_roundup_reported_form():
    assert _temps("Portland reported 75, Richmond reported at 73, and Shelbyville reported 74.") \
        == {"Portland": 75, "Richmond": 73, "Shelbyville": 74}


def test_roundup_temperature_of_form():
    # number AFTER the city; "at"/"Ed" (STT for "at") both work
    assert _temps("Ed Lima, Ohio, it was clear with a temperature of 71.") == {"Lima": 71}
    assert _temps("At Cincinnati, rain was falling with a temperature of 72.") == {"Cincinnati": 72}
    # and the home city's sky is NOT polluted by the roundup city's "it was ..."
    out = CityConditionsAggregator(primary_city="Muncie").update(
        "Ed Lima, Ohio, it was partly cloudy, with a temperature of 71.")
    assert not any(r["city"] == "Muncie" for r in out)


def test_roundup_dedup_same_city_value():
    out = CityConditionsAggregator().update("74 at Marion. Marion reported 74.")
    assert sum(1 for r in out if r["city"] == "Marion") == 1


def test_roundup_peer_outlier_dropped():
    # Real mishear (2026-06-28): "...22 at Cincinnati..." for 92 — a lost leading
    # digit passes the absolute range check but sits 50F off its roundup peers.
    t = _temps("It was cloudy with a temperature of 74 at Champaign, Illinois, "
               "72 at Dayton, 22 at Cincinnati.")
    assert "Cincinnati" not in t
    assert t["Dayton"] == 72 and t["Champaign"] == 74


def test_roundup_peer_outlier_needs_quorum():
    # with only two readings there is no telling which one is the impostor
    assert _temps("Nearby, 74 at Marion, 22 at Anderson.") \
        == {"Marion": 74, "Anderson": 22}


def test_roundup_real_front_spread_survives():
    # a sharp front skews the whole feed together — a genuine 28F end-to-end
    # spread clusters around the median and every reading is kept
    assert _temps("Nearby, 40 at Chicago, 55 at Lafayette, 68 at Louisville.") \
        == {"Chicago": 40, "Lafayette": 55, "Louisville": 68}


def test_slot_resolves_novel_garble_after_champaign_anchor():
    # The entry right after "Champaign, Illinois" is Lima. A garble we have NEVER
    # catalogued must still land under Lima via the positional anchor, not a
    # spelling list — this is what ends the whack-a-mole.
    assert _temps("69 at Champaign, Illinois, 67 at Zorblax and 71 at Dayton.") \
        == {"Champaign": 69, "Lima": 67, "Dayton": 71}


def test_slot_resolves_novel_garble_in_just_outside_indiana_leadin():
    assert _temps("Just outside Indiana, Ed Quixotle, sunny with a temperature of 73.") \
        == {"Lima": 73}


def test_slot_resolves_garble_via_extended_anchor_chain():
    # The mined roundup order gives a full anchor chain, so a garble in ANY
    # anchored slot lands under the right city without a spelling entry — the
    # entry after Dayton is Cincinnati, and the one after Anderson is Indianapolis.
    assert _temps("71 at Dayton, 70 at Blorptown.") == {"Dayton": 71, "Cincinnati": 70}
    assert _temps("80 at Anderson, 75 at Wuzzle.") == {"Anderson": 80, "Indianapolis": 75}


def test_slot_does_not_remap_unknown_outside_a_known_slot():
    # An unknown city that is NOT in an anchored slot is left untouched (better a
    # harmless phantom than a wrong real-city reading). Louisville ends the mined
    # chain, so it has no anchor.
    assert _temps("80 at Louisville, 75 at Wuzzle.") \
        == {"Louisville": 80, "Wuzzle": 75}


def test_slot_leaves_known_city_after_anchor_untouched():
    # A roster city following the anchor must not be force-mapped to Lima.
    assert _temps("69 at Champaign, Illinois, 70 at Cincinnati.") \
        == {"Champaign": 69, "Cincinnati": 70}


def test_ed_muncie_header():
    out = {(r["city"], r["condition"]): r["value"] for r in
           CityConditionsAggregator().update(
               "Ed Muncie, it was cloudy. The temperature was 76 degrees.")}
    assert out[("Muncie", "temperature_f")] == 76 and out[("Muncie", "sky")] == "cloudy"


def test_timestamped_were_reported_home_ob():
    # the evening ob form "At 7 p.m., <garbled Muncie>, ... were reported. The
    # temperature was N" must still attach the temp to the home city — even when a
    # roundup follows in the same segment (which is what was dropping it).
    for header in ("Ed Muncie", "Edmondsee"):
        out = {(r["city"], r["condition"]): r["value"] for r in
               CityConditionsAggregator().update(
                   f"At 7 p.m., {header}, light rain and fog were reported. "
                   "The temperature was 70 degrees. Nearby, 71 at Anderson.")}
        assert out[("Muncie", "temperature_f")] == 70, header
        assert out[("Anderson", "temperature_f")] == 71, header  # roundup still parsed


def test_correct_terms_pies_to_highs():
    from wxparser.data.stt_terms import correct_terms
    assert correct_terms("Pies around 80.") == "Highs around 80."
    assert correct_terms("with pies in the lower 90s") == "with highs in the lower 90s"
    # forecast high is extracted only after the correction
    fc = ForecastAggregator()
    fc.update("Saturday, mostly sunny.")
    fc.update(correct_terms("Pies around 80."))
    assert {p["period"]: p for p in fc.snapshot()}["Saturday"]["high_f"] == 80


def test_correct_terms_chants_of_brain_to_chance_of_rain():
    from wxparser.data.stt_terms import correct_terms
    assert correct_terms("Chants of Brain 90% for Friday") == "Chance of Rain 90% for Friday"
    assert correct_terms("a chants of brain") == "a chance of rain"


def test_correct_terms_eyes_blows_to_highs_lows():
    # STT garbles "highs"->"eyes" and "lows"->"blows" in grouped extended-period
    # forecasts; uncorrected, the daytime high band leaks onto the night period as
    # an impossible low (observed: Sunday Night low 95F).
    from wxparser.data.stt_terms import correct_terms
    assert correct_terms("eyes in the lower 90s") == "highs in the lower 90s"
    assert correct_terms("Blows in the lower 70s") == "Lows in the lower 70s"
    # end-to-end: after correction the grouped period routes correctly — the 90s
    # high is dropped from the night period and the real low is recovered.
    fc = ForecastAggregator()
    fc.update(correct_terms("for Sunday night through Wednesday, mostly clear, hot, "
                            "blows in the lower 70s, eyes in the lower 90s."))
    sun = {p["period"]: p for p in fc.snapshot()}["Sunday Night"]
    assert "high_f" not in sun and sun["low_f"] == 71


def test_correct_terms_mined_garbles():
    # additional consistent mis-hearings found by mining transcripts.
    from wxparser.data.stt_terms import correct_terms
    assert correct_terms("Hives in the upper 70s.") == "Highs in the upper 70s."
    assert correct_terms("flows in the lower 70s") == "lows in the lower 70s"
    assert correct_terms("Lowes in the mid-sixties") == "Lows in the mid-sixties"
    assert correct_terms("Cants of rain 30%.") == "Chance of rain 30%."


def test_correct_terms_close_is_context_scoped():
    # "close" -> "lows" only in the temperature slot; legit uses are untouched.
    from wxparser.data.stt_terms import correct_terms
    assert correct_terms("Close in the lower 60s.") == "Lows in the lower 60s."
    assert correct_terms("close around 70") == "lows around 70"
    # must NOT corrupt legitimate climate phrasing
    assert correct_terms("temperatures close to normal") == "temperatures close to normal"
    assert correct_terms("a close call with the storm") == "a close call with the storm"


def test_correct_terms_eased_is_context_scoped():
    # "east" wind direction mis-heard as "eased" ("the wind was eased at 7 miles
    # an hour", "eased winds around 5") -> corrected only in wind position.
    from wxparser.data.stt_terms import correct_terms
    assert correct_terms("The wind was eased at 7 miles an hour.") == \
        "The wind was east at 7 miles an hour."
    assert correct_terms("Eased winds around 5 miles an hour.") == \
        "East winds around 5 miles an hour."
    # must NOT corrupt legitimate uses of "eased"
    assert correct_terms("winds eased overnight") == "winds eased overnight"
    assert correct_terms("the threat eased at 7 p.m.") == "the threat eased at 7 p.m."


def test_correct_terms_failed_is_context_scoped():
    # "fell" in the almanac precip recap mis-heard as "failed" ("no precipitation
    # failed yesterday") -> corrected only right after "precipitation".
    from wxparser.data.stt_terms import correct_terms
    assert correct_terms("No precipitation failed yesterday.") == \
        "No precipitation fell yesterday."
    assert correct_terms("precipitation failed") == "precipitation fell"
    # must NOT corrupt legitimate uses of "failed"
    assert correct_terms("the warning failed to clear") == "the warning failed to clear"
    assert correct_terms("the system failed overnight") == "the system failed overnight"


# --- almanac / climate recap extraction ------------------------------------ #
def test_extract_almanac_full_block():
    out = extract_almanac(
        "The total precipitation for the year now stands at 17.39 inches, which is "
        "2.72 inches below normal. Sunset tonight is at 9.15pm. Sunrise tomorrow is at 6.14am.")
    assert out == {"sunrise": "6:14 AM", "sunset": "9:15 PM",
                   "precip_year_in": 17.39, "precip_departure_in": -2.72}


def test_extract_almanac_time_forms_and_above_normal():
    # colon form, no-minutes form, and an above-normal (positive) departure
    out = extract_almanac(
        "Sunrise today is at 6:13 AM and sunset is at 9 PM. The total precipitation "
        "from the year still stands at 30.00 inches, which is 1.20 inches above normal.")
    assert out["sunrise"] == "6:13 AM" and out["sunset"] == "9:00 PM"
    assert out["precip_departure_in"] == 1.2


def test_extract_almanac_degree_days_and_normal_week():
    # "no" -> 0, and the season-to-date total ("this leaves 288") is NOT the daily value
    out = extract_almanac(
        "There were no heating degree days yesterday. This leaves 288 heating degree days. "
        "The normal precipitation total for the seven days is around 1.10 inches.")
    assert out["heating_degree_days"] == 0 and out["normal_precip_week_in"] == 1.1
    assert extract_almanac("There were 6 cooling degree days yesterday.")[
        "cooling_degree_days"] == 6


def test_extract_almanac_degree_days_spelled_out():
    # STT renders small counts as words as often as digits; a word-only day used to
    # be dropped, freezing the vote on the prior digit value.
    assert extract_almanac("There were nine cooling degree days yesterday.")[
        "cooling_degree_days"] == 9
    assert extract_almanac("There were twenty one heating degree days yesterday.")[
        "heating_degree_days"] == 21
    # an unparseable count is skipped, not crashed on or stored as garbage
    out = extract_almanac(
        "There were several heating degree days and several cooling degree days yesterday.")
    assert "heating_degree_days" not in out and "cooling_degree_days" not in out


def test_extract_almanac_ignores_non_climate_and_range():
    assert extract_almanac("At Muncie, the temperature was 76 degrees with cloudy skies.") == {}
    assert extract_almanac("Tonight, mostly cloudy with a chance of rain 50%.") == {}
    # out-of-range YTD precip is dropped by the range check
    assert "precip_year_in" not in extract_almanac(
        "precipitation for the year now stands at 999.00 inches")


def test_almanac_aggregator_votes_primes_snapshots():
    agg = AlmanacAggregator()
    agg.update("Sunrise tomorrow is at 6.14am.")
    agg.update("Sunrise tomorrow is at 6.14am.")
    agg.update("Sunrise tomorrow is at 7.00am.")          # outvoted
    snap = agg.snapshot()
    assert snap["sunrise"]["value"] == "6:14 AM" and snap["sunrise"]["votes"] == 2
    # prime a fresh aggregator from stored latest readings
    primed = AlmanacAggregator()
    primed.prime([{"field": "sunset", "value": "9:15 PM"}])
    assert primed.snapshot()["sunset"]["value"] == "9:15 PM"


def test_roundup_peer_band_is_tunable():
    # the band comes from Config in production (WX_PEER_MIN_CITIES /
    # WX_PEER_MAX_DEV_F); verify the aggregator parameters actually steer it
    text = ("It was cloudy with a temperature of 74 at Champaign, Illinois, "
            "72 at Dayton, 60 at Cincinnati.")
    default = CityConditionsAggregator().update(text)
    assert any(r["city"] == "Cincinnati" for r in default)      # within ±30
    tight = CityConditionsAggregator(peer_max_dev=5).update(text)
    assert not any(r["city"] == "Cincinnati" for r in tight)    # outside ±5
    no_quorum = CityConditionsAggregator(peer_min=4, peer_max_dev=5).update(text)
    assert any(r["city"] == "Cincinnati" for r in no_quorum)    # only 3 cities
