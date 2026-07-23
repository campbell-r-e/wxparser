"""Word-level STT mis-hearing corrections for non-place weather terms.

Applied to transcript text right after transcription (stt.transcribe) — purely
backend post-processing, the STT model/prompt is untouched. The point is that
base.en consistently mis-hears some templated forecast vocabulary the same way,
and a wrong word silently breaks structured extraction: "Highs around 80" heard
as "Pies around 80" drops the forecast high entirely (the high regex looks for
"high"). Place names are handled separately in place_names.py.

Add entries as new consistent mis-hearings surface. Matching is whole-word and
case-insensitive; the replacement keeps the original word's capitalization.
`deploy/propose_corrections.py` mines the stored transcripts for candidates and
labels each SAFE (fold globally) or CONTEXT-SCOPE (real word — scope it to the
slot); its verdicts are advisory, not automatic. Two of its recurring "garbles"
are false positives that must NEVER be folded: "inches"/"trace" before "of
precipitation" are the almanac's real vocabulary ("0.12 inches of precipitation
has fallen", "a trace of precipitation"), not misheard "chance".

Station-specific vocabulary (the callsign) is NOT hard-coded here — it comes from
the active profile's optional `term_corrections`, so porting to another region
stays a drop-in profile.
"""

from __future__ import annotations

import re

from ..profile import get_profile

# canonical term -> consistent STT mis-hearings folded into it
TERM_CORRECTIONS: dict[str, list[str]] = {
    # "Highs"/"Lows" label the forecast temps; a garble either drops the number
    # entirely (the high/low regexes look for the literal word) or, worse, lets a
    # grouped period's daytime high leak onto a night period as an impossible low
    # ("Sunday night through Wednesday ... eyes in the lower 90s" -> Sunday Night
    # low 95F). "Eyes"/"Blows" never appear in NWR's templated vocabulary, so
    # they're safe to fold like "Pies".
    # "Eyes"/"Hives" (highs) and "Blows"/"Flows" (lows) all rhyme with the word
    # they garble and never appear in NWR's templated vocabulary, so fold them.
    # "Hines"/"Knows"/"Ponds"/"Mows" came from a 20,584-transcript mining pass
    # (2026-07-23) — each was flagged SAFE: not an English word in any weather
    # context ("Hines", "Mows") or never used by NWR ("Knows", "Ponds").
    "Highs": ["Pies", "Eyes", "Hives", "Hines"],
    "Lows": ["Blows", "Flows", "Lowes", "Knows", "Ponds", "Mows"],
    # "Chance of Rain" is consistently heard as "Chants of Brain" (also "Cants of
    # rain"); none of these appear in legit NWR vocabulary, so fold each word.
    "Chance": ["Chants", "Cants", "Chans"],
    "Rain": ["Brain"],
}

# Garbles that ARE real words elsewhere, so they can only be folded inside the
# temperature slot: "those"/"goes"/"pines"/"close" all have legit uses ("those
# storms", "goes on", "temperatures close to normal"), but nothing legitimately
# precedes a temperature band, so in that position they're a misheard Highs/Lows.
_SLOT_CORRECTIONS: dict[str, list[str]] = {
    "Lows": ["close", "those", "goes"],
    "Highs": ["pines"],
}
# what marks the temperature slot: a band ("in the lower 60s") or a bare value
# ("around 70", "near 68")
_TEMP_SLOT = r"(?=\s+(?:in the (?:lower|low|mid|middle|upper)\b|around\s+\d|near\s+\d))"

_SLOT_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(rf"\b(?:{'|'.join(re.escape(v) for v in variants)})\b{_TEMP_SLOT}", re.I),
     canon)
    for canon, variants in _SLOT_CORRECTIONS.items()
]

# "Chances of rain 50%" — the plural is a real word, so scope it to the precip
# slot rather than folding globally.
_CHANCES_AS_CHANCE = re.compile(
    r"\bchances\b(?=\s+of\s+(?:rain|showers|thunderstorms))", re.I)

_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(rf"\b(?:{'|'.join(re.escape(v) for v in variants)})\b", re.I), canon)
    for canon, variants in TERM_CORRECTIONS.items()
]

# "east" in the wind slot is mis-heard as "eased" ("The wind was eased at 7 miles
# an hour", "eased winds around 5"), dropping the wind direction. Can't fold
# globally — "eased" is legit ("winds eased", "the threat eased at 7 p.m."). So
# correct it ONLY in wind-direction position: directly before "winds" or before
# an "at N miles" speed (which a non-wind "eased at 7 p.m." never matches).
_EASED_AS_EAST = re.compile(
    r"\beased\b(?=\s+(?:winds?\b|at\s+\d+\s+miles?\b))", re.I)

# "fell" in the almanac precip recap ("no precipitation fell yesterday") is
# consistently mis-heard as "failed" and "trail". Can't fold either globally —
# both are legit words ("the warning failed to...", "the storm trail"). Correct
# them ONLY directly after "precipitation".
_FAILED_AS_FELL = re.compile(r"(?<=precipitation )(?:failed|trail)\b", re.I)

# Station-specific corrections from the active profile (e.g. the callsign: KJY93
# is decoded as "KJ193" — 1,088 of 20,612 stored transcripts, and never once
# correctly). Loaded on FIRST USE, not at import, so importing wxparser does no
# disk I/O and a WX_PROFILE override set before first use is still honored —
# the same contract place_names.py keeps.
_profile_patterns: list[tuple[re.Pattern, str]] | None = None


def _ensure_loaded() -> list[tuple[re.Pattern, str]]:
    """The active profile's term patterns, compiled once. Empty if it defines none."""
    global _profile_patterns
    if _profile_patterns is None:
        _profile_patterns = [
            (re.compile(rf"\b(?:{'|'.join(re.escape(v) for v in variants)})\b", re.I),
             canon)
            for canon, variants in get_profile().get("term_corrections", {}).items()
            if variants
        ]
    return _profile_patterns


def _cased(canon: str):
    def repl(m: re.Match) -> str:
        return canon if m.group(0)[:1].isupper() else canon.lower()
    return repl


def correct_terms(text: str) -> str:
    """Fix known STT word mis-hearings in a transcript, preserving capitalization."""
    for pattern, canon in _PATTERNS:
        text = pattern.sub(_cased(canon), text)
    for pattern, canon in _ensure_loaded():
        text = pattern.sub(_cased(canon), text)
    for pattern, canon in _SLOT_PATTERNS:
        text = pattern.sub(_cased(canon), text)
    text = _CHANCES_AS_CHANCE.sub(
        lambda m: "Chance" if m.group(0)[:1].isupper() else "chance", text)
    text = _EASED_AS_EAST.sub(
        lambda m: "East" if m.group(0)[:1].isupper() else "east", text)
    text = _FAILED_AS_FELL.sub(
        lambda m: "Fell" if m.group(0)[:1].isupper() else "fell", text)
    return text
