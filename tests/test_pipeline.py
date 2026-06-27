"""pipeline: the shared transcript -> structured step used by live + reprocess."""

from __future__ import annotations

from wxparser.config import CONFIG
from wxparser.db import Database
from wxparser.extract import AlmanacAggregator, CityConditionsAggregator, ForecastAggregator
from wxparser.pipeline import apply_readings, write_alert_detail_if_any

# conditions + forecast + almanac in one transcript, to exercise every branch
_TEXT = ("At Muncie, it was clear. The temperature was 70 degrees. Tonight, clear. "
         "Lows in the lower 60s. Sunrise today is at 6:13 AM and sunset is at 9 PM.")


class _HB:
    def __init__(self):
        self.touched: list[str] = []

    def touch(self, key):
        self.touched.append(key)


def _db():
    db = Database(CONFIG, database="wxparser_test")
    db.clear()
    return db


def test_apply_readings_writes_all_and_touches_hb():
    db, hb = _db(), _HB()
    s = apply_readings(_TEXT, "2026-06-24T12:00:00Z",
                       CityConditionsAggregator(), ForecastAggregator(), AlmanacAggregator(), db, hb)
    assert any(r["condition"] == "temperature_f" and r["value"] == 70 for r in s["readings"])
    assert s["forecast"] is True
    assert {a["field"] for a in s["almanac"]} >= {"sunrise", "sunset"}
    assert "last_extraction_at" in hb.touched
    assert db.all_conditions_for_city("Muncie", 1) and db.latest_forecasts()[0]["periods"]


def test_apply_readings_db_none_still_extracts():
    # no DB / no heartbeat: extraction still runs and returns the readings
    s = apply_readings(_TEXT, "2026-06-24T12:00:00Z",
                       CityConditionsAggregator(), ForecastAggregator(), AlmanacAggregator(), None)
    assert any(r["value"] == 70 for r in s["readings"]) and s["forecast"] is True and s["almanac"]


def test_write_alert_detail_paths():
    db = _db()
    assert write_alert_detail_if_any("x", "2026-06-24T12:00:00Z", "r", "p", None) is None  # no DB
    written = write_alert_detail_if_any(
        "A tornado warning remains in effect until 6:30 PM. The storm is moving east at 30 mph.",
        "2026-06-24T12:00:00Z", "r1", "tornado_warning", db)
    assert written is not None                                   # alert product -> written
    assert write_alert_detail_if_any("It was clear.", "2026-06-24T12:00:00Z",
                                     "r2", "current_conditions", db) is None  # not an alert
