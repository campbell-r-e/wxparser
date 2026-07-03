"""The shared transcript -> structured-data step.

The live STT worker (main._stt_worker) and the offline reprocess (reprocess.py)
both call these, so the two can never drift in how a transcript becomes structured
data. That's what lets the DB be a faithful, re-derivable *projection* of the raw
transcript store: replaying the stored transcripts through the same step rebuilds
the same conditions/forecasts/almanac/alert-details.
"""
from __future__ import annotations

from .extract import (
    AlmanacAggregator,
    CityConditionsAggregator,
    ForecastAggregator,
    extract_alert_details,
)
from .store import ALERT_PRODUCTS


def apply_readings(text: str, captured_at, aggregator: CityConditionsAggregator,
                   forecast: ForecastAggregator, almanac: AlmanacAggregator,
                   db, hb=None, *, confidence: float | None = None,
                   confidence_floor: float = 0.0) -> dict:
    """Vote this transcript's conditions/forecast/almanac into the aggregators and
    persist the results, stamped with captured_at. Returns a summary for logging.

    When confidence_floor > 0 and this transcript's measured STT confidence falls
    below it, the values are skipped entirely (returned summary flags
    `low_confidence`) so a mangled reading can't sway the aggregates — the raw
    transcript is still stored by the caller, just not voted. A confidence of
    exactly 0.0 means "unmeasured" (pre -ojf transcripts) and is never gated, so
    replaying old history through reprocess isn't wiped. Both the live worker and
    reprocess pass the same (confidence, floor), keeping the DB a faithful
    projection of the transcript store."""
    if (confidence is not None and confidence_floor > 0.0
            and 0.0 < confidence < confidence_floor):
        return {"readings": [], "forecast": False, "almanac": [],
                "low_confidence": True}
    readings = []
    for r in aggregator.update(text):
        if db is not None:
            db.record_reading(r, captured_at)
        if hb is not None:
            hb.touch("last_extraction_at")
        readings.append(r)
    forecast_written = False
    if forecast.update(text):
        if db is not None:
            db.write_forecast(forecast.snapshot(), captured_at, city=forecast.city)
        if hb is not None:
            hb.touch("last_extraction_at")
        forecast_written = True
    almanac_readings = []
    for r in almanac.update(text):
        if db is not None:
            db.record_almanac(r, captured_at)
        if hb is not None:
            hb.touch("last_extraction_at")
        almanac_readings.append(r)
    return {"readings": readings, "forecast": forecast_written, "almanac": almanac_readings}


def write_alert_detail_if_any(text: str, captured_at, report_id: str,
                              product_type: str | None, db) -> dict | None:
    """Structure the spoken narrative of a warning/statement and persist it keyed by
    report_id (so it can be linked to the SAME header at query time). Returns the
    parsed details when written, else None."""
    if db is None:
        return None
    details = extract_alert_details(text)
    if details or product_type in ALERT_PRODUCTS:
        db.write_alert_detail(report_id, captured_at, product_type, details, text)
        return details
    return None
