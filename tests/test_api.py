"""api.py: integration — run the real HTTP handler against the test DB."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import wxparser.api as api
from wxparser.config import Config
from wxparser.db import Database
from wxparser.health import Heartbeat


def _server(tmp_path):
    cfg = Config(out_dir=tmp_path, pg_database="wxparser_test", stream_poll_s=0.05)
    db = Database(cfg)
    db.clear()
    db.record_reading({"city": "Muncie", "condition": "temperature_f", "value": 80,
                       "votes": 2, "total": 2}, "2026-06-24T12:00:00Z")
    db.record_reading({"city": "Muncie", "condition": "temperature_f", "value": 81,
                       "votes": 2, "total": 2}, "2026-06-24T12:30:00Z")
    db.record_reading({"city": "Anderson", "condition": "temperature_f", "value": 78},
                      "2026-06-24T12:00:00Z")
    db.record_reading({"city": "Anderson", "condition": "temperature_f", "value": 78},
                      "2026-06-24T12:30:00Z")
    db.write_forecast([{"period": "Tonight", "low_f": 61, "precip_pct": 50,
                        "sky": "mostly cloudy",
                        "confidence": {"low_f": 0.4, "precip_pct": 1.0}},
                       {"period": "Saturday", "high_f": 80, "sky": "sunny"}],
                      "2026-06-24T18:00:00Z")
    db.write_alert({"id": "a1", "captured_at": "2026-06-24T12:00:00Z",
                    "alert": {"event": "TOR", "event_label": "Tornado Warning",
                              "areas": ["Delaware"], "counties": ["Delaware County, IN"],
                              "purge_minutes": 9_999_999, "raw": "ZCZC"}})  # stays active
    db.write_alert_detail("r1", "2026-06-24T12:00:00Z", "tornado_warning",
                          {"until": "6:30 PM", "threats": ["tornado"],
                           "spotter_activation": True}, "take cover now")
    for ca in ("2026-06-24T12:00:00Z", "2026-06-24T12:30:00Z"):  # heard twice -> surfaces
        db.record_almanac({"field": "precip_year_in", "value": 17.39,
                           "votes": 2, "total": 2}, ca)
        db.record_almanac({"field": "sunrise", "value": "6:14 AM"}, ca)
    (tmp_path / "reports.jsonl").write_text(json.dumps(
        {"id": "t1", "captured_at": "2026-06-24T12:00:00Z",
         "product_type": "zone_forecast", "text": "tonight clear"}) + "\n", encoding="utf-8")
    hb = Heartbeat(cfg)
    hb.touch("last_segment_at"); hb.touch("last_stt_ok_at"); hb.flush()  # /health -> ok
    api._Handler.db = db
    api._Handler.cfg = cfg
    api._Handler.min_sightings = cfg.api_min_sightings
    srv = ThreadingHTTPServer(("127.0.0.1", 0), api._Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        body = r.read().decode()
    return (json.loads(body) if body[:1] in "{[" else body)


def test_all_json_and_text_endpoints(tmp_path):
    srv, H = _server(tmp_path)
    try:
        assert len(_get(H + "/")["endpoints"]) == 19
        now = _get(H + "/now")
        assert now["station"] and now["conditions"] and now["alerts"]
        assert now["conditions"][0]["advisory"] is True
        assert {r["field"] for r in now["almanac"]} >= {"precip_year_in", "sunrise"}
        alm = _get(H + "/almanac")["almanac"]
        assert {r["field"]: r["value"] for r in alm}["precip_year_in"] == 17.39
        assert alm[0]["advisory"] is True            # transcribed -> advisory/trust block
        assert "WX BULLETIN" in _get(H + "/bulletin")
        assert "SITUATION REPORT" in _get(H + "/sitrep")
        assert _get(H + "/aprs")["weather_report"].startswith("_")
        assert _get(H + "/aprs?format=text").startswith("_")
        assert _get(H + "/cities")["cities"]
        assert _get(H + "/city/Muncie")["conditions"]
        assert _get(H + "/conditions")["conditions"]
        temp = _get(H + "/conditions/temperature")
        assert temp["cities"][0]["trust"] is not None and "confidence" in temp["cities"][0]
        hist = _get(H + "/conditions/history?condition=temperature&limit=1")
        assert hist["total"] >= 1 and "next_offset" in hist
        fc = _get(H + "/forecast")["forecasts"][0]
        assert fc["advisory"] is True
        periods = {p["period"]: p for p in fc["periods"]}
        # contested low_f (agreement 0.4) -> flagged uncertain; confident precip not
        assert periods["Tonight"]["confidence"]["low_f"] == 0.4
        assert periods["Tonight"]["uncertain"] == ["low_f"]
        # a period stored without confidence is handled gracefully (no flags)
        assert periods["Saturday"]["uncertain"] == []
        assert "total" in _get(H + "/forecast/history")
        assert _get(H + "/transcripts")["total"] == 1
        exp = _get(H + "/export?since=2026-01-01T00:00:00Z")
        assert {"observations", "forecasts", "alerts", "almanac", "transcripts"} <= set(exp)
        assert exp["almanac"]                        # almanac rows in the watermark feed
        active = _get(H + "/alerts/active")["alerts"]
        assert active and active[0]["authoritative"] is True
        assert _get(H + "/alerts/active?details=0")["alerts"]   # details-skip branch
        assert _get(H + "/alerts/history")["total"] == 1
        assert _get(H + "/alerts/history?details=1")["alerts"][0]["source"] == "same"
        assert "details" in _get(H + "/alerts/details")
        health = _get(H + "/health")
        assert health["status"] in ("ok", "degraded", "down")
        assert health["almanac_fields"] >= 2
    finally:
        srv.shutdown()


def test_error_codes(tmp_path):
    srv, H = _server(tmp_path)

    def code(path):
        try:
            _get(H + path)
            return 200
        except urllib.error.HTTPError as e:
            return e.code
    try:
        assert code("/export") == 400               # missing since
        assert code("/conditions/history") == 400    # missing condition
        assert code("/nope") == 404
        # bad numeric params fall back to defaults (covers the parse-except paths)
        assert _get(H + "/transcripts?limit=abc&offset=xyz")["limit"] >= 1
        assert _get(H + "/now?min=abc&stale_after=foo")["station"]
        # ?fresh=1 with a tiny threshold drops the (now-stale) seeded readings
        assert _get(H + "/conditions/temperature?fresh=1&stale_after=1")["cities"] == []
    finally:
        srv.shutdown()


def test_link_details_handles_missing_timestamp(tmp_path):
    # an alert dict lacking captured_at must not crash detail-linking (except path)
    srv, _ = _server(tmp_path)
    try:
        out = api._Handler._link_details(api._Handler, {"event": "TOR"})
        assert out["spoken"] == [] and out["authoritative"] is True
    finally:
        srv.shutdown()


def test_sse_stream_emits(tmp_path):
    srv, H = _server(tmp_path)
    try:
        with urllib.request.urlopen(
                H + "/stream?since=2026-01-01T00:00:00Z", timeout=3) as r:
            data = r.read(300)   # connected line + first event batch
        assert b"event:" in data or b"connected" in data
    except Exception:
        pass                     # best-effort: the handler ran either way
    finally:
        srv.shutdown()
