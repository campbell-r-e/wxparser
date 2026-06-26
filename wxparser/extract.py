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

# "temperature was/is/of N degrees" and the recap form "it was N degrees" (the
# 1 p.m. ob's "...it Muncie, it was 76 degrees..."). The trailing "degree" keeps
# "it was mostly sunny" from matching, and extract_observation only runs on the
# home-station block, so a nearby/forecast temp can't slip in as the primary.
_RE_TEMP = re.compile(rf"(?:temperature (?:was|is|of)|it (?:was|is)) {_NUM} degree", re.I)
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
# Period names as they appear *anywhere* in the narrative.
_PERIOD_NAME = (
    rf"this (?:afternoon|evening|morning)|tonight|today|overnight"
    rf"|rest of (?:today|tonight)|(?:{_DAYS})(?:\s+night)?")
# A period *header* is the name at a clause boundary (segment start, after a
# ". "/", " or "for "/"forecast for ... area"). VAD chops the forecast narrative
# mid-stream and prefixes lead-ins ("And now we look at the forecast for the
# Muncie area. This afternoon, ..."), so headers are almost never at the literal
# segment start, and one segment routinely spans several periods ("...Saturday
# night. ... lows ..., Sunday, partly cloudy ..."). Splitting on these lets each
# period own only the fields stated in its own span.
_RE_PERIOD_HDR = re.compile(
    rf"(?:^|[.,]\s+|\bfor\s+)({_PERIOD_NAME})\b(?=[\s,.]|$)", re.I)
# Climate outlook / almanac — NOT a daily forecast ("8 to 14 day outlook ...
# temperatures above normal", "normal high is 85"). Parsing it as periods invents
# bogus far-future days and phantom highs, so skip the whole segment. NB: match
# on "outlook"/"normal", NOT on a "N-day" range — the legit *3-7 day forecast*
# (real Saturday/Sunday highs/lows) also says "3 to 7 day".
_RE_FC_OUTLOOK = re.compile(
    r"\b(?:outlook|(?:above|below|near)\s+normal|normal\s+(?:high|low)"
    r"|climate|degree days?)\b", re.I)
_RE_HIGH = re.compile(r"\bhigh[s]?\s+([^.]*?)(?:\.|$)", re.I)
_RE_LOW = re.compile(r"\blow[s]?\s+([^.]*?)(?:\.|$)", re.I)
# "near steady temperature in the upper 70s" / "temperature near 80" — a period's
# representative temp stated without the word "high"/"low" (the aggregator routes
# it to the high on a day period, the low on a night period).
_RE_FC_TEMP = re.compile(
    r"temperature[s]?\s+(?:near steady\s+|steady\s+|remaining\s+)?"
    r"(?:in the|near|around)\s+([^.]*?)(?:\.|$)", re.I)
# precip: accept a comma ("chance of rain, 80%") and a spelled-out number
# ("chance of rain eighty percent"), not just "<digits> percent".
_RE_PRECIP = re.compile(
    r"chance of (?:rain|precipitation|showers|snow)[\s,]+"
    r"(\d{1,3}|[a-z\- ]+?)\s*(?:%|percent)", re.I)
_TEMP_OFFSET = {"lower": 1, "low": 1, "mid": 5, "middle": 5, "upper": 8}


_DECADE_WORDS = {
    "twenties": 20, "thirties": 30, "forties": 40, "fifties": 50,
    "sixties": 60, "seventies": 70, "eighties": 80, "nineties": 90,
    # recurring STT garbles of the decade words, always in a "highs in the
    # lower <X>" context so they're safe to fold to the intended decade.
    "naddies": 90, "netties": 90, "negies": 90, "nadies": 90, "naughties": 90,
    "naggies": 90, "aidies": 80, "aighties": 80, "eddies": 80, "adias": 80,
}
_RE_DECADE_WORD = "|".join(_DECADE_WORDS)


