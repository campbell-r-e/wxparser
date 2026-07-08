"""Forecast-verification (/verify) tests — full-record scoring from the DB."""

from __future__ import annotations

from datetime import date

from wxparser.verify import verify

# 2026-06-24 is the target day; period names are derived so the weekday math
# always lands the forecast on it regardless of what weekday it is.
D24, D25 = date(2026, 6, 24), date(2026, 6, 25)
DAY, DAY2 = D24.strftime("%A"), D25.strftime("%A")


def _seed_obs(db):
    # Muncie temps: 7 readings inside the Jun 24 day window (max 90), 6 inside
    # its night window (min 62, spilling past midnight into Jun 25)
    for utc, v in [("12:00", 75), ("14:00", 80), ("16:00", 85), ("18:00", 88),
                   ("20:00", 90), ("21:00", 89), ("23:00", 78)]:
        db.record_reading({"city": "Muncie", "condition": "temperature_f", "value": v},
                          f"2026-06-24T{utc}:00Z")
    for utc, v in [("02:00", 72), ("05:00", 66), ("07:00", 63),
                   ("09:00", 62), ("10:00", 64)]:
        db.record_reading({"city": "Muncie", "condition": "temperature_f", "value": v},
                          f"2026-06-25T{utc}:00Z")
    # sky: modal clear-step by day (sunny/clear/sunny), partly by night
    for utc, v in [("13:00", "sunny"), ("16:00", "clear"), ("19:00", "sunny")]:
        db.record_reading({"city": "Muncie", "condition": "sky", "value": v},
                          f"2026-06-24T{utc}:00Z")
    for d, utc in [("24", "23:30"), ("25", "03:00"), ("25", "06:00")]:
        db.record_reading({"city": "Muncie", "condition": "sky", "value": "partly cloudy"},
                          f"2026-06-{d}T{utc}:00Z")


def _seed_rain(db):
    # YTD precip: consecutive-day diffs give Jun24=0.25 (wet) and Jun25=0.00
    # (dry); Jun26 dips (STT mishear -> dropped); Jun28 follows a gap (skipped);
    # Jun29/30 diff without any PoP on record. Plus a non-precip field.
    for d, v in [("23", 10.00), ("24", 10.25), ("25", 10.25), ("26", 10.20),
                 ("28", 10.40), ("29", 10.40), ("30", 10.45)]:
        db.record_almanac({"field": "precip_year_in", "value": v},
                          f"2026-06-{d}T23:00:00Z")
    db.record_almanac({"field": "sunrise", "value": "6:14 AM"}, "2026-06-24T23:00:00Z")


def test_verify_scores_every_field(wxdb, make_cfg):
    _seed_obs(wxdb)
    _seed_rain(wxdb)
    # day-before issuance: verifiable high/low/sky + the day-before PoPs. The
    # night row's high and the day row's low must be ignored (wrong side), and
    # its "cloudy" call misses the observed partly step by 2.
    wxdb.write_forecast(
        [{"period": DAY, "high_f": 88, "low_f": 70, "sky": "sunny", "precip_pct": 40},
         {"period": f"{DAY} Night", "low_f": 60, "high_f": 95, "sky": "cloudy",
          "precip_pct": 40},
         {"period": "Blursday", "high_f": 80}],                              # no window at all
        "2026-06-23T16:00:00Z", city="Muncie")
    # day-before issuance for Jun 25: PoP lands in the scorecard, but its temp
    # and sky windows are too thin to score
    wxdb.write_forecast(
        [{"period": DAY2, "high_f": 85, "sky": "sunny", "precip_pct": 30},
         {"period": f"{DAY2} Night", "low_f": 58}],
        "2026-06-24T16:00:00Z", city="Muncie")
    # garbled sky wording is not scoreable
    wxdb.write_forecast([{"period": DAY, "sky": "hazy"}], "2026-06-23T17:00:00Z",
                        city="Muncie")
    # same-day issuance: lead 0 (its PoP must NOT enter the day-before scorecard)
    wxdb.write_forecast([{"period": "Today", "high_f": 91, "precip_pct": 55}],
                        "2026-06-24T15:00:00Z", city="Muncie")
    # issued on the target weekday itself -> next week -> lead 7 -> out of range
    wxdb.write_forecast([{"period": DAY, "high_f": 80}], "2026-06-24T12:00:00Z",
                        city="Muncie")

    doc = verify(wxdb, make_cfg())
    t = doc["temperature"]
    assert t["high"] == {"n": 2, "bias_f": -0.5, "mae_f": 1.5}   # -2 (lead 1), +1 (lead 0)
    assert t["low"] == {"n": 1, "bias_f": -2.0, "mae_f": 2.0}
    assert t["mae_by_lead_days"] == {"0": 1.0, "1": 2.0}
    assert doc["sky"] == {"n": 2, "exact_pct": 50.0, "within_one_step_pct": 50.0}

    r = doc["rain"]
    assert r["days_measured"] == 4 and r["wet_days"] == 2 and r["total_in"] == 0.3
    assert [s["day"] for s in r["scorecard"]] == ["2026-06-24", "2026-06-25"]
    assert r["scorecard"][0] == {"day": "2026-06-24", "pop_day_before": 40.0,
                                 "rained": True, "inches": 0.25}
    assert r["scorecard"][1]["rained"] is False
    assert r["brier_day_before"] == 0.225 and r["base_rate"] == 0.5
    assert r["brier_skill"] == 0.1


def test_verify_empty_record(wxdb, make_cfg):
    doc = verify(wxdb, make_cfg())
    assert doc["temperature"]["high"] == {"n": 0, "bias_f": None, "mae_f": None}
    assert doc["sky"] == {"n": 0, "exact_pct": None, "within_one_step_pct": None}
    assert doc["rain"]["days_measured"] == 0 and doc["rain"]["scorecard"] == []
    assert doc["rain"]["brier_skill"] is None


def test_verify_skill_undefined_when_every_day_matches_base_rate(wxdb, make_cfg):
    # a single scored (wet) day: base rate 1.0 -> the climatology reference
    # Brier is 0, so skill is undefined rather than a division error
    for d, v in [("23", 10.00), ("24", 10.25)]:
        db_val = {"field": "precip_year_in", "value": v}
        wxdb.record_almanac(db_val, f"2026-06-{d}T23:00:00Z")
    wxdb.write_forecast([{"period": DAY, "precip_pct": 50}],
                        "2026-06-23T16:00:00Z", city="Muncie")
    r = verify(wxdb, make_cfg())["rain"]
    assert r["brier_day_before"] == 0.25 and r["base_rate"] == 1.0
    assert r["brier_skill"] is None
