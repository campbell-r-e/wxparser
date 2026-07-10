"""PostgreSQL store round-trip tests (generic city/condition schema)."""

from __future__ import annotations

from datetime import datetime, timezone

from wxparser.db import period_window


def test_city_conditions_latest(wxdb):
    db = wxdb
    db.record_reading({"city": "Muncie", "condition": "temperature_f", "value": 61,
                       "votes": 3, "total": 4}, "2026-06-24T06:00:00Z")
    db.record_reading({"city": "Anderson", "condition": "temperature_f", "value": 56,
                       "votes": 1, "total": 1}, "2026-06-24T06:01:00Z")
    by = {r["city"]: r["value"] for r in db.latest_for_condition("temperature_f")}
    assert by["Muncie"] == 61 and by["Anderson"] == 56


def test_min_sightings_filter(wxdb):
    db = wxdb
    db.record_reading({"city": "Lulule", "condition": "temperature_f", "value": 72},
                      "2026-06-24T06:00:00Z")  # heard once -> garbage
    db.record_reading({"city": "Anderson", "condition": "temperature_f", "value": 56},
                      "2026-06-24T06:00:00Z")
    db.record_reading({"city": "Anderson", "condition": "temperature_f", "value": 57},
                      "2026-06-24T06:05:00Z")  # heard twice
    surfaced = {r["city"] for r in db.latest_for_condition("temperature_f", min_sightings=2)}
    assert "Anderson" in surfaced and "Lulule" not in surfaced
    # raw history still has both
    assert any(h["city"] == "Lulule" for h in db.condition_history("temperature_f", None, None, None))


def test_text_condition_roundtrip(wxdb):
    db = wxdb
    db.record_reading({"city": "Muncie", "condition": "sky", "value": "clear"},
                      "2026-06-24T06:00:00Z")
    rows = db.latest_for_condition("sky")
    assert rows[0]["city"] == "Muncie" and rows[0]["value"] == "clear"


def test_condition_history_between_times(wxdb):
    db = wxdb
    db.record_reading({"city": "Muncie", "condition": "temperature_f", "value": 61},
                      "2026-06-24T06:00:00Z")
    db.record_reading({"city": "Muncie", "condition": "temperature_f", "value": 63},
                      "2026-06-24T07:00:00Z")
    h = db.condition_history("temperature_f", "Muncie",
                             "2026-06-24T06:30:00Z", "2026-06-24T07:30:00Z")
    assert len(h) == 1 and h[0]["value"] == 63


def test_forecast_city_and_latest(wxdb):
    db = wxdb
    db.write_forecast([{"period": "Tonight", "low_f": 61, "precip_pct": 70, "sky": "partly cloudy"}],
                      "2026-06-24T12:00:00Z", city="Muncie")
    fcs = db.latest_forecasts()
    assert fcs[0]["city"] == "Muncie"
    p = fcs[0]["periods"][0]
    assert p["low_f"] == 61 and p["valid_from"] is not None


def test_latest_forecasts_groups_per_city(wxdb):
    db = wxdb
    db.write_forecast([{"period": "Tonight", "low_f": 61}], "2026-06-24T12:00:00Z", city="Muncie")
    db.write_forecast([{"period": "Tonight", "low_f": 59},
                       {"period": "Wednesday", "high_f": 84}],
                      "2026-06-24T13:00:00Z", city="Anderson")
    fcs = {f["city"]: f for f in db.latest_forecasts()}
    assert set(fcs) == {"Muncie", "Anderson"}
    assert len(fcs["Anderson"]["periods"]) == 2
    assert fcs["Muncie"]["issued_at"] == "2026-06-24T12:00:00Z"


def test_forecast_confidence_roundtrip(wxdb):
    db = wxdb
    db.write_forecast([{"period": "Tonight", "low_f": 61,
                        "confidence": {"low_f": 0.53}}], "2026-06-24T18:00:00Z")
    p = db.latest_forecasts()[0]["periods"][0]
    assert p["low_f"] == 61 and p["confidence"] == {"low_f": 0.53}
    # a forecast written without confidence round-trips as None (not an error)
    db.write_forecast([{"period": "Tonight", "low_f": 62}], "2026-06-24T19:00:00Z")
    assert db.latest_forecasts()[0]["periods"][0]["confidence"] is None


def test_forecast_history_between_dates(wxdb):
    db = wxdb
    db.write_forecast([{"period": "Tonight", "low_f": 60}], "2026-06-23T12:00:00Z", city="Muncie")
    db.write_forecast([{"period": "Tonight", "low_f": 61}], "2026-06-24T12:00:00Z", city="Muncie")
    h = db.forecast_history("2026-06-24T00:00:00Z", "2026-06-25T00:00:00Z", None)
    assert len(h) == 1 and h[0]["low_f"] == 61