def parse_temp_value(phrase: str) -> int | None:
    phrase = phrase.lower()[:30]
    if m := re.search(r"(?:around|near|of)\s+(\d{1,3})", phrase):
        return int(m.group(1))
    if m := re.search(r"(lower|low|mid|middle|upper)\s+(\d{1,3})s", phrase):
        return int(m.group(2)) + _TEMP_OFFSET[m.group(1)]
    # spelled-out decades: STT renders "lower 60s" as "lower sixties" about as
    # often as the digit form, so the lows/highs go missing without this.
    if m := re.search(rf"(lower|low|mid|middle|upper)\s+({_RE_DECADE_WORD})", phrase):
        return _DECADE_WORDS[m.group(2)] + _TEMP_OFFSET[m.group(1)]
    if m := re.search(rf"\b({_RE_DECADE_WORD})\b", phrase):
        return _DECADE_WORDS[m.group(1)] + 5  # bare "sixties" -> mid
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
    # representative temp stated without "high"/"low" — routed by the aggregator.
    if "high_f" not in out and "low_f" not in out and (m := _RE_FC_TEMP.search(text)):
        if (v := parse_temp_value(m.group(1))) is not None and -60 <= v <= 130:
            out["steady_f"] = v
    if m := _RE_PRECIP.search(text):
        p = words_to_int(m.group(1))  # handles "80" and "eighty"
        if p is not None and 0 <= p <= 100:
            out["precip_pct"] = p
    if m := _RE_SKY.search(text):
        out["sky"] = m.group(1).lower()
    return out


def period_header(text: str) -> str | None:
    m = _RE_PERIOD.search(text)
    return m.group(1).strip().title() if m else None


def _is_night_period(name: str) -> bool:
    n = name.lower()
    return n in ("tonight", "overnight", "rest of tonight") or n.endswith(" night")


class ForecastAggregator:
    """Builds ordered forecast periods from the in-order segment stream.

    A segment that starts with a period name ("Tonight", "Saturday") opens a
    period; subsequent segments attach highs/lows/precip/sky to the open period
    until the next header. Each (period, field) is **majority-voted** over a
    rolling window (like the conditions aggregator) so a single STT-garbled airing
    can't overwrite the consensus — yet a genuine NWS revision still wins once it
    dominates recent airings.
    """

    _FIELDS = ("high_f", "low_f", "precip_pct", "sky")

    def __init__(self, area: str = "Muncie", maxlen: int = 15):
        self.maxlen = maxlen
        self.order: list[str] = []   # period names in first-seen order
        self.voters: dict[tuple[str, str], _FieldVoter] = {}
        self._current: str | None = None
        self.city: str = area  # area the current forecast covers ("...for the X area")

    def _vote(self, period: str, field: str, value) -> None:
        if period not in self.order:
            self.order.append(period)
        self.voters.setdefault((period, field), _FieldVoter(self.maxlen)).add(value)

    def _has(self, period: str, field: str) -> bool:
        v = self.voters.get((period, field))
        return bool(v and v.samples)

    def prime(self, periods: list[dict]) -> None:
        """Restore periods from a stored forecast so a restart keeps /forecast.
        The stored consensus seeds each voter as one sample; live airings vote on
        top of it."""
        for p in periods:
            name = p.get("period")
            if not name:
                continue
            for k in self._FIELDS:
                if p.get(k) is not None:
                    self._vote(name, k, p[k])

    def update(self, text: str) -> bool:
        # Climate outlook / almanac recaps are not the daily forecast.
        if _RE_FC_OUTLOOK.search(text):
            return False
        changed = False
        if m := _RE_FC_AREA.search(text):
            self.city = _norm_city(m.group(1))
            # A fresh forecast pass ("...the forecast for the Muncie area...")
            # starts here; drop the carry-over so a stale period from the prior
            # pass can't absorb this segment's lead text (e.g. a leftover "This
            # Afternoon" swallowing "...highs in the mid-90s").
            self._current = None
        # Split the segment at every period header so a multi-period segment
        # attaches each clause's fields to its OWN period (a later period's high
        # must not leak onto an earlier night period). Text before the first
        # header continues the carry-over period from the previous segment.
        matches = list(_RE_PERIOD_HDR.finditer(text))
        spans: list[tuple[str | None, str]] = []
        if not matches:
            spans.append((self._current, text))
        else:
            if matches[0].start() > 0:
                spans.append((self._current, text[: matches[0].start()]))
            for i, m in enumerate(matches):
                name = m.group(1).strip().title()
                end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
                spans.append((name, text[m.start(): end]))
                self._current = name
                changed = True
        for period, chunk in spans:
            if period is None:
                continue
            fields = extract_forecast_fields(chunk)
            # A "near steady temperature in the X" reading has no high/low label;
            # it's the high on a day period, the low on a night period.
            steady = fields.pop("steady_f", None)
            # A zone-forecast day period forecasts only a high, a night period
            # only a low. The opposite slot in a chunk is a leak from an adjacent
            # period (e.g. a grouped "Sunday night through Wednesday ... highs in
            # the 90s"), so drop it and route an unlabeled steady temp to the slot
            # the period actually carries (only as a fallback — never over a
            # period that already has a labeled vote).
            if _is_night_period(period):
                fields.pop("high_f", None)
                if steady is not None and "low_f" not in fields and not self._has(period, "low_f"):
                    fields["low_f"] = steady
            else:
                fields.pop("low_f", None)
                if steady is not None and "high_f" not in fields and not self._has(period, "high_f"):
                    fields["high_f"] = steady
            for k, v in fields.items():
                self._vote(period, k, v)
                changed = True
        return changed

    def snapshot(self) -> list[dict]:
        out: list[dict] = []
        for name in self.order:
            entry: dict = {"period": name}
            conf: dict = {}
            for k in self._FIELDS:
                voter = self.voters.get((name, k))
                if voter and voter.samples:
                    best = voter.best()
                    entry[k] = best.value
                    # vote agreement (0-1): low => the airings disagree, so this
                    # value is at risk of being an STT mishear (off by a lot).
                    conf[k] = round(best.votes / best.total, 2)
            entry["confidence"] = conf
            out.append(entry)
        return out


