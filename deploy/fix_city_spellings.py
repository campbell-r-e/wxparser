#!/usr/bin/env python3
"""Nightly city-name autocorrect for the wxparser database.

Whisper-tiny mis-hears the station's home city ("Muncie") as things like
"Munsee", "Muncy", etc. This script rewrites those mis-spellings to the
canonical name across every table that stores a city, MERGING rather than
blindly renaming so it never trips a primary-key collision when a correctly
spelled row already exists.

Standalone on purpose: it only needs python3 + pg8000 (already installed on the
deploy host) and the same WX_PG_* env vars the service uses. It does not import
wxparser, so it keeps working even if moved out of the repo tree.

Run it by hand to see what it would do:
    python3 fix_city_spellings.py
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pg8000.native

# --- canonical name + its mis-spellings ----------------------------------- #
# Key = correct spelling, value = STT variants to fold into it (matched
# case-insensitively). Add new variants here as you spot them in the data;
# anything not listed is left untouched.
CORRECTIONS: dict[str, list[str]] = {
    "Muncie": [
        "Monthsy",  # the variant whisper-tiny actually produces most often
        "Munsee", "Muncey", "Muncy", "Munci", "Muncee",
        "Munsie", "Munsy", "Monsey", "Munce", "Mun See", "Muns",
    ],
    # "... at Terre Haute ..." — whisper-tiny mangles the silent-final-syllable.
    "Terre Haute": [
        "Terrehold", "Terrehald", "Terrehalt", "Terrahold", "Terrahaut",
        "Terrell", "Terrelld", "Terrellt", "Terre Hold", "Terre Halt",
        "Terreault",  # "...79 Edterreault and 80 Ed Evansville..."
    ],
    # "... at Champaign, Illinois ..." — homophones of the French word.
    "Champaign": [
        "Champagne", "Campaign", "Champ Pain", "Sham Pain",
    ],
    # "... at Lima, Ohio ..." — pronounced "LYE-muh", so it scatters badly.
    "Lima": [
        "Lyle", "Laima", "Laimo", "Loomo", "Lulule", "Lulevel",
        "Lyme", "Lima Ohio", "Leema",
        "Limo", "Lime",  # "...Lima, Ohio, just outside Indiana, it was sunny..."
        "La Mile", "La",  # post-Champaign slot (parallels "...69 at Lyle...")
    ],
    # "... at South Bend ..." — the 'b' drops out.
    "South Bend": [
        "South End", "Southend", "South And",
    ],
    # "... at Marion ..." (MAIR-ee-un) — heard as a three-syllable name.
    "Marion": [
        "Merriam", "Meridian", "Mary Ann", "Marian", "Merion",
    ],
    # "... at Louisville ..." — the out-of-state tail slot after Cincinnati;
    # the decoder collapses the name to a fragment ("...at Blue or more?", or
    # "...73 at Luhl" parallel to the clean "...73 at Louisville").
    "Louisville": [
        "Blue", "Luhl",
    ],
    # "... at Portland ..." (Portland, IN) — slot after Marion ("63 at Portland"
    # heard as "63 at Ridgebrough").
    "Portland": [
        "Ridgebrough",
    ],
    # "... at Dayton ..." — confirmed by temperature cross-reference: the
    # "Deepan"/"Deep" readings (64F @ 13Z, 78F @ 22Z on 2026-06-24) match KDAY's
    # actual obs, not Kokomo/Tipton. tiny.en just mangles Dayton some runs.
    "Dayton": [
        "Deepan", "Deep", "Deepin", "Deepen",
    ],
}

# (table, [key columns other than city]) — the non-city half of each PK, used
# to detect a collision before renaming.
TABLES = [
    ("city_conditions", ["condition"]),
    ("city_observations", ["captured_at", "condition"]),
    ("forecasts", ["issued_at", "period"]),
]


def _connect() -> pg8000.native.Connection:
    return pg8000.native.Connection(
        user=os.environ.get("WX_PG_USER", "wxparser"),
        host=os.environ.get("WX_PG_HOST", "127.0.0.1"),
        port=int(os.environ.get("WX_PG_PORT", "5432")),
        database=os.environ.get("WX_PG_DATABASE", "wxparser"),
        password=os.environ.get("WX_PG_PASSWORD") or None,
    )


def _merge_table(conn, table: str, keys: list[str], good: str, bad: str) -> int:
    """Fold rows whose city == bad (case-insensitive) into city == good.

    Returns the number of bad rows affected (merged-away + renamed).
    """
    # how many bad rows are there to begin with?
    n = conn.run(
        f"SELECT COUNT(*) FROM {table} WHERE LOWER(city)=LOWER(:bad) AND city<>:good",
        bad=bad, good=good,
    )[0][0]
    if not n:
        return 0

    key_match = " AND ".join(f"g.{k}=b.{k}" for k in keys)

    if table == "city_conditions":
        # A bad row may carry a newer reading or extra sightings than the good
        # row for the same condition — preserve both: sum sightings, keep the
        # newer value/last_seen, keep the earlier first_seen.
        conn.run(
            "UPDATE city_conditions g SET "
            "  sightings  = g.sightings + b.sightings,"
            "  first_seen = LEAST(g.first_seen, b.first_seen),"
            "  last_seen  = GREATEST(g.last_seen, b.last_seen),"
            "  value_num  = CASE WHEN b.last_seen > g.last_seen THEN b.value_num  ELSE g.value_num  END,"
            "  value_text = CASE WHEN b.last_seen > g.last_seen THEN b.value_text ELSE g.value_text END,"
            "  votes      = CASE WHEN b.last_seen > g.last_seen THEN b.votes      ELSE g.votes      END,"
            "  total      = CASE WHEN b.last_seen > g.last_seen THEN b.total      ELSE g.total      END "
            "FROM city_conditions b "
            f"WHERE g.city=:good AND LOWER(b.city)=LOWER(:bad) AND b.city<>:good AND {key_match}",
            good=good, bad=bad,
        )

    # Drop bad rows that would collide with an existing good row, then rename
    # the survivors. (For city_observations / forecasts a collision means the
    # identical reading was already recorded under the correct spelling, so the
    # bad copy is pure duplicate.)
    conn.run(
        f"DELETE FROM {table} b WHERE LOWER(b.city)=LOWER(:bad) AND b.city<>:good "
        f"AND EXISTS (SELECT 1 FROM {table} g WHERE g.city=:good AND {key_match})",
        good=good, bad=bad,
    )
    conn.run(
        f"UPDATE {table} SET city=:good WHERE LOWER(city)=LOWER(:bad) AND city<>:good",
        good=good, bad=bad,
    )
    return n


def main() -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn = _connect()
    total = 0
    try:
        for good, variants in CORRECTIONS.items():
            for bad in variants:
                for table, keys in TABLES:
                    n = _merge_table(conn, table, keys, good, bad)
                    if n:
                        total += n
                        print(f"[{stamp}] {table}: {n:>4} row(s) '{bad}' -> '{good}'")
    finally:
        conn.close()
    if total:
        print(f"[{stamp}] done — corrected {total} row(s).")
    else:
        print(f"[{stamp}] done — nothing to correct.")


if __name__ == "__main__":
    main()
