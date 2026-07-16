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
from statistics import median

from .data.place_names import correct_place, is_known_city, resolve_slot
from .timefmt import parse_iso_utc

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

# how far past the pressure value to look for its rising/falling/steady trend
_TREND_WINDOW = 30

_FIELD_RANGE = {
    "temperature_f": (-60, 130),
    "dewpoint_f": (-60, 100),
    "humidity_pct": (0, 100),
    "pressure_in": (25.0, 35.0),
    "wind_speed_mph": (0, 120),
}


def _num(s: str) -> int | None:
    return words_to_int(s.strip())


def _drop_out_of_range(out: dict, ranges: dict) -> None:
    """Drop extracted fields whose value falls outside its plausible range
    (an STT mishear like "999 degrees"), leaving the rest of `out` intact.
    """
    for k, (lo, hi) in ranges.items():
        if k in out and not (lo <= out[k] <= hi):
            del out[k]


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
        if t := _RE_PRESS_TREND.search(text[m.end():m.end() + _TREND_WINDOW]):
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
    _drop_out_of_range(out, _FIELD_RANGE)
    return out


# --------------------------------------------------------------------------- #
# Zone-forecast extraction (Phase 6)
# --------------------------------------------------------------------------- #
_DAYS = "monday|tuesday|wednesday|thursday|friday|saturday|sunday"
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
# The NWS paragraph break before a period (".Thursday Night") is spoken as a
# lead-in the STT renders as "for"/"fore" and, routinely, the garble "four"
# ("...mid 80s. Four Thursday night..."). Accepting "four/fore" here lets the
# real header open the period so its trailing "chance of rain N%" attaches to
# that day instead of leaking onto the carry-over period.
RE_PERIOD_HDR = re.compile(
    rf"(?:^|[.,]\s+|\b(?:for|fore|four)\s+)({_PERIOD_NAME})\b(?=[\s,.]|$)", re.I)
# Climate outlook / almanac — NOT a daily forecast ("8 to 14 day outlook ...
# temperatures above normal", "normal high is 85"). Parsing it as periods invents
# bogus far-future days and phantom highs, so skip the whole segment. NB: match
# on "outlook"/"normal", NOT on a "N-day" range — the legit *3-7 day forecast*
# (real Saturday/Sunday highs/lows) also says "3 to 7 day".
# vocabulary shared with _RE_RECAP: phrases that mark climate/almanac talk
# rather than live conditions or the daily forecast.
_ALMANACY = r"normal\s+(?:high|low)|degree days?"
_RE_FC_OUTLOOK = re.compile(
    rf"\b(?:outlook|(?:above|below|near)\s+normal|{_ALMANACY}|climate)\b", re.I)
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
    rf"chance of (?:rain|precipitation|showers|snow)[\s,]+"
    rf"{_NUM}\s*(?:%|percent)", re.I)
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


# a temp phrase ("in the mid 80s") ends quickly; the cap keeps a run-on
# sentence captured by the lazy [^.]*? from feeding garbage to the parsers
_TEMP_PHRASE_MAX = 30


def parse_temp_value(phrase: str) -> int | None:
    phrase = phrase.lower()[:_TEMP_PHRASE_MAX]
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


# plausible-value bounds per forecast field (highs/steady share the observation
# temperature range). A forecast LOW of 85+ never verifies in this feed's
# climate — it's the "lows in the mid-EIGHTIES" mishear of "mid-sixties"
# (aired twice 2026-07-07; audit_data.py flags any stored night low >= 85),
# so the cap sits just under it.
_FC_RANGE = {"high_f": _FIELD_RANGE["temperature_f"], "low_f": (-60, 84),
             "steady_f": _FIELD_RANGE["temperature_f"], "precip_pct": (0, 100)}


def _fc_ok(field: str, v) -> bool:
    lo, hi = _FC_RANGE[field]
    return lo <= v <= hi


