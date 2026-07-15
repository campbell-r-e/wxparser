"""dedup.py: text normalization + rolling-window fuzzy dedup."""

from __future__ import annotations

from wxparser.config import Config
from wxparser.dedup import TextDeduper, normalize, similarity


def test_normalize_strips_punctuation_and_case():
    assert normalize("Highs, around 80!  (West winds)") == "highs around 80 west winds"


def test_similarity_bounds():
    assert similarity("", "x") == 0.0
    assert similarity("abc def", "abc def") == 1.0
    assert 0.0 < similarity("abc def", "abc xyz") < 1.0


def _rep(rid, text, pt="zone_forecast"):
    return {"id": rid, "text": text, "product_type": pt}


def test_new_then_duplicate_then_update():
    d = TextDeduper(Config())
    a = d.consider(_rep("1", "Tonight mostly cloudy lows in the lower 60s chance of rain 40%"))
    assert a.kind == "new"
    # text-identical -> duplicate
    b = d.consider(_rep("2", "Tonight mostly cloudy lows in the lower 60s chance of rain 40%"))
    assert b.kind == "duplicate" and b.supersedes == "1"
    # changed-but-similar (0.75 <= sim < 0.97), same product -> update
    c = d.consider(
        _rep("3", "Tonight mostly cloudy lows in the lower 60s chance of rain 40% gusty winds"))
    assert c.kind == "update" and c.supersedes is not None


def test_low_similarity_is_new():
    d = TextDeduper(Config())
    d.consider(_rep("1", "Tonight mostly cloudy lows in the 60s"))
    r = d.consider(_rep("2", "Tornado warning for Delaware County until 630 PM seek shelter"))
    assert r.kind == "new"


def test_prime_skips_records_without_text():
    d = TextDeduper(Config())
    d.prime([{"id": "x", "product_type": "same_alert"},          # no "text" -> skipped
             _rep("1", "Tonight mostly cloudy lows in the lower 60s")])
    dup = d.consider(_rep("2", "Tonight mostly cloudy lows in the lower 60s"))
    assert dup.kind == "duplicate" and dup.supersedes == "1"
