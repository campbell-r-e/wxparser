"""store.py: product classification and report building.

The raw transcript query/sync readers moved to db.raw_reports (see test_db.py's
raw_reports round-trip tests); this module now owns only report *shape*.
"""

from __future__ import annotations

from wxparser.config import Config
from wxparser.store import build_report, classify
from wxparser.stt import TranscriptSegment, Transcript


def _t(text: str) -> Transcript:
    return Transcript(text=text, segments=[TranscriptSegment(0.0, 1.0, text)], language="en")


def test_classify_explicit_products():
    assert classify("Tornado warning for Delaware County") == "tornado_warning"
    assert classify("Hazardous weather outlook for central Indiana") == "hazardous_weather_outlook"


def test_classify_conditions_forecast_unknown():
    assert classify("At Muncie, the temperature was 70 degrees.") == "current_conditions"
    assert classify("It was 74 at Marion and 72 at Anderson.") == "current_conditions"
    assert classify("Tonight, mostly cloudy. Lows in the lower 60s.") == "zone_forecast"
    assert classify("A slight chance of showers and thunderstorms.") == "zone_forecast"
    assert classify("This is KJY93 Muncie all hazards radio.") == "unknown"


def test_classify_almanac():
    assert classify("Sunrise today is at 6.13 AM and sunset is at 9.15 PM.") == "almanac"
    assert classify("The total precipitation for the year now stands at 17.39 inches, "
                    "which is 2.72 inches below normal.") == "almanac"


def test_build_report_fields():
    cfg = Config()
    r = build_report(_t("Highs around 80."), cfg, duration_s=12.34, fingerprint="abc",
                     captured_at="2026-06-24T12:00:00Z")
    assert r["id"].startswith("2026-06-24T12:00:00Z-")
    assert r["product_type"] == "zone_forecast"
    assert r["duration_s"] == 12.3 and r["fingerprint"] == "abc"
    assert r["text"] == "Highs around 80." and r["station"] == cfg.station
    assert r["segments"][0]["text"] == "Highs around 80."