# --------------------------------------------------------------------------- #
# Generic multi-city extraction (Phase 6+, city-agnostic)
# --------------------------------------------------------------------------- #
_CITY = r"[A-Z][a-z]+(?:\s[A-Z][a-z]+)?"
# "At Muncie, it was clear." / "At Muncie, the temperature was ..." -> primary city.
# STT routinely mis-hears the lead-in "at" as "it"/"in"/"ed" ("...it Muncie, it
# was mostly sunny...", "...Ed Muncie, it was cloudy..."), so accept those; the
# city must still be capitalized and followed by observation framing.
# The evening ob also uses a time-stamped form — "At 7 p.m., [at] Muncie, <sky>
# were reported. The temperature was N" — where STT can slur "At Muncie" into one
# word ("Edmondsee"). Accept the time prefix and the "...were reported" framing so
# that ob's temperature still attaches to the home city (else, when a roundup
# follows in the same segment, the home reading is dropped entirely).
_HOME_LEADIN = (r"(?:(?:at|it|in|ed)\s+"
                r"|at\s+\d{1,2}(?::\d{2})?\s*[ap]\.?\s?m\.?,?\s+(?:(?:at|it|in|ed)\s+)?)")
_RE_CITY_HEADER = re.compile(
    rf"\b{_HOME_LEADIN}({_CITY})\s*,?\s+(?:it (?:was|is)|the (?:temperature|sky|relative|"
    rf"barometric|wind|dew)|[\w\s,]{{0,40}}?were reported)", re.I)
# Regional roundup temperatures, across the phrasings NWR/STT use:
# NB: these are NOT case-insensitive — _CITY is capital-anchored on purpose, so the
# matcher can't swallow a leading lowercase word ("and Shelbyville"). Keyword case
# is handled explicitly instead.
_RE_NEARBY = re.compile(rf"(-?\d{{2,3}})\s+(?:degrees?\s+)?at\s+({_CITY})")      # "63 at Portland"
_RE_REPORTED = re.compile(rf"\b({_CITY})\s+reported\s+(?:at\s+)?(-?\d{{1,3}})\b")  # "Portland reported 75"
# "at/Ed Lima, Ohio ... temperature of 71" / "At Cincinnati ... temperature of 72"
# (number AFTER the city). "temperature OF" marks a roundup city; the home ob uses
# "temperature WAS N degrees", so this never grabs the primary observation.
_RE_AT_TEMP = re.compile(
    rf"\b(?:[Aa]t|[Ee]d|[Ii]t|[Ii]n)\s+({_CITY})\b(?:,\s+[A-Z][a-z]+)?"
    rf"[^.]*?\b[Tt]emperature of\s+(-?\d{{1,3}})\b")


def _nearby_temps(text: str) -> list[tuple[str, int]]:
    """(city, temperature_f) for every roundup phrasing in `text`."""
    out: list[tuple[str, int]] = [(c, int(v)) for v, c in _RE_NEARBY.findall(text)]
    out += [(c, int(v)) for c, v in _RE_REPORTED.findall(text)]
    out += [(c, int(v)) for c, v in _RE_AT_TEMP.findall(text)]
    return out
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
# Lead-in phrases that introduce the regional roundup right after the home-station
# observation ("... and falling. Nearby, at Indianapolis ...").
_RE_ROUNDUP_LEADIN = re.compile(
    r"\b(?:nearby|elsewhere|just outside|other (?:locations|cities)|"
    r"across (?:the|central|northern|southern|indiana))\b", re.I)


