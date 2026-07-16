"""The shared transcript -> structured-data step.

The live STT worker (main._stt_worker) and the offline reprocess (reprocess.py)
both call these, so the two can never drift in how a transcript becomes structured
data. That's what lets the DB be a re-derivable *projection* of the raw transcript
store: replaying the stored transcripts through the same step rebuilds the same
conditions/forecasts/almanac/alert-details.

Known approximation: the projection converges on the same VALUES, but vote
provenance (votes/total/sightings, hence trust scores) can differ from what the
live path served — live votes before text-dedup, so boundary-shifted repeats
contribute voting events that are never stored and thus never replayed; restart
priming likewise seeds voters with one latest-value sample instead of history.
Also note the pruning/revote maintenance timers (deploy/) edit the serving
tables out of band; a reprocess resurrects whatever they trimmed.
"""
from __future__ import annotations

from dataclasses import dataclass

from .dedup import TextDeduper
from .extract import (
    AlmanacAggregator,
    CityConditionsAggregator,
    ForecastAggregator,
    extract_alert_details,
)
from .store import ALERT_PRODUCTS


@dataclass
class PipelineState:
    """The collaborators one transcript flows through, bundled so the live
    worker, the offline replay, and apply_readings share one small signature
    instead of threading six parallel parameters everywhere.

    `db` and `hb` stay deliberately untyped (anything with the writer /
    heartbeat methods; None disables that leg) so this use-case module never
    imports the gateway or the heartbeat. `deduper` is None on the replay
    path — reprocess replays an already-deduped store.
    """
    aggregator: CityConditionsAggregator
    forecast: ForecastAggregator
    almanac: AlmanacAggregator
    deduper: TextDeduper | None = None
    db: object | None = None
    hb: object | None = None


def apply_readings(text: str, captured_at, state: PipelineState, *,
                   confidence: float | None = None,
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
    projection of the transcript store.
    """
    if (confidence is not None and confidence_floor > 0.0
            and 0.0 < confidence < confidence_floor):
        return {"readings": [], "forecast": False, "almanac": [],
                "low_confidence": True}
    db, hb = state.db, state.hb
    readings = []
    for r in state.aggregator.update(text, captured_at):
        if db is not None:
            db.record_reading(r, captured_at)
        if hb is not None:
            hb.touch("last_extraction_at")
        readings.append(r)
    forecast_written = False
    if state.forecast.update(text):
        if db is not None:
            db.write_forecast(state.forecast.snapshot(), captured_at,
                              city=state.forecast.city)
        if hb is not None:
            hb.touch("last_extraction_at")
        forecast_written = True
    almanac_readings = []
    for r in state.almanac.update(text):
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
    parsed details when written, else None.
    """
    if db is None:
        return None
    details = extract_alert_details(text)
    if details or product_type in ALERT_PRODUCTS:
        db.write_alert_detail(report_id, captured_at, product_type, details, text)
        return details
    return None