def test_alert_active_then_expires(wxdb):
    db = wxdb
    rec = {"id": "a1", "captured_at": "2026-06-24T06:00:00Z",
           "alert": {"event": "TOR", "event_label": "Tornado Warning", "areas": ["018035"],
                     "counties": ["Delaware County, IN"], "purge_minutes": 45,
                     "issued_raw": "1741830", "station": "KJY93", "raw": "ZCZC-..."}}
    db.write_alert(rec)
    assert len(db.get_active_alerts(now="2026-06-24T06:30:00Z")) == 1
    assert len(db.get_active_alerts(now="2026-06-24T07:00:00Z")) == 0


def test_write_alert_tolerates_missing_purge(wxdb):
    db = wxdb
    # purge_minutes present-but-None must not crash (expires == captured)
    db.write_alert({"id": "a2", "captured_at": "2026-06-24T06:00:00Z",
                    "alert": {"event": "RWT", "purge_minutes": None, "raw": "ZCZC"}})
    assert db.alerts_history_count(None, None, None) == 1


def test_period_window_weekday_night():
    issued = datetime(2026, 6, 24, 12, 0, 0, tzinfo=timezone.utc)
    vf, vt = period_window("Saturday Night", issued)
    assert vf is not None and vt is not None and vf < vt


def test_all_conditions_for_city_and_cities_index(wxdb):
    db = wxdb
    db.record_reading({"city": "Muncie", "condition": "temperature_f", "value": 77},
                      "2026-06-24T12:00:00Z")
    db.record_reading({"city": "Muncie", "condition": "temperature_f", "value": 78},
                      "2026-06-24T12:30:00Z")  # twice -> surfaces
    db.record_reading({"city": "Muncie", "condition": "humidity_pct", "value": 68},
                      "2026-06-24T12:00:00Z")
    db.record_reading({"city": "Muncie", "condition": "humidity_pct", "value": 67},
                      "2026-06-24T12:30:00Z")
    conds = {c["condition"]: c["value"] for c in db.all_conditions_for_city("muncie", 2)}
    assert conds == {"temperature_f": 78, "humidity_pct": 67}
    cities = {c["city"]: c for c in db.cities(2)}
    assert cities["Muncie"]["conditions"] == 2


def test_condition_history_pagination(wxdb):
    db = wxdb
    for i in range(5):
        db.record_reading({"city": "Muncie", "condition": "temperature_f", "value": 70 + i},
                          f"2026-06-24T12:0{i}:00Z")
    assert db.condition_history_count("temperature_f", None, None, None) == 5
    page1 = db.condition_history("temperature_f", None, None, None, limit=2, offset=0)
    page2 = db.condition_history("temperature_f", None, None, None, limit=2, offset=2)
    assert len(page1) == 2 and len(page2) == 2
    assert {r["captured_at"] for r in page1} != {r["captured_at"] for r in page2}


def test_alerts_history_and_since(wxdb):
    db = wxdb
    for i in range(2):
        db.write_alert({"id": f"a{i}", "captured_at": f"2026-06-24T0{i}:00:00Z",
                        "alert": {"event": "SVR", "event_label": "Severe T-storm",
                                  "areas": ["Delaware"], "counties": ["18035"],
                                  "purge_minutes": 10, "raw": "ZCZC"}})
    rows = db.alerts_history(None, None, None, 1, 0)
    assert db.alerts_history_count(None, None, None) == 2 and len(rows) == 1
    assert db.alerts_history_count(None, None, "SVR") == 2
    since = db.alerts_since("2026-06-24T00:30:00Z", 10)
    assert [a["id"] for a in since] == ["a1"]       # strictly after, ascending


def test_observations_and_forecasts_since(wxdb):
    db = wxdb
    db.record_reading({"city": "Muncie", "condition": "temperature_f", "value": 70},
                      "2026-06-24T12:00:00Z")
    db.record_reading({"city": "Muncie", "condition": "temperature_f", "value": 72},
                      "2026-06-24T13:00:00Z")
    obs = db.observations_since("2026-06-24T12:30:00Z", 10)
    assert [o["value"] for o in obs] == [72]
    db.write_forecast([{"period": "Tonight", "low_f": 61}], "2026-06-24T18:00:00Z")
    assert len(db.forecasts_since("2026-06-24T17:00:00Z", 10)) == 1
    assert len(db.forecasts_since("2026-06-24T19:00:00Z", 10)) == 0


