"""STT trust-scoring tests."""

from __future__ import annotations

from wxparser.trust import field_trust, mark


def test_strong_agreement_many_sightings_is_high():
    t = field_trust(votes=6, total=6, sightings=12)
    assert t["confidence"] == "high" and t["advisory"] is True and t["source"] == "stt"
    assert t["agreement"] == 1.0


def test_split_vote_lowers_trust():
    strong = field_trust(votes=6, total=6, sightings=12)["trust"]
    split = field_trust(votes=2, total=6, sightings=12)["trust"]
    assert split < strong


def test_few_sightings_lowers_trust():
    many = field_trust(votes=3, total=3, sightings=12)["trust"]
    few = field_trust(votes=1, total=1, sightings=1)["trust"]
    assert few < many


def test_stale_halves_trust():
    fresh = field_trust(votes=6, total=6, sightings=12, stale=False)["trust"]
    stale = field_trust(votes=6, total=6, sightings=12, stale=True)["trust"]
    assert abs(stale - fresh / 2) < 1e-9


def test_mark_annotates_rows_in_place():
    rows = [{"city": "Muncie", "value": 77, "votes": 2, "total": 2, "sightings": 8}]
    mark(rows)
    assert rows[0]["advisory"] is True and "trust" in rows[0]


def test_knobs_are_tunable():
    # the scoring knobs come from Config in production (WX_TRUST_*); verify the
    # parameters actually steer the outcome
    strict = field_trust(votes=6, total=6, sightings=6, sightings_full=12.0)
    assert strict["trust"] == 0.5                       # saturation point moved
    relabeled = field_trust(votes=6, total=6, sightings=12, high=1.01)
    assert relabeled["confidence"] == "medium"          # same trust, new label
    rows = [{"votes": 6, "total": 6, "sightings": 12}]
    mark(rows, high=1.01, low=0.5)
    assert rows[0]["confidence"] == "medium"