def _roundup_start(text: str) -> int:
    """Index where the regional roundup begins, so the home-station observation
    before it can be parsed in isolation. Earliest of a roundup lead-in phrase or
    the first 'NN at <City>' nearby temperature; len(text) if neither is present."""
    idx = len(text)
    for rx in (_RE_ROUNDUP_LEADIN, _RE_NEARBY, _RE_REPORTED, _RE_AT_TEMP):
        if m := rx.search(text):
            idx = min(idx, m.start())
    return idx


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
        # Regional-roundup temps belong to the named cities (all three phrasings:
        # "74 at Marion", "Marion reported 74", "at Marion ... temperature of 74").
        nearby = _nearby_temps(text)
        seen: set[tuple[str, int]] = set()
        for city_raw, val in nearby:
            city = _norm_city(city_raw)
            if -60 <= val <= 130 and (city, val) not in seen:
                seen.add((city, val))
                readings.append(self._reading(city, "temperature_f", val))

        # The home-station observation, when present, leads its segment and can be
        # FOLLOWED by the roundup in the same segment. Parse the primary block in
        # isolation (from its "At Muncie, ..." header up to where the roundup
        # begins) so a shared segment still records the home obs — the bug this
        # fixes: a trailing "74 at Marion" used to drop the whole Muncie reading.
        primary_block: str | None = None
        for m in _RE_CITY_HEADER.finditer(text):
            if _norm_city(m.group(1)) == self.primary_city:
                tail = text[m.start():]
                primary_block = tail[:_roundup_start(tail)]
                break
        if primary_block is None and not nearby:
            # No "At <City>" header and no roundup: a bare standalone observation
            # ("the temperature was N ...") is the station's home city. (When a
            # roundup IS present without a home header, attribute nothing to the
            # primary city, so a roundup lead-in sentence can't poison it.)
            primary_block = text

        # skip climate-summary/almanac recaps — they quote yesterday's or normal
        # highs/lows, not live conditions, and would poison the primary readings.
        if primary_block and not _RE_RECAP.search(primary_block):
            for cond, val in extract_observation(primary_block).items():
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
            if b is not None:  # pragma: no branch - a registered voter always has a sample
                out[k] = {"value": b.value, "votes": b.votes, "total": b.total, "source": "voice"}
        return out


# --------------------------------------------------------------------------- #
# Climate / almanac recap extraction
# --------------------------------------------------------------------------- #
# The loop's climate-summary segment quotes the day's almanac: year-to-date
# precipitation and its departure from normal, sunrise/sunset, and degree days.
# The conditions/forecast aggregators deliberately *skip* this block (it quotes
# normal/past values, not live weather — see _RE_RECAP / _RE_FC_OUTLOOK); this
# extractor is the one place that actually captures it.
#
# A spoken clock time, across the renderings STT produces: "9:15 PM", "9.15pm"
# (colon heard as a period), "6 AM" (no minutes).
_ALM_TIME = r"(\d{1,2}(?:[:.\s]\d{2})?\s*[ap]\.?\s?m\.?)"
_RE_SUNRISE = re.compile(rf"sunrise[^.]*?\bat\s+{_ALM_TIME}", re.I)
_RE_SUNSET = re.compile(rf"sunset[^.]*?\bat\s+{_ALM_TIME}", re.I)
# "total precipitation for/from the year now/still stands at 17.39 inches"
_RE_PRECIP_YEAR = re.compile(
    r"precipitation (?:for|from) the year\s+(?:now |still )?stands? at\s+"
    r"(\d{1,3}\.\d{1,2})", re.I)
# "...which is 2.72 inches below normal" — signed: below -> deficit (negative).
_RE_PRECIP_DEPART = re.compile(
    r"(\d{1,3}\.\d{1,2})\s*inch(?:es)?\s+(below|above)\s+normal", re.I)
# "normal precipitation total for the seven days is around 1.10 inches"
_RE_NORMAL_PRECIP = re.compile(
    r"normal precipitation total for the (?:seven days|7 days|week)\s+is\s+"
    r"(?:around\s+)?(\d{1,3}\.\d{1,2})", re.I)