def extract_forecast_fields(text: str) -> dict:
    """Highs/lows/precip/sky found in a single forecast sentence/segment."""
    out: dict = {}
    if m := _RE_HIGH.search(text):
        if (v := parse_temp_value(m.group(1))) is not None and _fc_ok("high_f", v):
            out["high_f"] = v
    if m := _RE_LOW.search(text):
        if (v := parse_temp_value(m.group(1))) is not None and _fc_ok("low_f", v):
            out["low_f"] = v
    # representative temp stated without "high"/"low" — routed by the aggregator.
    if "high_f" not in out and "low_f" not in out and (m := _RE_FC_TEMP.search(text)):
        if (v := parse_temp_value(m.group(1))) is not None and _fc_ok("steady_f", v):
            out["steady_f"] = v
    if m := _RE_PRECIP.search(text):
        p = words_to_int(m.group(1))  # handles "80" and "eighty"
        if p is not None and _fc_ok("precip_pct", p):
            out["precip_pct"] = p
    if m := _RE_SKY.search(text):
        out["sky"] = m.group(1).lower()
    return out


def _is_night_period(name: str) -> bool:
    n = name.lower()
    return n in ("tonight", "overnight", "rest of tonight") or n.endswith(" night")


# Recap / re-open lead-ins that END the period in progress ("...chance of rain
# 20%. Once again, the forecast for today...", "Repeating the forecast..."). The
# recapped period re-opens via its own header right after, so the carry-over must
# be dropped here — otherwise the *previous* pass's trailing "chance of rain N%"
# rides onto the recapped near-term period (the "Today precip 70%" leak).
_RE_FC_RECAP = re.compile(
    r"\b(?:once again|repeating|to repeat|here (?:is|again))\b", re.I)

# Recurring STT garbles of the near-term period headers, which otherwise go
# unparsed so Today/Tonight stop getting fresh votes and freeze on a stale value.
# "Rest of" -> "West of" (rest->west) and the ". Tonight"/"For tonight" break
# heard as "Port tonight". Normalized to the canonical wording before header
# matching so the period opens and votes normally.
_FC_GARBLE_SUBS = (
    (re.compile(r"\bwest of (today|tonight)\b", re.I), r"rest of \1"),
    (re.compile(r"\bport (today|tonight)\b", re.I), r"\1"),
)

# Periods air in sequence and a period's "chance of rain N%" is the LAST clause of
# its paragraph, so a segment's pre-header tail (the text before its first header)
# closes the header's *immediate predecessor* — never an arbitrary carry-over. A
# stale carry-over ("Wednesday", left over from a pass hours ago) that is NOT that
# predecessor must not absorb the tail's PoP/temp (the "Wednesday precip 70%" that
# was really Friday's, and the "Today precip 70%" that was really Thursday's).
_WEEKDAY_ORDER = _DAYS.split("|")
_WEEKDAYS = frozenset(_WEEKDAY_ORDER)
# Near-term chain predecessors (day/evening/night of the current day).
_NEAR_TERM_PRED = {
    "tonight": "Today", "this evening": "Today", "this afternoon": "Today",
    "this morning": None, "today": None, "rest of today": "Today",
    "overnight": "Tonight", "rest of tonight": "Tonight"}


def _period_predecessor(name: str) -> str | None:
    """The period that immediately precedes `name` in a zone forecast, or None if
    `name` opens the forecast. Used to tell a genuine carry-over continuation from
    a stale one when a pre-header tail is followed by a header.
    """
    n = name.lower()
    parts = n.split(" ")
    if len(parts) == 2 and parts[0] in _WEEKDAYS and parts[1] == "night":
        return parts[0].title()                       # "Friday Night" -> "Friday"
    if n in _WEEKDAYS:                                 # "Thursday" -> "Wednesday Night"
        return f"{_WEEKDAY_ORDER[_WEEKDAY_ORDER.index(n) - 1].title()} Night"
    return _NEAR_TERM_PRED.get(n)


# City-name plumbing shared by the forecast and multi-city aggregators (the
# rest of the city patterns live with CityConditionsAggregator below).
_CITY = r"[A-Z][a-z]+(?:\s[A-Z][a-z]+)?"
# "... forecast for the Muncie area ..." -> forecast area name (case-sensitive city,
# literal " area" suffix so the city group can't swallow the word "area")
_RE_FC_AREA = re.compile(rf"[Ff]orecast for (?:the )?({_CITY})\s+area")


