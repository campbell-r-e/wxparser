"""Word-level STT mis-hearing corrections for non-place weather terms.

Applied to transcript text right after transcription (stt.transcribe) — purely
backend post-processing, the STT model/prompt is untouched. The point is that
base.en consistently mis-hears some templated forecast vocabulary the same way,
and a wrong word silently breaks structured extraction: "Highs around 80" heard
as "Pies around 80" drops the forecast high entirely (the high regex looks for
"high"). Place names are handled separately in place_names.py.

Add entries as new consistent mis-hearings surface. Matching is whole-word and
case-insensitive; the replacement keeps the original word's capitalization.
"""

from __future__ import annotations

import re

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
    "Highs": ["Pies", "Eyes", "Hives"],
    "Lows": ["Blows", "Flows"],
    # "Chance of Rain" is consistently heard as "Chants of Brain" (also "Cants of
    # rain"); none of these appear in legit NWR vocabulary, so fold each word.
    "Chance": ["Chants", "Cants"],
    "Rain": ["Brain"],
}

_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(rf"\b(?:{'|'.join(re.escape(v) for v in variants)})\b", re.I), canon)
    for canon, variants in TERM_CORRECTIONS.items()
]

# "close" can't be folded globally — it's legit elsewhere ("temperatures close to
# normal"). But in the temperature slot ("Close in the lower 60s", "Close around
# 70") it's a misheard "lows", so correct it ONLY when a temp band/value follows.
_CLOSE_AS_LOWS = re.compile(
    r"\bclose\b(?=\s+(?:in the (?:lower|low|mid|middle|upper)\b|around\s+\d|near\s+\d))",
    re.I)


def _cased(canon: str):
    def repl(m: re.Match) -> str:
        return canon if m.group(0)[:1].isupper() else canon.lower()
    return repl


def correct_terms(text: str) -> str:
    """Fix known STT word mis-hearings in a transcript, preserving capitalization."""
    for pattern, canon in _PATTERNS:
        text = pattern.sub(_cased(canon), text)
    text = _CLOSE_AS_LOWS.sub(
        lambda m: "Lows" if m.group(0)[:1].isupper() else "lows", text)
    return text
