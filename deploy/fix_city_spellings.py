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

# project timestamp format (kept local: this script is standalone by design)
ISO_FMT = "%Y-%m-%dT%H:%M:%SZ"

import pg8000.native

# --- canonical name + its mis-spellings ----------------------------------- #
# The correction table lives in the station profile (place_corrections in
# wxparser/profiles/<name>.json, selected by WX_PROFILE) — the same single
# source of truth extraction uses at write time, so the two can't drift. This
# script re-reads it every run and retro-folds rows stored before a variant
# was added. LOCAL_EXTRAS is for deployment-local variants you are still
# confirming; promote them into the profile once proven.
import json
from pathlib import Path


def _profile_corrections() -> dict[str, list[str]]:
    name = os.environ.get("WX_PROFILE", "kjy93_muncie")
    path = (Path(name) if name.endswith(".json") else
            Path(__file__).resolve().parent.parent
            / "wxparser" / "profiles" / f"{name}.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    return {city: list(variants)
            for city, variants in data.get("place_corrections", {}).items()}


LOCAL_EXTRAS: dict[str, list[str]] = {}

CORRECTIONS: dict[str, list[str]] = _profile_corrections()
for _city, _variants in LOCAL_EXTRAS.items():
    CORRECTIONS.setdefault(_city, []).extend(_variants)

# --- non-city hallucinations to delete outright --------------------------- #
# Tokens whisper emits in a city slot that are NOT garbles of any real station
# in the roundup — forecast prose ("They have danger..."/"They include portions
# of..."), bare conjunctions/fragments, state names, and plausible-but-wrong
# cities the decoder invents. Unlike CORRECTIONS these map to no real city, so
# they are deleted rather than renamed. Matched case-insensitively against the
# WHOLE city string, so e.g. "South" never touches "South Bend". Keep this
# CONSERVATIVE — only add a token once you've confirmed it is not a real city.
JUNK_CITIES: list[str] = [
    "They", "South", "South Ed", "City", "County", "You", "Sh", "Ch",
    "Line", "Pardon", "Ridge", "Shire", "Wood", "Dave", "Mary", "Moore",
    "Lewis", "Beep", "Mattie", "Jampinee", "Chandler", "Fordland", "Lawford",
    "Maris", "The Maddy", "Sarah Holt", "Sarah Hope",
    # state names / out-of-feed cities the decoder invents in a temp slot
    "Indiana", "Indian", "Ohio", "Nevada", "San Diego", "San Andi",
    "Cleveland", "Liverpool", "Madison", "Huntsville", "Burlington",
    "Blue Ridge",
    # "Whi-" cluster: tail-of-sentence garble with no consistent target
    "Whivl", "Whivol", "Whivolm", "Whittle", "Whill", "Whill Hall",
    "Weville", "Wille", "Willem", "Willow", "Wilv", "Evans Hill",
    # "Luev-" cluster: ambiguous between Lima and Louisville, so drop rather
    # than risk folding a reading into the wrong real city.
    "Luev", "Luevo", "Luevle", "Luell", "Luellington", "Luever", "Luv",
    # 2026-07-06 audit: one-off tokens that appeared once in a temp slot —
    # nonsense garbles plus real-but-out-of-roster cities the decoder invents.
    # (Laim / "Terrell Hoeck" / "The Dayton" were confirmed garbles of Lima /
    # Terre Haute / Dayton and moved to CORRECTIONS to fold the reading in.)
    "Looft", "Loughrey", "Lying", "Rich New", "Thunner", "North",
    "Hamilton", "Hudson", "Washington",
    # 2026-07-08 audit: one-off temp-slot tokens with no confident target —
    # "Fairhope reported 74" / "Long reported 64" sit in roster-city "X
    # reported" slots but could be several cities; "Illinois" is a state name.
    "Fairhope", "Long", "Illinois",
    # 2026-07-18: "Lima, Ohio" heard as "Line Ohio" (comma dropped, state welded
    # on) — extraction now strips the trailing state at write time so new rows
    # collapse to the already-junked "Line", but retro-clean the stuck row here.
    "Line Ohio",
]

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
            "  value_num  = CASE WHEN b.last_seen > g.last_seen "
            "THEN b.value_num  ELSE g.value_num  END,"
            "  value_text = CASE WHEN b.last_seen > g.last_seen "
            "THEN b.value_text ELSE g.value_text END,"
            "  votes      = CASE WHEN b.last_seen > g.last_seen "
            "THEN b.votes      ELSE g.votes      END,"
            "  total      = CASE WHEN b.last_seen > g.last_seen "
            "THEN b.total      ELSE g.total      END "
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


def _drop_junk(conn, table: str, junk: str) -> int:
    """Delete rows whose city == junk (case-insensitive). Returns rows removed."""
    n = conn.run(
        f"SELECT COUNT(*) FROM {table} WHERE LOWER(city)=LOWER(:junk)",
        junk=junk,
    )[0][0]
    if n:
        conn.run(
            f"DELETE FROM {table} WHERE LOWER(city)=LOWER(:junk)",
            junk=junk,
        )
    return n


def main() -> None:
    stamp = datetime.now(timezone.utc).strftime(ISO_FMT)
    conn = _connect()
    total = 0
    dropped = 0
    try:
        for good, variants in CORRECTIONS.items():
            for bad in variants:
                for table, keys in TABLES:
                    n = _merge_table(conn, table, keys, good, bad)
                    if n:
                        total += n
                        print(f"[{stamp}] {table}: {n:>4} row(s) '{bad}' -> '{good}'")
        for junk in JUNK_CITIES:
            for table, _keys in TABLES:
                n = _drop_junk(conn, table, junk)
                if n:
                    dropped += n
                    print(f"[{stamp}] {table}: {n:>4} junk row(s) '{junk}' deleted")
    finally:
        conn.close()
    if total or dropped:
        print(f"[{stamp}] done — corrected {total} row(s), dropped {dropped} junk row(s).")
    else:
        print(f"[{stamp}] done — nothing to correct.")


if __name__ == "__main__":
    main()
