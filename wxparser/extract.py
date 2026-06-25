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

from .data.place_names import correct_place
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
_SKY_WORDS = (
    r"clear|sunny|fair|partly cloudy|mostly cloudy|cloudy|overcast|"
    r"partly sunny|mostly sunny|fog|foggy")
_RE_SKY = re.compile(rf"\b({_SKY_WORDS})\b", re.I)
# Current-conditions sky requires the observation framing ("it was clear"), so
# forecast sky phrases ("Saturday, partly cloudy") don't pollute /current.
_RE_COND_SKY = re.compile(
    rf"(?:it (?:was|is)|currently|skies? (?:were|was|are|is)) ({_SKY_WORDS})\b", re.I)

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
    if m := _RE_COND_SKY.search(text):
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

    def __init__(self, area: str = "Muncie"):
        self.periods: dict[str, dict] = {}
        self._current: str | None = None
        self.city: str = area  # area the current forecast covers ("...for the X area")

    def prime(self, periods: list[dict]) -> None:
        """Restore periods from a stored forecast so a restart keeps /forecast."""
        for p in periods:
            name = p.get("period")
            if not name:
                continue
            entry = {"period": name}
            for k in ("high_f", "low_f", "precip_pct", "sky"):
                if p.get(k) is not None:
                    entry[k] = p[k]
            self.periods[name] = entry

    def update(self, text: str) -> bool:
        changed = False
        if m := _RE_FC_AREA.search(text):
            self.city = _norm_city(m.group(1))
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


# --------------------------------------------------------------------------- #
# Generic multi-city extraction (Phase 6+, city-agnostic)
# --------------------------------------------------------------------------- #
_CITY = r"[A-Z][a-z]+(?:\s[A-Z][a-z]+)?"
# "At Muncie, it was clear." / "At Muncie, the temperature was ..." -> primary city
_RE_CITY_HEADER = re.compile(
    rf"\bat ({_CITY})\s*,?\s+(?:it (?:was|is)|the (?:temperature|sky|relative|"
    rf"barometric|wind|dew))", re.I)
# "... 56 at Anderson, 63 at Portland ..." -> per-city temperatures
_RE_NEARBY = re.compile(rf"(-?\d{{2,3}})\s+(?:degrees?\s+)?at\s+({_CITY})")
# "... forecast for the Muncie area ..." -> forecast area name (case-sensitive city,
# literal " area" suffix so the city group can't swallow the word "area")
_RE_FC_AREA = re.compile(rf"[Ff]orecast for (?:the )?({_CITY})\s+area")
# Climate-summary / almanac recaps quote PAST or normal values, not live
# conditions: "Yesterday's low temperature was 55 degrees", "normal high is 85",
# "record low ...". Their "(high|low) temperature was N degrees" is a substring
# match for _RE_TEMP, so without this guard they get ingested as the primary
# city's *current* temperature (e.g. Muncie current temp wrongly set to 55).
_RE_RECAP = re.compile(
    r"\b(climate summary|yesterday|normal (?:high|low)|record (?:high|low)"
    r"|(?:high|low) temperature was|degree days?)\b", re.I)


def _norm_city(name: str) -> str:
    # title-case, then fold known STT mis-hearings to the canonical spelling so
    # the store only ever sees correct city names (no nightly cleanup needed).
    return correct_place(name.strip().title())


class CityConditionsAggregator:
    """City-agnostic current-conditions extraction with per-(city,condition) voting.

    A "At <City>, it was ..." header sets the active city; the condition sentences
    that follow (temperature/humidity/pressure/wind/sky) attach to it. A "Nearby
    ... <temp> at <City>" list yields a temperature per named city. Each
    (city, condition) is majority-voted independently.
    """

    def __init__(self, maxlen: int = 15, primary_city: str = "Muncie"):
        self.maxlen = maxlen
        self.primary_city = primary_city
        self.voters: dict[tuple[str, str], _FieldVoter] = {}

    def update(self, text: str) -> list[dict]:
        """Returns every (city, condition) reading heard in this text, with the
        current voted value/votes/total. Each is a 'sighting' the store counts."""
        readings: list[dict] = []
        nearby = [(int(v), _norm_city(c)) for v, c in _RE_NEARBY.findall(text)]
        if nearby:
            # a nearby list: the temps belong to the named cities
            for val, city in nearby:
                if -60 <= val <= 130:
                    readings.append(self._reading(city, "temperature_f", val))
            return readings
        # standalone conditions ("the temperature was N", no "at <City>") are always
        # the station's home city; attribute them there rather than to a possibly
        # mis-heard header city ("At Monthsy ...").
        # ...but skip climate-summary/almanac recaps — those quote yesterday's or
        # normal highs/lows, not live conditions, and would poison the primary
        # city's current readings.
        if _RE_RECAP.search(text):
            return readings
        for cond, val in extract_observation(text).items():
            readings.append(self._reading(self.primary_city, cond, val))
        return readings

    def _reading(self, city: str, condition: str, value) -> dict:
        voter = self.voters.setdefault((city, condition), _FieldVoter(self.maxlen))
        voter.add(value)
        best = voter.best()
        return {"city": city, "condition": condition,
                "value": best.value, "votes": best.votes, "total": best.total}

    def prime(self, readings: list[dict]) -> None:
        """Seed voters from stored latest readings so a restart keeps state."""
        for r in readings:
            self.voters.setdefault((r["city"], r["condition"]), _FieldVoter(self.maxlen)).add(r["value"])


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
        top = max(counts.values())
        tied = {v for v, c in counts.items() if c == top}
        # break ties toward the most recent reading (conditions change over time)
        value = next(v for v in reversed(self.samples) if v in tied)
        return Voted(value=value, votes=top, total=len(self.samples))