def _norm_city(name: str) -> str:
    # title-case, then fold known STT mis-hearings to the canonical spelling so
    # the store only ever sees correct city names (no nightly cleanup needed).
    return correct_place(name.strip().title())


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

    # A (period, field) aired every readout stays within a couple of passes; one
    # that stops airing must age out or it serves a frozen value forever. This
    # covers both a superseded period (e.g. "Today" -> "Rest Of Today" -> "Tonight",
    # or a dropped extended day) AND a cleared field: when a period's chance of rain
    # falls to nil the broadcast simply stops saying "chance of rain", casting no
    # vote, so the precip would otherwise freeze on its last stated value while the
    # period keeps airing. Evict anything not seen in this many forecast passes
    # ("...forecast for the X area..." starts a pass).
    def __init__(self, area: str = "Muncie", maxlen: int = 15, stale_passes: int = 15):
        self.maxlen = maxlen
        self.stale_passes = stale_passes
        self.order: list[str] = []   # period names in first-seen order
        self.voters: dict[tuple[str, str], _FieldVoter] = {}
        self._current: str | None = None
        self._pass = 0               # forecast-readout counter, for staleness
        self._last_pass: dict[tuple[str, str], int] = {}  # (period,field) -> pass aired
        self.city: str = area  # area the current forecast covers ("...for the X area")

    def _vote(self, period: str, field: str, value) -> None:
        if period not in self.order:
            self.order.append(period)
        self._last_pass[(period, field)] = self._pass
        self.voters.setdefault((period, field), _FieldVoter(self.maxlen)).add(value)

    def _fresh(self, period: str, field: str) -> bool:
        """This (period, field) was aired within the last `stale_passes` readouts —
        else it's a superseded period or a field the broadcast has stopped stating
        (e.g. a chance of rain that dropped to nil), and must not be served.
        """
        return self._pass - self._last_pass.get((period, field), self._pass) <= self.stale_passes

    def _has(self, period: str, field: str) -> bool:
        v = self.voters.get((period, field))
        return bool(v and v.samples)

    def prime(self, periods: list[dict]) -> None:
        """Restore periods from a stored forecast so a restart keeps /forecast.
        The stored consensus seeds each voter as one sample; live airings vote on
        top of it.
        """
        for p in periods:
            name = p.get("period")
            if not name:
                continue
            for k in self._FIELDS:
                if p.get(k) is not None:
                    self._vote(name, k, p[k])

    def update(self, text: str) -> bool:
        for pat, rep in _FC_GARBLE_SUBS:   # canonicalize near-term header garbles
            text = pat.sub(rep, text)
        # Climate outlook / almanac recaps are not the daily forecast.
        if _RE_FC_OUTLOOK.search(text):
            return False
        before = self._values()
        if m := _RE_FC_AREA.search(text):
            self.city = _norm_city(m.group(1))
            self._pass += 1   # a fresh readout begins — advances the staleness clock
            # A fresh forecast pass ("...the forecast for the Muncie area...")
            # starts here; drop the carry-over so a stale period from the prior
            # pass can't absorb this segment's lead text (e.g. a leftover "This
            # Afternoon" swallowing "...highs in the mid-90s").
            self._current = None
        # A recap ("Once again, the forecast for today...") likewise closes the
        # period in progress; without this its lead-in "chance of rain N%" (the
        # tail of the previous pass) rides onto the carry-over near-term period.
        if _RE_FC_RECAP.search(text):
            self._current = None
        spans, first_hdr, has_tail = self._split_spans(text)
        for idx, (period, chunk) in enumerate(spans):
            if period is None:
                continue
            # Drop a stale carry-over tail: the pre-header tail closes the first
            # header's predecessor. If the carry-over period isn't that predecessor
            # it's left over from an earlier pass — attributing this segment's
            # trailing "chance of rain N%" to it is the cross-period precip leak.
            if (idx == 0 and has_tail
                    and period != _period_predecessor(first_hdr)):
                continue
            self._route_fields(period, chunk)
        # True only when a voted VALUE actually changed — so the store isn't
        # rewritten a full issuance per airing just because vote tallies ticked.
        return self._values() != before

    def _split_spans(self, text: str) -> tuple[list[tuple[str | None, str]], str | None, bool]:
        """Split the segment at every period header so a multi-period segment
        attaches each clause's fields to its OWN period (a later period's high
        must not leak onto an earlier night period). Text before the first
        header continues the carry-over period from the previous segment.

        Returns (spans, first_header, has_tail): has_tail is True when carry-over
        text precedes the first header (it exists only when that header isn't at
        the segment start). Advances self._current to the last header seen.
        """
        matches = list(RE_PERIOD_HDR.finditer(text))
        if not matches:
            return [(self._current, text)], None, False
        spans: list[tuple[str | None, str]] = []
        if matches[0].start() > 0:
            spans.append((self._current, text[: matches[0].start()]))
        for i, m in enumerate(matches):
            name = m.group(1).strip().title()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            spans.append((name, text[m.start(): end]))
            self._current = name
        return spans, matches[0].group(1).strip().title(), matches[0].start() > 0

    def _route_fields(self, period: str, chunk: str) -> None:
        """Extract one span's fields and vote them onto its period."""
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
            # the steady temp passed the wide steady_f range, but as a LOW it
            # must also clear the tighter low_f cap (a steady "upper 80s" on a
            # night period is the same mid-eighties mishear)
            if (steady is not None and _fc_ok("low_f", steady)
                    and "low_f" not in fields and not self._has(period, "low_f")):
                fields["low_f"] = steady
        else:
            fields.pop("low_f", None)
            if steady is not None and "high_f" not in fields and not self._has(period, "high_f"):
                fields["high_f"] = steady
        for k, v in fields.items():
            self._vote(period, k, v)

    def _values(self) -> dict:
        """The voted value of each (period, field) — no confidence/tallies, for
        change detection.
        """
        return {(name, k): self.voters[(name, k)].best().value
                for name in self.order for k in self._FIELDS
                if self._has(name, k) and self._fresh(name, k)}

    def snapshot(self) -> list[dict]:
        out: list[dict] = []
        for name in self.order:
            entry: dict = {"period": name}
            conf: dict = {}
            for k in self._FIELDS:
                voter = self.voters.get((name, k))
                if voter and voter.samples and self._fresh(name, k):
                    best = voter.best()
                    entry[k] = best.value
                    # vote agreement (0-1): low => the airings disagree, so this
                    # value is at risk of being an STT mishear (off by a lot).
                    conf[k] = round(best.votes / best.total, 2)
            if not conf:   # every field superseded/cleared — drop the whole period
                continue
            entry["confidence"] = conf
            out.append(entry)
        return out


