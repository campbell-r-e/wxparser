"""pipeline: the shared transcript -> structured step used by live + reprocess."""

from __future__ import annotations

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


def test_apply_readings_writes_all_and_touches_hb(wxdb):
    db, hb = wxdb, _HB()
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


def _apply(confidence, floor):
    return apply_readings(_TEXT, "2026-06-24T12:00:00Z", CityConditionsAggregator(),
                          ForecastAggregator(), AlmanacAggregator(), None,
                          confidence=confidence, confidence_floor=floor)


def test_apply_readings_low_confidence_is_skipped():
    # a measured confidence below the floor votes nothing (still stored elsewhere)
    s = _apply(confidence=0.3, floor=0.5)
    assert s["low_confidence"] is True
    assert s["readings"] == [] and s["forecast"] is False and s["almanac"] == []


def test_apply_readings_high_confidence_extracts_normally():
    s = _apply(confidence=0.9, floor=0.5)
    assert not s.get("low_confidence")
    assert any(r["value"] == 70 for r in s["readings"]) and s["forecast"] is True


def test_apply_readings_unmeasured_confidence_not_gated():
    # 0.0 == "unmeasured" (pre -ojf transcripts): must NOT be gated, else a full
    # reprocess of old history would drop every legacy reading.
    s = _apply(confidence=0.0, floor=0.5)
    assert not s.get("low_confidence")
    assert any(r["value"] == 70 for r in s["readings"]) and s["forecast"] is True


def test_apply_readings_gate_disabled_by_default():
    # no confidence / floor 0 -> original behaviour, extracts regardless
    s = _apply(confidence=0.1, floor=0.0)
    assert not s.get("low_confidence") and any(r["value"] == 70 for r in s["readings"])


def test_write_alert_detail_paths(wxdb):
    db = wxdb
    assert write_alert_detail_if_any("x", "2026-06-24T12:00:00Z", "r", "p", None) is None  # no DB
    written = write_alert_detail_if_any(
        "A tornado warning remains in effect until 6:30 PM. The storm is moving east at 30 mph.",
        "2026-06-24T12:00:00Z", "r1", "tornado_warning", db)
    assert written is not None                                   # alert product -> written
    assert write_alert_detail_if_any("It was clear.", "2026-06-24T12:00:00Z",
                                     "r2", "current_conditions", db) is None  # not an alert