class ConditionsAggregator:
    """Accumulates field readings across reports and majority-votes each field."""

    def __init__(self, maxlen: int = 15):
        self.voters: dict[str, _FieldVoter] = {}
        self.maxlen = maxlen
        self._last_snapshot: dict = {}

    def prime(self, fields: dict) -> None:
        """Seed voters from a stored observation snapshot (field -> {value,...}).

        Lets a restart keep showing last-known conditions until fresh readings
        arrive; live readings then vote on top and age the primed value out.
        """
        for field, info in fields.items():
            val = info.get("value") if isinstance(info, dict) else info
            if val is not None:
                self.voters.setdefault(field, _FieldVoter(self.maxlen)).add(val)
        self._last_snapshot = self.snapshot()

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


# --------------------------------------------------------------------------- #
# Spoken warning / statement narrative -> structured details (Phase 7)
# --------------------------------------------------------------------------- #
# The SAME digital header gives the event type, counties, and expiry. The spoken
# narrative that follows carries the rest — when it expires in plain language,
# where the threat is, how it's moving, what the hazard is, and whether spotters
# are activated. These are STT-transcribed, so place names may be garbled; the
# numbers/times/keywords survive well and are what we pull here.
_TIME = r"\d{1,2}(?::\d{2})?\s*(?:[ap]\.?\s?m\.?)|\d{3,4}\s*(?:[ap]\.?\s?m\.?)|noon|midnight"
_RE_UNTIL = re.compile(rf"until\s+({_TIME})", re.I)
_DIR = r"north|south|east|west|northeast|northwest|southeast|southwest"
_RE_MOTION = re.compile(
    rf"\bmov(?:ing|ed)\s+(?:to\s+the\s+)?({_DIR})\s+at\s+(\d{{1,3}})\s*(?:mph|miles per hour)",
    re.I)
_RE_TORNADO = re.compile(r"\btornado(?:es)?\b", re.I)
_RE_FLASH_FLOOD = re.compile(r"\bflash\s+flood", re.I)
_RE_HAIL = re.compile(
    r"(\d(?:\.\d+)?)\s*inch(?:es)?\s*(?:hail|in diameter)"
    r"|hail\s*(?:up to\s*|of\s*)?(\d(?:\.\d+)?)\s*inch"
    r"|(quarter|nickel|penny|ping[\s-]?pong|golf[\s-]?ball|half[\s-]?dollar|"
    r"tennis[\s-]?ball|baseball)\s*[- ]?siz", re.I)
_RE_WIND = re.compile(
    r"(?:winds?|gusts?)\s*(?:up to|of|to|near|around)?\s*(\d{2,3})\s*(?:mph|miles per hour)",
    re.I)
# "near Yorktown", "over the Muncie area", "approaching Albany" — place after a
# locator preposition. STT may garble the name, but capturing it is still useful.
_RE_NEAR = re.compile(rf"\b(?:near|over|approaching|just (?:north|south|east|west) of|from)\s+(?:the\s+)?({_CITY})")
_RE_SPOTTER = re.compile(
    r"spotter activation|weather spotters?|spotters?\s+(?:are\s+|should\s+be\s+)?"
    r"(?:needed|activated|encouraged|in the area|on alert)|report(?:ing)? (?:any )?severe weather",
    re.I)


def extract_alert_details(text: str) -> dict:
    """Pull structured fields from a spoken warning/statement transcript.

    Returns {} when nothing alert-like is found, so callers can cheaply tell a
    real narrative from a routine segment.
    """
    d: dict = {}
    if m := _RE_UNTIL.search(text):
        d["until"] = re.sub(r"\s+", " ", m.group(1).strip()).rstrip(" .")
    if m := _RE_MOTION.search(text):
        d["motion"] = {"direction": m.group(1).lower(), "mph": int(m.group(2))}

    threats: list[str] = []
    if _RE_TORNADO.search(text):
        threats.append("tornado")
    if m := _RE_HAIL.search(text):
        size = next((g for g in m.groups() if g), "")
        threats.append(f"hail {size.strip()}".strip() + ("in" if re.match(r"^\d", size) else ""))
    if m := _RE_WIND.search(text):
        threats.append(f"wind {m.group(1)}mph")
    if _RE_FLASH_FLOOD.search(text):
        threats.append("flash flood")
    if threats:
        d["threats"] = threats

    locs: list[str] = []
    for c in _RE_NEAR.findall(text):
        c = _norm_city(c)
        if c not in locs:
            locs.append(c)
    if locs:
        d["locations"] = locs

    if _RE_SPOTTER.search(text):
        d["spotter_activation"] = True
    return d
