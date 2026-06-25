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
    "Highs": ["Pies"],
    # "Chance of Rain" is consistently heard as "Chants of Brain"; neither
    # "chants" nor "brain" appears in legit NWR vocabulary, so fold each word.
    "Chance": ["Chants"],
    "Rain": ["Brain"],
}

_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(rf"\b(?:{'|'.join(re.escape(v) for v in variants)})\b", re.I), canon)
    for canon, variants in TERM_CORRECTIONS.items()
]


def _cased(canon: str):
    def repl(m: re.Match) -> str:
        return canon if m.group(0)[:1].isupper() else canon.lower()
    return repl


def correct_terms(text: str) -> str:
    """Fix known STT word mis-hearings in a transcript, preserving capitalization."""
    for pattern, canon in _PATTERNS:
        text = pattern.sub(_cased(canon), text)
    return text
