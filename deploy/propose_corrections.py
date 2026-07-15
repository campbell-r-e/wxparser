#!/usr/bin/env python3
"""Mine transcripts and PROPOSE stt_terms corrections — a review tool, no runtime effect.

Generalizes the manual "find consistent STT garbles" pass: scans the raw_reports
store for tokens sitting where known forecast vocabulary belongs (the temperature-band and
precip slots that, when garbled, silently break extraction), clusters each to the
word it most likely garbles, and prints reviewable proposals with:

  * frequency in the slot,
  * a SAFETY verdict — SAFE (fold globally) vs CONTEXT-SCOPE (the token is a real
    word seen elsewhere, e.g. "close", so it must be scoped to the slot) vs SKIP,
  * example contexts.

It NEVER edits anything. Output is for adding to wxparser/data/stt_terms.py by hand
after review — the human keeps the final say on whether a swap is safe, which is
exactly why an offline data source shouldn't auto-rewrite its own transcripts.

Run on the box (reads the raw_reports Postgres store):
    python3 deploy/propose_corrections.py [--min-count 2]
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from wxparser.config import Config                        # noqa: E402
from wxparser.data.place_names import place_corrections   # noqa: E402
from wxparser.data.stt_terms import TERM_CORRECTIONS     # noqa: E402
from wxparser.db import Database                          # noqa: E402
from wxparser.extract import _DECADE_WORDS               # noqa: E402

DECADE = (r"(?:\d{1,3}s|twenties|thirties|forties|fifties|sixties|seventies|eighties"
          r"|nineties|naddies|netties|negies|nadies|naughties|naggies|aidies|aighties"
          r"|eddies|adias)")

# Real words — a garble token that IS one of these can't be folded globally (it
# would corrupt legit text), so it's flagged CONTEXT-SCOPE/SKIP rather than SAFE.
COMMON_WORDS = {
    "the", "a", "an", "of", "in", "to", "and", "is", "was", "are", "with", "at", "for",
    "on", "this", "those", "these", "that", "close", "once", "he", "we", "good", "trace",
    "near", "around", "from", "then", "not", "as", "cool", "far", "through", "toward",
    "into", "up", "by", "or", "be", "it", "but", "out", "off", "down", "over", "low",
    "high", "hot", "warm", "cold", "rain", "snow", "wind", "winds", "clear", "sunny",
    "cloudy", "partly", "mostly", "highs", "lows", "chance", "chances", "showers",
    "thunderstorms", "thunderstorm", "percent", "degrees", "scattered", "isolated",
    "precipitation", "humidity", "dewpoint", "pressure", "barometric", "sunrise",
    "sunset", "normal", "mph", "fog", "frost", "temperature", "temperatures",
    "north", "south", "east", "west", "northwest", "northeast", "southwest", "southeast",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "night", "tonight", "today", "afternoon", "evening", "morning", "overnight",
    # ordinary words that show up rarely in a slot but are real (would be false-SAFE)
    "tree", "trees", "line", "lines", "plain", "plains", "time", "times", "pine",
    "pines", "area", "areas", "day", "days", "rivers", "river", "lake", "lakes",
}
HANDLED = {v.lower() for vs in TERM_CORRECTIONS.values() for v in vs} | {"close", "flows"}
_CORRECTIONS = place_corrections()
PLACES = ({c.lower() for c in _CORRECTIONS}
          | {v.lower() for vs in _CORRECTIONS.values() for v in vs})


def temp_label(tok: str) -> str | None:
    """Whether a temp-band garble rhymes with 'highs' or 'lows' (the slot itself is
    ambiguous; the vowel sound disambiguates)."""
    t = tok.lower()
    if re.search(r"ow|ose|oes|oze", t):
        return "Lows"
    if re.search(r"igh|ies|yes|ize|uys", t) or "y" in t:
        return "Highs"
    if "o" in t:
        return "Lows"
    if "i" in t:
        return "Highs"
    return None


# (slot name, regex capturing the filler word, canonical or None=use temp_label)
SLOTS = [
    ("temp-band  '<X> in the <lower|mid|upper> <band>'",
     re.compile(rf"\b(\w+)\s+in the (?:lower|low|mid|middle|upper)\s+{DECADE}", re.I), None),
    ("precip     '<X> of {rain|showers|thunderstorms}'",
     re.compile(r"\b(\w+)\s+of (?:rain|showers|thunderstorms?|precipitation|snow|flurries)",
                re.I), "Chance"),
]


def load_texts(db: Database) -> list[str]:
    """Every raw transcript text from the Postgres raw store, in capture order."""
    return [t for rec in db.iter_raw_reports() if (t := (rec.get("text") or "").strip())]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--min-count", type=int, default=2)
    ap.add_argument("--examples", type=int, default=2)
    args = ap.parse_args()

    db = Database(Config())
    try:
        texts = load_texts(db)
    finally:
        db.close()
    blob = "\n".join(texts)
    low = blob.lower()
    word_counts = Counter(re.findall(r"[a-z]+", low))  # whole-corpus token frequency
    print(f"scanned {len(texts)} transcripts\n")

    proposals: list[dict] = []
    for name, rx, canon in SLOTS:
        fillers = Counter(m.group(1).lower() for m in rx.finditer(low))
        for tok, in_slot in fillers.most_common():
            if in_slot < args.min_count:
                break
            target = canon or temp_label(tok)
            if target is None or tok == target.lower() or tok in HANDLED or tok in PLACES:
                continue
            if tok in {c.lower() for cs in [TERM_CORRECTIONS] for c in cs}:  # already a canonical
                continue
            total = word_counts[tok]
            out_slot = total - in_slot
            if tok in COMMON_WORDS:
                verdict = "CONTEXT-SCOPE/SKIP — legit word; scope to slot or skip"
            elif out_slot > max(1, in_slot // 2):
                verdict = f"CONTEXT-SCOPE — also seen {out_slot}x outside this slot"
            else:
                verdict = "SAFE — fold globally in stt_terms"
            ex = []
            for m in rx.finditer(blob):
                if m.group(1).lower() == tok:
                    s = max(0, m.start() - 18)
                    ex.append("..." + blob[s:m.end() + 12].replace("\n", " ").strip() + "...")
                    if len(ex) >= args.examples:
                        break
            proposals.append({"slot": name, "tok": tok, "target": target, "count": in_slot,
                              "verdict": verdict, "ex": ex})

    proposals.sort(key=lambda p: (-p["count"], p["slot"]))
    if not proposals:
        print("no new garble candidates above the threshold — corpus looks clean.")
        return 0

    print(f"{'COUNT':>5}  {'GARBLE':<12} -> {'CANONICAL':<10}  VERDICT")
    print("-" * 78)
    for p in proposals:
        print(f"{p['count']:>5}  {p['tok']:<12} -> {p['target']:<10}  {p['verdict']}")
        print(f"        slot: {p['slot']}")
        for e in p["ex"]:
            print(f"        e.g. {e}")
    print()

    # --- number/decade mishearings -------------------------------------- #
    # A garbled decade word ("eighties" -> "aidies") in "highs in the lower <X>"
    # drops the number or lands it on the wrong decade (off by ~10F). Surface
    # tokens in that slot that aren't a recognised decade for mapping into
    # extract._DECADE_WORDS.
    known = {d.lower() for d in _DECADE_WORDS}
    dec = Counter()
    _RANGE = {"and", "to", "or", "the", "through", "into"}  # "lower and upper 70s" etc.
    for m in re.finditer(r"\bin the (?:lower|low|mid|middle|upper)\s+([a-z]+)", low):
        t = m.group(1)
        if t not in known and t not in _RANGE and not re.match(r"\d{1,3}s$", t):
            dec[t] += 1
    dec_cand = [(t, n) for t, n in dec.most_common() if n >= args.min_count]
    if dec_cand:
        print("Decade/number mishearings (map to a decade in extract._DECADE_WORDS):")
        for t, n in dec_cand:
            print(f"   {n:4}  {t!r}")
        print()

    safe = [p for p in proposals if p["verdict"].startswith("SAFE")]
    if safe:
        by_canon: dict[str, list[str]] = {}
        for p in safe:
            by_canon.setdefault(p["target"], []).append(p["tok"])
        print("Ready-to-review stt_terms additions (SAFE folds only):")
        for canon, toks in by_canon.items():
            variants = ", ".join(f'"{t.title()}"' for t in sorted(set(toks)))
            print(f'    "{canon}": [..., {variants}],')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