# --------------------------------------------------------------------------- #
# Generic multi-city extraction (Phase 6+, city-agnostic)
# --------------------------------------------------------------------------- #
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
    rf"barometric|wind|dew)|[\w\s,]{{0,40}}?(?:was|were) reported)", re.I)
# Regional roundup temperatures, across the phrasings NWR/STT use:
# NB: these are NOT case-insensitive — _CITY is capital-anchored on purpose, so the
# matcher can't swallow a leading lowercase word ("and Shelbyville"). Keyword case
# is handled explicitly instead.
_RE_NEARBY = re.compile(rf"(-?\d{{2,3}})\s+(?:degrees?\s+)?at\s+({_CITY})")      # "63 at Portland"
# "Portland reported 75"
_RE_REPORTED = re.compile(rf"\b({_CITY})\s+reported\s+(?:at\s+)?(-?\d{{1,3}})\b")
# "at/Ed Lima, Ohio ... temperature of 71" / "At Cincinnati ... temperature of 72"
# (number AFTER the city). "temperature OF" marks a roundup city; the home ob uses
# "temperature WAS N degrees", so this never grabs the primary observation.
_RE_AT_TEMP = re.compile(
    rf"\b(?:[Aa]t|[Ee]d|[Ii]t|[Ii]n)\s+({_CITY})\b(?:,\s+[A-Z][a-z]+)?"
    rf"[^.]*?\b[Tt]emperature of\s+(-?\d{{1,3}})\b")


