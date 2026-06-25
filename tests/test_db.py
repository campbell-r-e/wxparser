"""PostgreSQL store round-trip tests (generic city/condition schema)."""

from __future__ import annotations

from datetime import datetime, timezone

from wxparser.config import CONFIG
from wxparser.db import Database, period_window


def _db() -> Database:
    db = Database(CONFIG, database="wxparser_test")
    db.clear()
    return db


def test_city_conditions_latest():
    db = _db()
    db.record_reading({"city": "Muncie", "condition": "temperature_f", "value": 61,
                       "votes": 3, "total": 4}, "2026-06-24T06:00:00Z")
    db.record_reading({"city": "Anderson", "condition": "temperature_f", "value": 56,
                       "votes": 1, "total": 1}, "2026-06-24T06:01:00Z")
    by = {r["city"]: r["value"] for r in db.latest_for_condition("temperature_f")}
    assert by["Muncie"] == 61 and by["Anderson"] == 56


def test_min_sightings_filter():
    db = _db()
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


def test_text_condition_roundtrip():
    db = _db()
    db.record_reading({"city": "Muncie", "condition": "sky", "value": "clear"},
                      "2026-06-24T06:00:00Z")
    rows = db.latest_for_condition("sky")
    assert rows[0]["city"] == "Muncie" and rows[0]["value"] == "clear"


def test_condition_history_between_times():
    db = _db()
    db.record_reading({"city": "Muncie", "condition": "temperature_f", "value": 61},
                      "2026-06-24T06:00:00Z")
    db.record_reading({"city": "Muncie", "condition": "temperature_f", "value": 63},
                      "2026-06-24T07:00:00Z")
    h = db.condition_history("temperature_f", "Muncie",
                             "2026-06-24T06:30:00Z", "2026-06-24T07:30:00Z")
    assert len(h) == 1 and h[0]["value"] == 63


def test_forecast_city_and_latest():
    db = _db()
    db.write_forecast([{"period": "Tonight", "low_f": 61, "precip_pct": 70, "sky": "partly cloudy"}],
                      "2026-06-24T12:00:00Z", city="Muncie")
    fcs = db.latest_forecasts()
    assert fcs[0]["city"] == "Muncie"
    p = fcs[0]["periods"][0]
    assert p["low_f"] == 61 and p["valid_from"] is not None


def test_forecast_history_between_dates():
    db = _db()
    db.write_forecast([{"period": "Tonight", "low_f": 60}], "2026-06-23T12:00:00Z", city="Muncie")
    db.write_forecast([{"period": "Tonight", "low_f": 61}], "2026-06-24T12:00:00Z", city="Muncie")
    h = db.forecast_history("2026-06-24T00:00:00Z", "2026-06-25T00:00:00Z", None)
    assert len(h) == 1 and h[0]["low_f"] == 61


def test_alert_active_then_expires():
    db = _db()
    rec = {"id": "a1", "captured_at": "2026-06-24T06:00:00Z",
           "alert": {"event": "TOR", "event_label": "Tornado Warning", "areas": ["018035"],
                     "counties": ["Delaware County, IN"], "purge_minutes": 45,
                     "issued_raw": "1741830", "station": "KJY93", "raw": "ZCZC-..."}}
    db.write_alert(rec)
    assert len(db.get_active_alerts(now="2026-06-24T06:30:00Z")) == 1
    assert len(db.get_active_alerts(now="2026-06-24T07:00:00Z")) == 0


def test_period_window_weekday_night():
    issued = datetime(2026, 6, 24, 12, 0, 0, tzinfo=timezone.utc)
    vf, vt = period_window("Saturday Night", issued)
    assert vf is not None and vt is not None and vf < vt


def test_all_conditions_for_city_and_cities_index():
    db = _db()
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


def test_condition_history_pagination():
    db = _db()
    for i in range(5):
        db.record_reading({"city": "Muncie", "condition": "temperature_f", "value": 70 + i},
                          f"2026-06-24T12:0{i}:00Z")
    assert db.condition_history_count("temperature_f", None, None, None) == 5
    page1 = db.condition_history("temperature_f", None, None, None, limit=2, offset=0)
    page2 = db.condition_history("temperature_f", None, None, None, limit=2, offset=2)
    assert len(page1) == 2 and len(page2) == 2
    assert {r["captured_at"] for r in page1} != {r["captured_at"] for r in page2}


def test_alerts_history_and_since():
    db = _db()
    for i in range(2):
        db.write_alert({"id": f"a{i}", "captured_at": f"2026-06-24T0{i}:00:00Z",
                        "alert": {"event": "SVR", "event_label": "Severe T-storm",
                                  "areas": ["Delaware"], "counties": ["18035"],
                                  "purge_minutes": 10, "raw": "ZCZC"}})
    total, rows = db.alerts_history(None, None, None, 1, 0)
    assert total == 2 and len(rows) == 1            # paginated, total reported
    assert db.alerts_history(None, None, "SVR", 10, 0)[0] == 2
    since = db.alerts_since("2026-06-24T00:30:00Z", 10)
    assert [a["id"] for a in since] == ["a1"]       # strictly after, ascending


def test_observations_and_forecasts_since():
    db = _db()
    db.record_reading({"city": "Muncie", "condition": "temperature_f", "value": 70},
                      "2026-06-24T12:00:00Z")
    db.record_reading({"city": "Muncie", "condition": "temperature_f", "value": 72},
                      "2026-06-24T13:00:00Z")
    obs = db.observations_since("2026-06-24T12:30:00Z", 10)
    assert [o["value"] for o in obs] == [72]
    db.write_forecast([{"period": "Tonight", "low_f": 61}], "2026-06-24T18:00:00Z")
    assert len(db.forecasts_since("2026-06-24T17:00:00Z", 10)) == 1
    assert len(db.forecasts_since("2026-06-24T19:00:00Z", 10)) == 0


def _run():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("all db tests passed")


if __name__ == "__main__":
    _run()
