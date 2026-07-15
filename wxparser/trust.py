"""Per-field trust scoring for transcribed readings.

The digital SAME header is authoritative; everything transcribed from the voice
loop is advisory. A transcribed field's trust is derived from the corroboration
signals we have: how strongly the repeat-vote agreed, how many times it was
heard, and whether it's gone stale. (whisper's per-token probability is now
captured as stt.avg_confidence per transcript — a per-utterance quality signal —
but trust here is a per-field, cross-airing measure, so it stays vote-based.)
Every transcribed reading is tagged
`source: "stt", advisory: true`; SAME data is tagged authoritative so a consumer
can always tell the life-safety source from the enrichment.
"""

from __future__ import annotations

# Defaults for the scoring knobs; deployments tune them through Config
# (WX_TRUST_SIGHTINGS_FULL / WX_TRUST_HIGH / WX_TRUST_LOW) — the API passes its
# cfg values through mark(). Kept as plain parameters so this module stays a
# pure function of its inputs.
SIGHTINGS_FULL = 6.0   # sightings before the "seen enough" factor saturates
LABEL_HIGH = 0.66      # trust >= this -> "high"
LABEL_LOW = 0.33       # trust >= this -> "medium"; below -> "low"


def field_trust(votes, total, sightings, stale: bool = False, *,
                sightings_full: float = SIGHTINGS_FULL,
                high: float = LABEL_HIGH, low: float = LABEL_LOW) -> dict:
    """Trust for one voted STT field: agreement × seen-enough, halved if stale."""
    votes = votes or 0
    total = total or 0
    sightings = sightings if sightings is not None else total
    agreement = votes / total if total else 0.0
    seen = min(1.0, (sightings or 0) / sightings_full)
    trust = round(agreement * seen * (0.5 if stale else 1.0), 2)
    label = "high" if trust >= high else "medium" if trust >= low else "low"
    return {"source": "stt", "advisory": True, "trust": trust,
            "confidence": label, "agreement": round(agreement, 2)}


def mark(rows: list[dict], *, sightings_full: float = SIGHTINGS_FULL,
         high: float = LABEL_HIGH, low: float = LABEL_LOW) -> list[dict]:
    """Annotate voted readings in place with a trust block."""
    for r in rows:
        r.update(field_trust(r.get("votes"), r.get("total"),
                             r.get("sightings"), bool(r.get("stale")),
                             sightings_full=sightings_full, high=high, low=low))
    return rows
