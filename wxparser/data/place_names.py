"""Canonical place-name corrections for STT mis-hearings.

whisper has no language model for local proper nouns, so it mis-hears the same
cities the same way ("Muncie"->"Monthsy", "Terre Haute"->"Terrell", "Lima"->
"Lyle"). The roster is region-specific, so it lives in the active station profile
(profile.py / WX_PROFILE) — a new deployment supplies its own `place_corrections`
without touching code. These were confirmed by cross-referencing the reported
temperature against the real NWS observation for each candidate station.

`correct_place()` is applied at extraction time (extract._norm_city) so the
database only ever sees the canonical spelling. The standalone nightly
`deploy/fix_city_spellings.py` keeps its own copy as a backstop for rows that
predate this or slip through.
"""

from __future__ import annotations

from ..profile import PROFILE

# canonical name -> STT variants folded into it (matched case-insensitively)
PLACE_CORRECTIONS: dict[str, list[str]] = PROFILE["place_corrections"]

# variant (lowercased) -> canonical
_LOOKUP: dict[str, str] = {
    variant.lower(): canon
    for canon, variants in PLACE_CORRECTIONS.items()
    for variant in variants
}

# --- positional ("slot") resolution -------------------------------------- #
# The alias map (above) only fixes spellings we've already catalogued, so a
# novel mis-hearing of a name still slips through. The regional roundup recites
# cities in a fixed order, though, so an UNKNOWN city sitting in a known slot can
# be recovered by position: the entry right after "Champaign, Illinois" is Lima,
# and the "...just outside Indiana, Ohio..." slot is Lima. These anchors key off
# names whisper decodes reliably (Champaign carries an "Illinois" tag), so they
# pin the scattering ones without an ever-growing spelling list. Region-specific,
# so they live in the profile (all optional — absent keys just disable the pass).
ROUNDUP_CITIES: frozenset[str] = frozenset(PROFILE.get("roundup_cities", []))
# canonical anchor city -> the canonical city that follows it in the roundup
SLOT_ANCHORS: dict[str, str] = PROFILE.get("slot_anchors", {})
# lead-in phrase (lowercased) -> the canonical city it introduces
ROUNDUP_LEADINS: dict[str, str] = {
    phrase.lower(): city for phrase, city in PROFILE.get("roundup_leadins", {}).items()
}
# how far back to scan for a lead-in phrase preceding an unknown roundup entry
_LEADIN_WINDOW = 60


def correct_place(name: str) -> str:
    """Map a heard place name to its canonical spelling, or return it unchanged."""
    return _LOOKUP.get(name.strip().lower(), name)


def is_known_city(name: str) -> bool:
    """True if `name` is a recognized canonical city in the roundup roster.

    Used to decide whether a roundup entry needs positional recovery. With no
    roster configured, treat every name as known (disables the slot pass)."""
    return not ROUNDUP_CITIES or name in ROUNDUP_CITIES


def resolve_slot(prev_city: str | None, text: str, pos: int) -> str | None:
    """Recover an unknown roundup city from its slot, or None if it can't.

    `prev_city` is the canonical city of the immediately preceding roundup entry;
    `text`/`pos` locate this entry so a lead-in phrase just before it can be seen.
    """
    if prev_city is not None and prev_city in SLOT_ANCHORS:
        return SLOT_ANCHORS[prev_city]
    if ROUNDUP_LEADINS:
        window = text[max(0, pos - _LEADIN_WINDOW):pos].lower()
        for phrase, city in ROUNDUP_LEADINS.items():
            if phrase in window:
                return city
    return None
