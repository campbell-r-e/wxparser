"""store.py: classification, report building, JSONL query/sync readers."""

from __future__ import annotations

import json
from pathlib import Path

from wxparser.config import Config
from wxparser.store import (
    build_report,
    classify,
    count_reports,
    load_recent_reports,
    query_reports,
    reports_since,
)
from wxparser.stt import Segment, Transcript


def _t(text: str) -> Transcript:
    return Transcript(text=text, segments=[Segment(0.0, 1.0, text)], language="en")


def test_classify_explicit_products():
    assert classify("Tornado warning for Delaware County") == "tornado_warning"
    assert classify("Hazardous weather outlook for central Indiana") == "hazardous_weather_outlook"


def test_classify_conditions_forecast_unknown():
    assert classify("At Muncie, the temperature was 70 degrees.") == "current_conditions"
    assert classify("It was 74 at Marion and 72 at Anderson.") == "current_conditions"
    assert classify("Tonight, mostly cloudy. Lows in the lower 60s.") == "zone_forecast"
    assert classify("A slight chance of showers and thunderstorms.") == "zone_forecast"
    assert classify("This is KJY93 Muncie all hazards radio.") == "unknown"


def test_build_report_fields():
    cfg = Config()
    r = build_report(_t("Highs around 80."), cfg, duration_s=12.34, fingerprint="abc",
                     captured_at="2026-06-24T12:00:00Z")
    assert r["id"].startswith("2026-06-24T12:00:00Z-")
    assert r["product_type"] == "zone_forecast"
    assert r["duration_s"] == 12.3 and r["fingerprint"] == "abc"
    assert r["text"] == "Highs around 80." and r["station"] == cfg.station
    assert r["segments"][0]["text"] == "Highs around 80."


def _write_jsonl(tmp: Path, recs: list[dict]) -> Config:
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "reports.jsonl").write_text(
        "\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
    return Config(out_dir=tmp)


def test_query_count_and_since(tmp_path):
    recs = [
        {"id": "1", "captured_at": "2026-06-24T10:00:00Z", "product_type": "zone_forecast",
         "text": "tonight clear"},
        {"id": "2", "captured_at": "2026-06-24T11:00:00Z", "product_type": "current_conditions",
         "text": "temperature was 70"},
        {"id": "3", "captured_at": "2026-06-24T12:00:00Z", "product_type": "zone_forecast",
         "text": "highs around 80"},
    ]
    cfg = _write_jsonl(tmp_path / "t", recs)
    # newest-first, filtered
    out = query_reports(cfg, limit=10)
    assert [r["id"] for r in out] == ["3", "2", "1"]
    assert [r["id"] for r in query_reports(cfg, product="zone_forecast")] == ["3", "1"]
    assert [r["id"] for r in query_reports(cfg, q="temperature")] == ["2"]
    # pagination via offset
    assert [r["id"] for r in query_reports(cfg, limit=1, offset=1)] == ["2"]
    # counts honour the same filters
    assert count_reports(cfg) == 3
    assert count_reports(cfg, product="zone_forecast") == 2
    # since = strictly-after, ascending
    assert [r["id"] for r in reports_since(cfg, "2026-06-24T10:30:00Z", 10)] == ["2", "3"]
    # recent reports oldest-first
    assert [r["id"] for r in load_recent_reports(cfg, 2)] == ["2", "3"]


def test_query_missing_file_is_empty(tmp_path):
    cfg = Config(out_dir=tmp_path / "nope")
    assert query_reports(cfg) == [] and count_reports(cfg) == 0
    assert reports_since(cfg, "2026-01-01T00:00:00Z", 5) == []
    assert load_recent_reports(cfg, 5) == []


def test_query_skips_bad_json_lines(tmp_path):
    d = tmp_path / "b"
    d.mkdir()
    (d / "reports.jsonl").write_text(
        'not json\n{"id":"1","captured_at":"2026-06-24T10:00:00Z","text":"ok"}\n',
        encoding="utf-8")
    cfg = Config(out_dir=d)
    assert [r["id"] for r in query_reports(cfg)] == ["1"]
    assert count_reports(cfg) == 1