# One roundup temp readily loses a leading digit to STT ("22 at Cincinnati"
# for 92, 2026-06-28) yet sails through the absolute range check. With enough
# peer cities in the same readout the impostor is obvious: drop any reading
# more than _PEER_MAX_DEV from the median of the list. A real front skews the
# whole feed together (readings cluster on both sides of the median), so
# genuine spreads survive; below _PEER_MIN readings there is no quorum to say
# which value is the wrong one, so nothing is dropped.
# (deployment-tunable through Config: WX_PEER_MIN_CITIES / WX_PEER_MAX_DEV_F,
# passed into CityConditionsAggregator by main/reprocess)
_PEER_MIN = 3
_PEER_MAX_DEV = 30

# Vote window for the current-conditions fields, in seconds of broadcast time.
# NWR re-reads the same hourly ob several times before the ob updates, so ~45min
# holds every repeat of the ob on air now while dropping the one before it.
_VOTE_STALE_SEC = 45 * 60


def _clock_sec(captured_at: str | None) -> int:
    """The vote clock: a transcript's ISO stamp as epoch seconds. Unstamped
    callers get 0, which reads as "one instant" and leaves the vote depth-only.
    """
    if not captured_at:
        return 0
    return int(parse_iso_utc(captured_at).timestamp())


def _drop_peer_outliers(pairs: list[tuple[str, int]],
                        peer_min: int = _PEER_MIN,
                        max_dev: int = _PEER_MAX_DEV) -> list[tuple[str, int]]:
    if len(pairs) < peer_min:
        return pairs
    med = median(v for _, v in pairs)
    return [(c, v) for c, v in pairs if abs(v - med) <= max_dev]


def _nearby_temps(text: str, peer_min: int = _PEER_MIN,
                  peer_max_dev: int = _PEER_MAX_DEV) -> list[tuple[str, int]]:
    """(city, temperature_f) for every roundup phrasing in `text`, in textual
    order. Each city is first folded through the alias map (_norm_city); any that
    is STILL unknown is then recovered from its slot when possible — e.g. the
    entry right after "Champaign, Illinois" is Lima — so novel mis-hearings land
    under the right city without needing a catalogued spelling.
    """
    # (pos, city_raw, temp) for every phrasing, merged into one ordered stream.
    raw: list[tuple[int, str, int]] = []
    for m in _RE_NEARBY.finditer(text):
        raw.append((m.start(), m.group(2), int(m.group(1))))
    for m in _RE_REPORTED.finditer(text):
        raw.append((m.start(), m.group(1), int(m.group(2))))
    for m in _RE_AT_TEMP.finditer(text):
        raw.append((m.start(), m.group(1), int(m.group(2))))
    raw.sort(key=lambda r: r[0])

    out: list[tuple[str, int]] = []
    prev_city: str | None = None
    for pos, city_raw, temp in raw:
        city = _norm_city(city_raw)
        if not is_known_city(city):
            city = resolve_slot(prev_city, text, pos) or city
        out.append((city, temp))
        prev_city = city
    return _drop_peer_outliers(out, peer_min, peer_max_dev)
# Climate-summary / almanac recaps quote PAST or normal values, not live
# conditions: "Yesterday's low temperature was 55 degrees", "normal high is 85",
# "record low ...". Their "(high|low) temperature was N degrees" is a substring
# match for _RE_TEMP, so without this guard they get ingested as the primary
# city's *current* temperature (e.g. Muncie current temp wrongly set to 55).
_RE_RECAP = re.compile(
    rf"\b(climate summary|yesterday|{_ALMANACY}|record (?:high|low)"
    rf"|(?:high|low) temperature was)\b", re.I)
# Lead-in phrases that introduce the regional roundup right after the home-station
# observation ("... and falling. Nearby, at Indianapolis ...").
_RE_ROUNDUP_LEADIN = re.compile(
    r"\b(?:nearby|elsewhere|just outside|other (?:locations|cities)|"
    r"across (?:the|central|northern|southern|indiana))\b", re.I)


def _roundup_start(text: str) -> int:
    """Index where the regional roundup begins, so the home-station observation
    before it can be parsed in isolation. Earliest of a roundup lead-in phrase or
    the first 'NN at <City>' nearby temperature; len(text) if neither is present.
    """
    idx = len(text)
    for rx in (_RE_ROUNDUP_LEADIN, _RE_NEARBY, _RE_REPORTED, _RE_AT_TEMP):
        if m := rx.search(text):
            idx = min(idx, m.start())
    return idx


