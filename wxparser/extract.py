"""Typed field extraction + repeat-voting (PLAN §5 Phase 5, §8).

NWR current-conditions audio is highly templated (one station, the Indianapolis
WFO's phrasing), so regex/grammar extraction is tractable. The catch is STT
mis-hearing numbers ("61" vs "67"); we fix that the cheap way the design calls
for: the same product airs every loop, so we collect many readings per field and
**majority-vote** each one, range-checked. Every field keeps its vote count as a
confidence/provenance signal.
"""

from __future__ import annotations

import re
from collections import Counter, deque
from dataclasses import dataclass

# --- small spoken-number parser (whisper sometimes spells numbers out) ------- #
_UNITS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19,
}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
         "seventy": 70, "eighty": 80, "ninety": 90}


def words_to_int(text: str) -> int | None:
    text = text.lower().replace("-", " ").strip()
    if text.isdigit():
        return int(text)
    total = 0
    found = False
    for tok in text.split():
        if tok in _TENS:
            total += _TENS[tok]; found = True
        elif tok in _UNITS:
            total += _UNITS[tok]; found = True
        else:
            return None
    return total if found else None


_NUM = r"(\d{1,3}|[a-z\- ]+?)"

_RE_TEMP = re.compile(rf"temperature (?:was|is|of) {_NUM} degree", re.I)
_RE_DEW = re.compile(rf"dew\s?point (?:was|is|of) {_NUM}(?: degree)?", re.I)
_RE_HUM = re.compile(rf"relative humidity (?:was |is |of )?{_NUM} ?(?:percent|%)", re.I)
_RE_PRESS = re.compile(r"(?:barometric )?pressure (?:was|is) (\d{2}\.\d{2}) inch", re.I)
_RE_PRESS_TREND = re.compile(r"\b(rising|falling|steady)\b", re.I)
_RE_WIND_CALM = re.compile(r"wind (?:was|is) calm", re.I)
_RE_WIND = re.compile(
    rf"wind (?:was|is) (?:from the )?(north|south|east|west|northeast|northwest|"
    rf"southeast|southwest)(?:erly)? (?:at|around) {_NUM}", re.I)
_RE_SKY = re.compile(
    r"\b(clear|sunny|fair|partly cloudy|mostly cloudy|cloudy|overcast|"
    r"partly sunny|mostly sunny|fog|foggy)\b", re.I)

_FIELD_RANGE = {
    "temperature_f": (-60, 130),
    "dewpoint_f": (-60, 100),
    "humidity_pct": (0, 100),
    "pressure_in": (25.0, 35.0),
    "wind_speed_mph": (0, 120),
}


def _num(s: str) -> int | None:
    return words_to_int(s.strip())


def extract_observation(text: str) -> dict:
    """Return whatever current-conditions fields are present in this text."""
    out: dict = {}
    if (m := _RE_TEMP.search(text)) and (v := _num(m.group(1))) is not None:
        out["temperature_f"] = v
    if (m := _RE_DEW.search(text)) and (v := _num(m.group(1))) is not None:
        out["dewpoint_f"] = v
    if (m := _RE_HUM.search(text)) and (v := _num(m.group(1))) is not None:
        out["humidity_pct"] = v
    if m := _RE_PRESS.search(text):
        out["pressure_in"] = float(m.group(1))
        if t := _RE_PRESS_TREND.search(text[m.end():m.end() + 30]):
            out["pressure_trend"] = t.group(1).lower()
    if _RE_WIND_CALM.search(text):
        out["wind"] = "calm"
        out["wind_speed_mph"] = 0
    elif m := _RE_WIND.search(text):
        spd = _num(m.group(2))
        out["wind"] = f"{m.group(1).lower()} at {spd}" if spd is not None else m.group(1).lower()
        if spd is not None:
            out["wind_speed_mph"] = spd
    if m := _RE_SKY.search(text):
        out["sky"] = m.group(1).lower()
    # range-check numeric fields
    for k, (lo, hi) in _FIELD_RANGE.items():
        if k in out and not (lo <= out[k] <= hi):
            del out[k]
    return out


# --------------------------------------------------------------------------- #
# Zone-forecast extraction (Phase 6)
# --------------------------------------------------------------------------- #
_DAYS = "monday|tuesday|wednesday|thursday|friday|saturday|sunday"
_RE_PERIOD = re.compile(
    rf"^\s*(today|tonight|this afternoon|this evening|this morning|overnight|"
    rf"(?:{_DAYS})(?: night)?|rest of (?:today|tonight))\b", re.I)