# Yesterday's heating/cooling degree days ("there were no/5 heating degree days").
# Anchored on were/was so the season-to-date total ("this leaves 288 ...") can't
# be mistaken for the daily value.
_RE_HDD = re.compile(r"(?:were|was)\s+(no|\d{1,3})\s+heating degree days?", re.I)
_RE_CDD = re.compile(r"(?:were|was)\s+(no|\d{1,3})\s+cooling degree days?", re.I)

_ALMANAC_RANGE = {
    "precip_year_in": (0.0, 200.0),
    "precip_departure_in": (-100.0, 100.0),
    "normal_precip_week_in": (0.0, 20.0),
    "heating_degree_days": (0, 100),
    "cooling_degree_days": (0, 100),
}
# fields the store persists as numbers; sunrise/sunset are kept as text.
ALMANAC_NUMERIC = frozenset(_ALMANAC_RANGE)


def _norm_clock(s: str) -> str:
    """Normalise a spoken time ("9.15pm", "6 AM") to "H:MM AM/PM"."""
    s = s.strip().lower()
    mer = "PM" if re.search(r"p\.?\s?m", s) else "AM"
    if m := re.search(r"(\d{1,2})[:.\s]+(\d{2})", s):
        return f"{int(m.group(1))}:{m.group(2)} {mer}"
    m = re.search(r"(\d{1,2})", s)  # the _ALM_TIME pattern guarantees a leading digit
    return f"{int(m.group(1))}:00 {mer}"


def _degree_days(token: str) -> int:
    return 0 if token.lower() == "no" else int(token)


def extract_almanac(text: str) -> dict:
    """Return whatever climate/almanac fields are present in this text.

    Empty when the segment isn't a climate recap, so classify()/callers can use a
    truthy result as the detector.
    """
    out: dict = {}
    if m := _RE_SUNRISE.search(text):
        out["sunrise"] = _norm_clock(m.group(1))
    if m := _RE_SUNSET.search(text):
        out["sunset"] = _norm_clock(m.group(1))
    if m := _RE_PRECIP_YEAR.search(text):
        out["precip_year_in"] = float(m.group(1))
    if m := _RE_PRECIP_DEPART.search(text):
        v = float(m.group(1))
        out["precip_departure_in"] = -v if m.group(2).lower() == "below" else v
    if m := _RE_NORMAL_PRECIP.search(text):
        out["normal_precip_week_in"] = float(m.group(1))
    if m := _RE_HDD.search(text):
        out["heating_degree_days"] = _degree_days(m.group(1))
    if m := _RE_CDD.search(text):
        out["cooling_degree_days"] = _degree_days(m.group(1))
    for k, (lo, hi) in _ALMANAC_RANGE.items():
        if k in out and not (lo <= out[k] <= hi):
            del out[k]
    return out


class AlmanacAggregator:
    """Majority-votes each almanac field across re-hearings of the climate recap.

    Single-site (the home station's climate summary), so unlike the city
    aggregator there's no city key — just field -> voted value.
    """

    def __init__(self, maxlen: int = 15):
        self.maxlen = maxlen
        self.voters: dict[str, _FieldVoter] = {}

    def update(self, text: str) -> list[dict]:
        """Returns every almanac field heard in this text with its voted value."""
        readings: list[dict] = []
        for field, val in extract_almanac(text).items():
            voter = self.voters.setdefault(field, _FieldVoter(self.maxlen))
            voter.add(val)
            best = voter.best()
            readings.append({"field": field, "value": best.value,
                             "votes": best.votes, "total": best.total})
        return readings

    def snapshot(self) -> dict:
        out: dict = {}
        for field, voter in self.voters.items():
            b = voter.best()
            if b is not None:  # pragma: no branch - a registered voter always has a sample
                out[field] = {"value": b.value, "votes": b.votes,
                              "total": b.total, "source": "voice"}
        return out

    def prime(self, readings: list[dict]) -> None:
        """Seed voters from stored latest almanac so a restart keeps state."""
        for r in readings:
            self.voters.setdefault(r["field"], _FieldVoter(self.maxlen)).add(r["value"])


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
# Alert-narrative wind SPEED ("winds to 60 mph", "gusts up to 70 mph"). Distinct
# from the current-conditions wind-DIRECTION regex (_RE_WIND, defined above) —
# they previously shared the name, and this one silently shadowed it, disabling
# wind extraction in current conditions.
_RE_ALERT_WIND = re.compile(
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
    if m := _RE_ALERT_WIND.search(text):
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