def _wind_speed_from_phrase(phrase: str) -> int:
    """Speed implied by a voted wind phrase: 'west at 8' -> 8, 'calm' (or any
    phrase without a trailing '... at N') -> 0. Keeps wind_speed_mph locked to
    the winning wind direction instead of voting it as an independent field.
    """
    m = re.search(r" at (\d{1,3})$", phrase)
    return int(m.group(1)) if m else 0


class CityConditionsAggregator:
    """City-agnostic current-conditions extraction with per-(city,condition) voting.

    A "At <City>, it was ..." header sets the active city; the condition sentences
    that follow (temperature/humidity/pressure/wind/sky) attach to it. A "Nearby
    ... <temp> at <City>" list yields a temperature per named city. Each
    (city, condition) is majority-voted independently.
    """

    # A field aired every cycle (temperature) stays current in a 15-deep window,
    # but humidity/wind/pressure air only in the periodic full-ob readout, so their
    # window spans hours and a morning value out-votes the current one. Bound each
    # field's vote to the airings heard in the last `stale_sec` seconds of
    # broadcast time (from the transcript's captured_at, so a reprocess replay
    # votes identically to the live run).
    # Temperature is a live reading that legitimately changes hour to hour, and it
    # airs ~2x per cycle (the full ob plus the "once again ... it was N degrees"
    # recap), so the default 15-deep window spans ~7h and the voted value trails the
    # real temperature by hours (67 at noon while it read 69 and was climbing).
    # Vote temperature over a short window instead: recent enough to track the
    # diurnal curve, still deep enough that a lone STT mishear ("97" for "67") is
    # outvoted by its neighbours. Slow fields (sky, humidity, pressure trend) keep
    # the long window, where re-hearing the same value is what buys robustness.
    # The depth bound alone is not enough: the novelty gate drops repeated audio,
    # so five temperature sightings can still span a whole night (seen 2026-07-16:
    # a pool of [88,84,80,75,75] reaching back 9h voted 88 while the station was
    # reading 80). Only a wall-clock bound keeps the vote on the current ob.
    _FIELD_MAXLEN = {"temperature_f": 5}

    def __init__(self, maxlen: int = 15, primary_city: str = "Muncie",
                 stale_sec: int = _VOTE_STALE_SEC, peer_min: int = _PEER_MIN,
                 peer_max_dev: int = _PEER_MAX_DEV):
        self.maxlen = maxlen
        self.primary_city = primary_city
        self.stale_sec = stale_sec
        self.peer_min = peer_min
        self.peer_max_dev = peer_max_dev
        self._clock = 0
        self.voters: dict[tuple[str, str], _FieldVoter] = {}

    def update(self, text: str, captured_at: str | None = None) -> list[dict]:
        """Returns every (city, condition) reading heard in this text, with the
        current voted value/votes/total. Each is a 'sighting' the store counts.

        `captured_at` (the transcript's ISO stamp) is the vote clock, bounding
        each field's pool to the airings within `stale_sec` of it.
        """
        self._clock = _clock_sec(captured_at)
        readings: list[dict] = []
        # Regional-roundup temps belong to the named cities (all three phrasings:
        # "74 at Marion", "Marion reported 74", "at Marion ... temperature of 74").
        nearby = _nearby_temps(text, self.peer_min, self.peer_max_dev)
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
            obs = extract_observation(primary_block)
            # wind_speed_mph is NOT voted on its own — it's derived from the voted
            # wind phrase below. Voted independently, the two windows can disagree
            # (a tie breaking the speed to 0 while the phrase wins "west at 8"),
            # yielding wind="west at 8" with wind_speed_mph=0.
            obs.pop("wind_speed_mph", None)
            for cond, val in obs.items():
                readings.append(self._reading(self.primary_city, cond, val))
            wind = next((r for r in readings if r["condition"] == "wind"), None)
            if wind is not None:
                readings.append({
                    "city": self.primary_city, "condition": "wind_speed_mph",
                    "value": _wind_speed_from_phrase(str(wind["value"])),
                    "votes": wind["votes"], "total": wind["total"],
                })
        return readings

    def _voter(self, city: str, condition: str) -> _FieldVoter:
        key = (city, condition)
        if key not in self.voters:
            maxlen = self._FIELD_MAXLEN.get(condition, self.maxlen)
            self.voters[key] = _FieldVoter(maxlen, stale=self.stale_sec)
        return self.voters[key]

    def _reading(self, city: str, condition: str, value) -> dict:
        voter = self._voter(city, condition)
        voter.add(value, self._clock)
        best = voter.best()
        return {"city": city, "condition": condition,
                "value": best.value, "votes": best.votes, "total": best.total}

    def prime(self, readings: list[dict]) -> None:
        """Seed voters from stored latest readings so a restart keeps state. Seeded
        at the pre-broadcast clock (0), so a primed value serves alone until that
        field airs again and then drops straight out of the vote as stale.
        """
        for r in readings:
            self._voter(r["city"], r["condition"]).add(r["value"], self._clock)


