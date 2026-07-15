"""Forecast verification over the full stored record (the /verify endpoint).

Scores every field the zone forecast promises against what the station
subsequently observed, from the first stored reading to now:

  - highs/lows: each stored issuance's high_f/low_f vs the observed extreme in
    the period's local-time window (day 06:00-21:00, night 18:00-08:30 next
    morning — local wall-clock, sidestepping the UTC-hour period_window quirk)
  - sky: forecast wording vs the modal observed wording, both folded onto a
    4-step cloudiness ladder (clear -> partly -> mostly -> cloudy)
  - chance of rain: a daily rain series is recovered by differencing the
    almanac's year-to-date precipitation day over day, then each rain-day's
    final day-before PoP is Brier-scored against the outcome

Windows/extremes come straight from the DB readers, so every request reflects
the whole record. Stdlib only — the API serves this without pandas.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .config import Config
from .db import Database

# spoken sky wordings folded onto one ordinal cloudiness ladder
LADDER = {"clear": 0, "sunny": 0, "fair": 0, "mostly sunny": 1, "partly sunny": 1,
          "partly cloudy": 1, "mostly cloudy": 2, "cloudy": 3, "overcast": 3}
_MIN_OBS = 6    # window readings required to trust a temperature extreme
_MIN_SKY = 3    # window readings required to trust a modal sky
_WET_IN = 0.005  # YTD diffs are spoken to 0.01 in; anything above this is rain
_EVERYTHING = 10_000_000   # reader limit that means "the whole record"


def _utc(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _by_day(readings: list[dict], tz: ZoneInfo) -> dict[date, list[tuple[datetime, object]]]:
    """Readings bucketed by local calendar day, as (local_dt, value) pairs."""
    out: dict[date, list] = defaultdict(list)
    for r in readings:
        local = _utc(r["captured_at"]).astimezone(tz)
        out[local.date()].append((local, r["value"]))
    return out


def _window(byday: dict, day: date, h0: float, h1: float, tz: ZoneInfo) -> list:
    """Values captured between day+h0 hours and day+h1 hours, local wall-clock."""
    t0 = datetime.combine(day, time(), tz) + timedelta(hours=h0)
    t1 = datetime.combine(day, time(), tz) + timedelta(hours=h1)
    vals = []
    d = day
    while d <= t1.date():
        for ts, v in byday.get(d, ()):
            if t0 <= ts <= t1:
                vals.append(v)
        d += timedelta(days=1)
    return vals


def _is_night(period: str) -> bool:
    # every night-side period name contains "night" (Tonight, Overnight,
    # <Weekday> Night, Rest Of Tonight)
    return "night" in period.lower()


def _stats(errs: list[float]) -> dict:
    if not errs:
        return {"n": 0, "bias_f": None, "mae_f": None}
    return {"n": len(errs),
            "bias_f": round(sum(errs) / len(errs), 2),
            "mae_f": round(sum(abs(e) for e in errs) / len(errs), 2)}


def daily_rain(db: Database, tz: ZoneInfo) -> dict[date, float]:
    """Daily rainfall recovered from consecutive-day YTD-precip diffs. A day
    without a prior-day reading can't be differenced; a negative diff is an
    STT mishear and is dropped."""
    ytd: dict[date, float] = {}
    for r in db.almanac_since("1970-01-01T00:00:00Z", _EVERYTHING):
        if r["field"] == "precip_year_in":
            ytd[_utc(r["captured_at"]).astimezone(tz).date()] = float(r["value"])
    out: dict[date, float] = {}
    for d, v in ytd.items():
        prev = ytd.get(d - timedelta(days=1))
        if prev is not None and v >= prev:
            out[d] = round(v - prev, 2)
    return out


def verify(db: Database, cfg: Config) -> dict:
    """The full verification document (see module docstring for methodology)."""
    tz = ZoneInfo(cfg.station_tz)
    city = cfg.primary_city
    temps = _by_day(db.condition_history("temperature_f", city, None, None,
                                         _EVERYTHING), tz)
    skies = _by_day(db.condition_history("sky", city, None, None, _EVERYTHING), tz)
    fcs = db.forecast_history(None, None, city, _EVERYTHING)

    hi_err: list[float] = []
    lo_err: list[float] = []
    mae_by_lead: dict[int, list[float]] = defaultdict(list)
    sky_n = sky_exact = sky_close = 0
    # the latest day-before PoP per target day, for the rain scorecard
    pop_before: dict[date, tuple[datetime, float]] = {}

    for f in fcs:
        if not f["valid_from"]:
            continue
        day = _utc(f["valid_from"]).astimezone(tz).date()
        issued = _utc(f["issued_at"]).astimezone(tz)
        lead = (day - issued.date()).days
        if lead < 0 or lead > 6:
            continue
        night = _is_night(f["period"])
        if f["high_f"] is not None and not night:
            w = _window(temps, day, 6, 21, tz)
            if len(w) >= _MIN_OBS:
                err = f["high_f"] - max(w)
                hi_err.append(err)
                mae_by_lead[lead].append(abs(err))
        if f["low_f"] is not None and night:
            w = _window(temps, day, 18, 32.5, tz)
            if len(w) >= _MIN_OBS:
                err = f["low_f"] - min(w)
                lo_err.append(err)
                mae_by_lead[lead].append(abs(err))
        if f["sky"] in LADDER:
            w = [LADDER[v] for v in
                 _window(skies, day, *((18, 30) if night else (8, 20)), tz)
                 if v in LADDER]
            if len(w) >= _MIN_SKY:
                modal = max(set(w), key=w.count)
                dist = abs(LADDER[f["sky"]] - modal)
                sky_n += 1
                sky_exact += dist == 0
                sky_close += dist <= 1
        if f["precip_pct"] is not None and lead == 1:
            best = pop_before.get(day)
            if best is None or issued > best[0]:
                pop_before[day] = (issued, float(f["precip_pct"]))

    rain = daily_rain(db, tz)
    scorecard = [{"day": d.isoformat(), "pop_day_before": pop_before[d][1],
                  "rained": rain[d] > _WET_IN, "inches": rain[d]}
                 for d in sorted(rain) if d in pop_before]
    brier = base = skill = None
    if scorecard:
        probs = [s["pop_day_before"] / 100 for s in scorecard]
        outcomes = [1.0 if s["rained"] else 0.0 for s in scorecard]
        brier = sum((p - o) ** 2 for p, o in zip(probs, outcomes)) / len(probs)
        base = sum(outcomes) / len(outcomes)
        ref = sum((base - o) ** 2 for o in outcomes) / len(outcomes)
        skill = round(1 - brier / ref, 3) if ref else None
        brier, base = round(brier, 3), round(base, 3)

    return {
        "city": city, "tz": cfg.station_tz,
        "temperature": {
            "high": _stats(hi_err), "low": _stats(lo_err),
            "mae_by_lead_days": {str(k): round(sum(v) / len(v), 2)
                                 for k, v in sorted(mae_by_lead.items())},
        },
        "sky": {"n": sky_n,
                "exact_pct": round(100 * sky_exact / sky_n, 1) if sky_n else None,
                "within_one_step_pct": round(100 * sky_close / sky_n, 1) if sky_n else None},
        "rain": {"days_measured": len(rain),
                 "wet_days": sum(1 for v in rain.values() if v > _WET_IN),
                 "total_in": round(sum(rain.values()), 2),
                 "brier_day_before": brier, "base_rate": base,
                 "brier_skill": skill, "scorecard": scorecard},
    }
