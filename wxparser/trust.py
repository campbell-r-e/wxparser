"""Per-field trust scoring (roadmap: STT trust/confidence layer).

The digital SAME header is authoritative; everything transcribed from the voice
loop is advisory. whisper-cli doesn't expose usable per-token confidence
(avg_confidence comes back 0), so a transcribed field's trust is derived from the
signals we *do* have: how strongly the repeat-vote agreed, how many times it was
heard, and whether it's gone stale. Every transcribed reading is tagged
`source: "stt", advisory: true`; SAME data is tagged authoritative so a consumer
can always tell the life-safety source from the enrichment.
"""

from __future__ import annotations

# how many sightings before the "seen enough" factor saturates
_SIGHTINGS_FULL = 6.0


def field_trust(votes, total, sightings, stale: bool = False) -> dict:
    """Trust for one voted STT field: agreement × seen-enough, halved if stale."""
    votes = votes or 0
    total = total or 0
    sightings = sightings if sightings is not None else total
    agreement = votes / total if total else 0.0
    seen = min(1.0, (sightings or 0) / _SIGHTINGS_FULL)
    trust = round(agreement * seen * (0.5 if stale else 1.0), 2)
    label = "high" if trust >= 0.66 else "medium" if trust >= 0.33 else "low"
    return {"source": "stt", "advisory": True, "trust": trust,
            "confidence": label, "agreement": round(agreement, 2)}


def mark(rows: list[dict]) -> list[dict]:
    """Annotate voted readings in place with a trust block."""
    for r in rows:
        r.update(field_trust(r.get("votes"), r.get("total"),
                             r.get("sightings"), bool(r.get("stale"))))
    return rows