@dataclass
class Voted:
    value: object
    votes: int
    total: int


class _FieldVoter:
    """Majority vote over a rolling window of recent sightings.

    Optionally recency-aware: with `stale` set, `best()` counts only samples whose
    clock (epoch seconds of broadcast time) is within `stale` seconds of the
    newest sample. A field aired infrequently (humidity, wind) otherwise keeps
    hours-old sightings in its fixed-length window and lets a morning value
    out-vote the current one; the stale window bounds the vote to recent airings
    instead. Callers that pass no clock (default 0) and no `stale` get the plain
    mode — unchanged behavior.
    """

    def __init__(self, maxlen: int, stale: int | None = None):
        self.samples: deque = deque(maxlen=maxlen)  # (value, clock)
        self.stale = stale

    def add(self, value, clock: int = 0) -> None:
        self.samples.append((value, clock))

    def best(self) -> Voted | None:
        if not self.samples:
            return None
        newest = self.samples[-1][1]   # clock is monotonic — last added is newest
        if self.stale is not None:
            pool = [(v, c) for v, c in self.samples if newest - c <= self.stale]
        else:
            pool = list(self.samples)
        counts = Counter(v for v, _ in pool)
        top = max(counts.values())
        tied = {v for v, c in counts.items() if c == top}
        # break ties toward the most recent reading (conditions change over time)
        value = next(v for v, _ in reversed(pool) if v in tied)
        return Voted(value=value, votes=top, total=len(pool))


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
# The count is "no", a digit ("12"), or spelled out ("nine", "twenty one") — STT
# renders small counts as words about as often as digits, so a word-only day
# silently kept yesterday's digit value in the vote. words_to_int() handles all
# three; the capture is anchored between were/was and "... degree days" so it
# can't swallow the season-to-date total ("this leaves 288 ...").
_RE_HDD = re.compile(r"(?:were|was)\s+([\w\- ]+?)\s+heating degree days?", re.I)
_RE_CDD = re.compile(r"(?:were|was)\s+([\w\- ]+?)\s+cooling degree days?", re.I)

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


def _degree_days(token: str) -> int | None:
    return 0 if token.strip().lower() == "no" else words_to_int(token)


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
    if (m := _RE_HDD.search(text)) and (v := _degree_days(m.group(1))) is not None:
        out["heating_degree_days"] = v
    if (m := _RE_CDD.search(text)) and (v := _degree_days(m.group(1))) is not None:
        out["cooling_degree_days"] = v
    _drop_out_of_range(out, _ALMANAC_RANGE)
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
# Alert-narrative wind SPEED ("winds to 60 mph", "gusts up to 70 mph") —
# distinct from the current-conditions wind-DIRECTION regex (_RE_WIND).
_RE_ALERT_WIND = re.compile(
    r"(?:winds?|gusts?)\s*(?:up to|of|to|near|around)?\s*(\d{2,3})\s*(?:mph|miles per hour)",
    re.I)
# "near Yorktown", "over the Muncie area", "approaching Albany" — place after a
# locator preposition. STT may garble the name, but capturing it is still useful.
_RE_NEAR = re.compile(
    rf"\b(?:near|over|approaching|just (?:north|south|east|west) of)\s+(?:the\s+)?({_CITY})")
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
        # numeric sizes carry the unit ("hail 1.5in"); named sizes stand alone
        threats.append(f"hail {size}in" if size[:1].isdigit() else f"hail {size}".strip())
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