_RE_HIGH = re.compile(r"\bhigh[s]?\s+([^.]*?)(?:\.|$)", re.I)
_RE_LOW = re.compile(r"\blow[s]?\s+([^.]*?)(?:\.|$)", re.I)
_RE_PRECIP = re.compile(
    r"chance of (?:rain|precipitation|showers|snow)\s+(\d{1,3})\s*percent", re.I)
_TEMP_OFFSET = {"lower": 1, "low": 1, "mid": 5, "middle": 5, "upper": 8}


def parse_temp_value(phrase: str) -> int | None:
    phrase = phrase.lower()[:30]
    if m := re.search(r"(?:around|near|of)\s+(\d{1,3})", phrase):
        return int(m.group(1))
    if m := re.search(r"(lower|low|mid|middle|upper)\s+(\d{1,3})s", phrase):
        return int(m.group(2)) + _TEMP_OFFSET[m.group(1)]
    if m := re.search(r"\b(\d{1,3})s\b", phrase):
        return int(m.group(1)) + 5  # bare "80s" -> mid
    if m := re.search(r"\b(\d{1,3})\b", phrase):
        return int(m.group(1))
    return None


def extract_forecast_fields(text: str) -> dict:
    """Highs/lows/precip/sky found in a single forecast sentence/segment."""
    out: dict = {}
    if m := _RE_HIGH.search(text):
        if (v := parse_temp_value(m.group(1))) is not None and -60 <= v <= 130:
            out["high_f"] = v
    if m := _RE_LOW.search(text):
        if (v := parse_temp_value(m.group(1))) is not None and -60 <= v <= 100:
            out["low_f"] = v
    if m := _RE_PRECIP.search(text):
        p = int(m.group(1))
        if 0 <= p <= 100:
            out["precip_pct"] = p
    if m := _RE_SKY.search(text):
        out["sky"] = m.group(1).lower()
    return out


def period_header(text: str) -> str | None:
    m = _RE_PERIOD.search(text)
    return m.group(1).strip().title() if m else None


class ForecastAggregator:
    """Builds ordered forecast periods from the in-order segment stream.

    A segment that starts with a period name ("Tonight", "Saturday") opens a
    period; subsequent segments attach highs/lows/precip/sky to the open period
    until the next header. Periods are keyed by name so a fresh airing updates in
    place rather than duplicating.
    """

    def __init__(self):
        self.periods: dict[str, dict] = {}
        self._current: str | None = None

    def update(self, text: str) -> bool:
        changed = False
        if (hdr := period_header(text)) is not None:
            self._current = hdr
            self.periods.setdefault(hdr, {"period": hdr})
            changed = True
        if self._current is None:
            return changed
        fields = extract_forecast_fields(text)
        if fields:
            self.periods[self._current].update(fields)
            changed = True
        return changed

    def snapshot(self) -> list[dict]:
        return list(self.periods.values())


@dataclass
class Voted:
    value: object
    votes: int
    total: int


class _FieldVoter:
    def __init__(self, maxlen: int):
        self.samples: deque = deque(maxlen=maxlen)

    def add(self, value) -> None:
        self.samples.append(value)

    def best(self) -> Voted | None:
        if not self.samples:
            return None
        counts = Counter(self.samples)
        value, votes = counts.most_common(1)[0]
        return Voted(value=value, votes=votes, total=len(self.samples))


class ConditionsAggregator:
    """Accumulates field readings across reports and majority-votes each field."""

    def __init__(self, maxlen: int = 15):
        self.voters: dict[str, _FieldVoter] = {}
        self.maxlen = maxlen
        self._last_snapshot: dict = {}

    def update(self, text: str) -> bool:
        """Feed a transcript; returns True if the voted snapshot changed."""
        fields = extract_observation(text)
        for k, v in fields.items():
            self.voters.setdefault(k, _FieldVoter(self.maxlen)).add(v)
        snap = self.snapshot()
        changed = {k: s["value"] for k, s in snap.items()} != {
            k: s["value"] for k, s in self._last_snapshot.items()
        }
        self._last_snapshot = snap
        return changed and bool(snap)

    def snapshot(self) -> dict:
        """Voted current conditions: field -> {value, votes, total, source}."""
        out: dict = {}
        for k, voter in self.voters.items():
            b = voter.best()
            if b is not None:
                out[k] = {"value": b.value, "votes": b.votes, "total": b.total, "source": "voice"}
        return out
