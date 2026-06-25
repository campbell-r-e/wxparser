"""Canonical place-name corrections for STT mis-hearings (KJY93 roster).

whisper has no language model for local proper nouns, so it mis-hears the same
cities the same way ("Muncie"->"Monthsy", "Terre Haute"->"Terrell", "Lima"->
"Lyle"). These were confirmed by cross-referencing the reported temperature
against the real NWS observation for each candidate station.

`correct_place()` is applied at extraction time (extract._norm_city) so the
database only ever sees the canonical spelling. The standalone nightly
`deploy/fix_city_spellings.py` keeps its own copy as a backstop for rows that
predate this or slip through.
"""

from __future__ import annotations

# canonical name -> STT variants folded into it (matched case-insensitively)
PLACE_CORRECTIONS: dict[str, list[str]] = {
    "Muncie": [
        "Monthsy", "Munsee", "Muncey", "Muncy", "Munci", "Muncee",
        "Munsie", "Munsy", "Monsey", "Munce", "Mun See", "Muns",
    ],
    "Terre Haute": [
        "Terrehold", "Terrehald", "Terrehalt", "Terrahold", "Terrahaut",
        "Terrell", "Terrelld", "Terrellt", "Terre Hold", "Terre Halt",
        "Terreault",
    ],
    "Champaign": ["Champagne", "Campaign", "Champ Pain", "Sham Pain"],
    "Lima": [
        "Lyle", "Laima", "Laimo", "Loomo", "Lulule", "Lulevel",
        "Lyme", "Lima Ohio", "Leema", "Limo", "Lime", "La Mile", "La", "Lyma",
    ],
    "South Bend": ["South End", "Southend", "South And"],
    "Marion": ["Merriam", "Meridian", "Mary Ann", "Marian", "Merion"],
    "Louisville": ["Blue", "Luhl"],
    "Dayton": ["Deepan", "Deep", "Deepin", "Deepen"],
    "Portland": ["Ridgebrough"],
}

# variant (lowercased) -> canonical
_LOOKUP: dict[str, str] = {
    variant.lower(): canon
    for canon, variants in PLACE_CORRECTIONS.items()
    for variant in variants
}


def correct_place(name: str) -> str:
    """Map a heard place name to its canonical spelling, or return it unchanged."""
    return _LOOKUP.get(name.strip().lower(), name)