def test_almanac_roundtrip_min_sightings_and_since(wxdb):
    db = wxdb
    # numeric field heard twice -> surfaces under min_sightings=2
    db.record_almanac({"field": "precip_year_in", "value": 17.39, "votes": 1, "total": 1},
                      "2026-06-24T06:00:00Z")
    db.record_almanac({"field": "precip_year_in", "value": 17.39, "votes": 2, "total": 2},
                      "2026-06-24T07:00:00Z")
    # text field heard once
    db.record_almanac({"field": "sunrise", "value": "6:14 AM"}, "2026-06-24T06:00:00Z")
    latest = {r["field"]: r for r in db.latest_almanac()}
    assert latest["precip_year_in"]["value"] == 17.39 and latest["precip_year_in"]["sightings"] == 2
    assert latest["sunrise"]["value"] == "6:14 AM"            # text round-trips
    # min_sightings gates the once-heard field out
    gated = {r["field"] for r in db.latest_almanac(min_sightings=2)}
    assert "precip_year_in" in gated and "sunrise" not in gated
    # priming readings + incremental export
    primed = {r["field"]: r["value"] for r in db.latest_almanac_readings()}
    assert primed["precip_year_in"] == 17.39 and primed["sunrise"] == "6:14 AM"
    since = db.almanac_since("2026-06-24T06:30:00Z", 10)
    assert [r["value"] for r in since] == [17.39]             # strictly after, ascending


def _raw(id_, ca, product, text):
    return {"id": id_, "captured_at": ca, "product_type": product,
            "station": "KJY93", "text": text,
            "segments": [{"start_s": 0.0, "end_s": 1.0, "text": text}]}


def test_raw_reports_query_count_since_recent(wxdb):
    db = wxdb
    db._run("TRUNCATE raw_reports")   # clear() is structured-only by design
    for r in [
        _raw("1", "2026-06-24T10:00:00Z", "zone_forecast", "tonight clear"),
        _raw("2", "2026-06-24T11:00:00Z", "current_conditions", "temperature was 70"),
        _raw("3", "2026-06-24T12:00:00Z", "zone_forecast", "highs around 80"),
    ]:
        db.insert_raw_report(r)
    # newest-first; full doc round-trips (segments preserved)
    out = db.query_raw_reports(limit=10)
    assert [r["id"] for r in out] == ["3", "2", "1"]
    assert out[0]["segments"][0]["text"] == "highs around 80"
    # filters: product exact, q substring (case-insensitive)
    assert [r["id"] for r in db.query_raw_reports(product="zone_forecast")] == ["3", "1"]
    assert [r["id"] for r in db.query_raw_reports(q="TEMPERATURE")] == ["2"]
    # from/to inclusive window
    assert [r["id"] for r in db.query_raw_reports(
        frm="2026-06-24T11:00:00Z", to="2026-06-24T12:00:00Z")] == ["3", "2"]
    # pagination via offset
    assert [r["id"] for r in db.query_raw_reports(limit=1, offset=1)] == ["2"]
    # counts honour the same filters
    assert db.count_raw_reports() == 3
    assert db.count_raw_reports(product="zone_forecast") == 2
    # since = strictly-after, ascending
    assert [r["id"] for r in db.raw_reports_since("2026-06-24T10:30:00Z", 10)] == ["2", "3"]
    # recent = last-n, oldest-first; iter = all, ascending
    assert [r["id"] for r in db.recent_raw_reports(2)] == ["2", "3"]
    assert [r["id"] for r in db.iter_raw_reports()] == ["1", "2", "3"]
    # last airing of a product = newest captured_at of that type only
    assert db.last_product_airing("zone_forecast") == "2026-06-24T12:00:00Z"
    assert db.last_product_airing("current_conditions") == "2026-06-24T11:00:00Z"


def test_raw_reports_upsert_in_place(wxdb):
    db = wxdb
    db._run("TRUNCATE raw_reports")   # clear() is structured-only by design
    db.insert_raw_report(_raw("x", "2026-06-24T10:00:00Z", "zone_forecast", "chants of brain"))
    # a term-fix rewrites the same id in place — no duplicate row
    db.insert_raw_report(_raw("x", "2026-06-24T10:00:00Z", "zone_forecast", "chance of rain"))
    rows = db.query_raw_reports()
    assert len(rows) == 1 and rows[0]["text"] == "chance of rain"
    assert db.count_raw_reports(q="chance of rain") == 1


def test_raw_reports_empty_store(wxdb):
    db = wxdb
    db._run("TRUNCATE raw_reports")   # clear() is structured-only by design
    assert db.query_raw_reports() == [] and db.count_raw_reports() == 0
    assert db.raw_reports_since("2026-01-01T00:00:00Z", 5) == []
    assert db.recent_raw_reports(5) == [] and db.iter_raw_reports() == []
    assert db.last_product_airing("zone_forecast") is None


def test_heartbeat_roundtrip_and_upsert(wxdb):
    assert wxdb.read_heartbeat() is None               # no pipeline has written yet
    wxdb.write_heartbeat("KJY93", {"segments": 1, "updated_at": "2026-06-24T12:00:00Z"})
    wxdb.write_heartbeat("KJY93", {"segments": 2, "updated_at": "2026-06-24T12:01:00Z"})
    assert wxdb.read_heartbeat()["segments"] == 2      # upserted in place, newest wins


def _run():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("all db tests passed")


if __name__ == "__main__":
    _run()
