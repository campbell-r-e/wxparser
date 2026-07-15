"""api.py: integration — run the real HTTP handler against the test DB."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import wxparser.api as api
from wxparser.db import Database
from wxparser.health import Heartbeat


def _server(make_cfg, **cfg_overrides):
    cfg = make_cfg(stream_poll_s=0.05, **cfg_overrides)
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
    db._run("TRUNCATE raw_reports")
    db.insert_raw_report({"id": "t1", "captured_at": "2026-06-24T12:00:00Z",
                          "product_type": "zone_forecast", "text": "tonight clear"})
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


def test_all_json_and_text_endpoints(make_cfg):
    srv, H = _server(make_cfg)
    try:
        assert len(_get(H + "/")["endpoints"]) == 20
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


def test_health_prefers_db_heartbeat(make_cfg):
    """A pipeline on ANOTHER machine publishes through the DB; the API must read
    that row and ignore the local health.json (which the fixture also wrote)."""
    srv, H = _server(make_cfg)
    try:
        api._Handler.db.write_heartbeat(
            "KJY93", {"segments": 42, "updated_at": "2026-06-24T12:00:00Z"})
        try:
            health = _get(H + "/health")
        except urllib.error.HTTPError as e:           # fail-loud 503 is the point
            health = json.loads(e.read().decode())
        assert health["pipeline"]["segments"] == 42   # DB row won over the file
        assert health["status"] == "down"             # and that row is ancient
    finally:
        srv.shutdown()


def test_forecast_confirmation_tracks_reairings(make_cfg):
    srv, H = _server(make_cfg)
    try:
        fc = _get(H + "/forecast")["forecasts"][0]
        # the only recorded airing (12:00Z) predates the issuance (18:00Z) ->
        # confirmation falls back to the issuance itself
        assert fc["last_confirmed_at"] == fc["issued_at"] == "2026-06-24T18:00:00Z"
        assert fc["confirmed_age_minutes"] == fc["age_minutes"]
        # an unchanged re-airing writes no new issuance yet refreshes the
        # confirmation: the forecast is only as stale as its last airing
        api._Handler.db.insert_raw_report(
            {"id": "t2", "captured_at": "2026-06-25T00:00:00Z",
             "product_type": "zone_forecast", "text": "tonight clear"})
        fc = _get(H + "/forecast")["forecasts"][0]
        assert fc["issued_at"] == "2026-06-24T18:00:00Z"        # unmoved
        assert fc["last_confirmed_at"] == "2026-06-25T00:00:00Z"
        assert fc["confirmed_age_minutes"] < fc["age_minutes"]
        assert fc["stale"] is True    # last aired June 2026 -> stale at any age now
        assert _get(H + "/forecast?stale_after=99999999")["forecasts"][0]["stale"] is False
    finally:
        srv.shutdown()


def test_almanac_uses_longer_stale_window(make_cfg):
    """Almanac/climate fields air a few times a day and stay valid until the next
    recap, so they get a longer staleness window than current conditions — the
    same-age reading is 'stale' as a condition but fresh as an almanac field, and
    an explicit ?stale_after= still overrides the almanac default."""
    # window wide enough to clear the (weeks-old) seeded 2026-06-24 almanac rows
    srv, H = _server(make_cfg, almanac_stale_after_min=99_999_999)
    try:
        alm = _get(H + "/almanac")
        assert alm["stale_after_min"] == 99_999_999
        assert alm["almanac"] and all(r["stale"] is False for r in alm["almanac"])
        # /now surfaces the same non-stale almanac (snapshot uses the almanac window)
        assert all(r["stale"] is False for r in _get(H + "/now")["almanac"])
        # current conditions keep the short current-conditions window -> still stale
        city = _get(H + "/city/Muncie?stale_after=60")
        assert city["stale_after_min"] == 60
        # an explicit ?stale_after= overrides the almanac default the other way
        stale = _get(H + "/almanac?stale_after=1")["almanac"]
        assert stale and all(r["stale"] is True for r in stale)
    finally:
        srv.shutdown()


def test_error_codes(make_cfg):
    srv, H = _server(make_cfg)

    def code(path):
        try:
            _get(H + path)
            return 200
        except urllib.error.HTTPError as e:
            return e.code
    try:
        assert code("/export") == 400               # missing since
        assert code("/conditions/history") == 400    # missing condition
        assert code("/stream?since=notadate") == 400  # bad since rejected before the 200 stream
        assert code("/nope") == 404
        # bad numeric params fall back to defaults (covers the parse-except paths)
        assert _get(H + "/transcripts?limit=abc&offset=xyz")["limit"] >= 1
        assert _get(H + "/now?min=abc&stale_after=foo")["station"]
        # ?fresh=1 with a tiny threshold drops the (now-stale) seeded readings
        assert _get(H + "/conditions/temperature?fresh=1&stale_after=1")["cities"] == []
    finally:
        srv.shutdown()


def test_link_details_handles_missing_timestamp(make_cfg):
    # an alert dict lacking captured_at must not crash detail-linking (except path)
    srv, _ = _server(make_cfg)
    try:
        out = api._Handler._link_details(api._Handler, {"event": "TOR"})
        assert out["spoken"] == [] and out["authoritative"] is True
    finally:
        srv.shutdown()


def test_sse_stream_emits(make_cfg):
    srv, H = _server(make_cfg)
    try:
        # since= replay makes the first event batch deterministic: the seeded
        # alert (oldest stamp) must arrive without waiting on live data.
        with urllib.request.urlopen(
                H + "/stream?since=2026-01-01T00:00:00Z", timeout=3) as r:
            data = r.read(400)   # connected line + start of the replay batch
        assert b": connected" in data
        assert b"event: alert" in data
        assert b"data: {" in data
    finally:
        srv.shutdown()


def test_flag_param_parsing_is_uniform():
    f = api._Handler._flag
    assert f({"x": "1"}, "x", False) and f({"x": "true"}, "x", False)
    assert f({"x": "YES"}, "x", False)                      # case-insensitive
    assert not f({"x": "0"}, "x", True) and not f({"x": "false"}, "x", True)
    assert not f({"x": "no"}, "x", True)
    assert f({}, "x", True) is True and f({}, "x", False) is False
    assert f({"x": "junk"}, "x", True) is True              # unrecognized -> default


def test_details_flag_consistent_across_alert_endpoints(make_cfg):
    # regression: ?details=false used to ENABLE linking on /alerts/active while
    # disabling it on /alerts/history — both must honor the same convention now
    srv, H = _server(make_cfg)
    try:
        assert "spoken" not in _get(H + "/alerts/active?details=false")["alerts"][0]
        assert "spoken" in _get(H + "/alerts/active?details=yes")["alerts"][0]
        assert "spoken" in _get(H + "/alerts/history?details=yes")["alerts"][0]
    finally:
        srv.shutdown()


def test_sync_window_no_truncation_and_empty():
    rows = [{"captured_at": f"2026-01-01T00:00:0{i}Z"} for i in range(3)]
    out, nxt, more = api._sync_window({"a": (rows, "captured_at", 5)},
                                      "2026-01-01T00:00:00Z")
    assert out["a"] == rows and nxt == rows[-1]["captured_at"] and more is False
    out, nxt, more = api._sync_window({"a": ([], "captured_at", 5)}, "start")
    assert out["a"] == [] and nxt == "start" and more is False


def test_sync_window_trims_split_stamp_group():
    # 4 rows fetched with limit 3: stamp "03" is split by the fetch horizon, so
    # the cutoff stops at "02" and both "03" rows wait for the next page.
    rows = [{"captured_at": s} for s in ("01", "02", "03", "03")]
    out, nxt, more = api._sync_window({"a": (rows, "captured_at", 3)}, "00")
    assert [r["captured_at"] for r in out["a"]] == ["01", "02"]
    assert nxt == "02" and more is True


def test_sync_window_truncated_section_caps_complete_ones():
    # THE original /export bug: a complete section's newer rows must be deferred
    # (not skipped) when another section is still truncated behind them.
    trunc = [{"captured_at": s} for s in ("01", "02", "03")]  # limit 2
    full = [{"captured_at": s} for s in ("01", "05")]
    out, nxt, more = api._sync_window(
        {"t": (trunc, "captured_at", 2), "f": (full, "captured_at", 5)}, "00")
    assert nxt == "02" and more is True
    assert [r["captured_at"] for r in out["f"]] == ["01"]  # "05" deferred


def test_sync_window_min_across_truncated_sections():
    a = [{"captured_at": s} for s in ("01", "04", "05")]  # limit 2 -> safe "04"
    b = [{"captured_at": s} for s in ("01", "02", "03")]  # limit 2 -> safe "02"
    _, nxt, more = api._sync_window(
        {"a": (a, "captured_at", 2), "b": (b, "captured_at", 2)}, "00")
    assert nxt == "02" and more is True


def test_sync_window_single_stamp_page_still_advances():
    # one capture wider than the whole page: advance through it anyway — a
    # permanently stuck client is worse than the horizon overflow.
    rows = [{"captured_at": "07"} for _ in range(4)]
    out, nxt, more = api._sync_window({"a": (rows, "captured_at", 3)}, "00")
    assert nxt == "07" and len(out["a"]) == 3 and more is True


def test_export_pages_losslessly(make_cfg):
    srv, H = _server(make_cfg)
    try:
        db = api._Handler.db
        for i in range(6):  # 6 rows, paged 2 at a time -> 3+ hops, zero skips
            db.record_reading(
                {"city": f"C{i}", "condition": "temperature_f", "value": 70 + i},
                f"2026-06-25T00:0{i}:00Z")
        got, since, hops = [], "2026-06-24T23:00:00Z", 0
        while True:
            page = _get(H + f"/export?since={since}&limit=2")
            got += [r["captured_at"] + r["city"] for r in page["observations"]]
            since = page["next_since"]
            hops += 1
            assert hops < 20
            if not page["more"]:
                break
        assert got == [f"2026-06-25T00:0{i}:00ZC{i}" for i in range(6)]  # all, once, in order
    finally:
        srv.shutdown()


def test_verify_endpoint(make_cfg):
    srv, H = _server(make_cfg)
    try:
        doc = _get(H + "/verify")
        assert {"temperature", "sky", "rain", "generated_at"} <= set(doc)
        assert doc["city"] == "Muncie"
        # the seeded forecasts/readings are too thin for temp windows, but the
        # document must still be fully formed
        assert doc["temperature"]["high"]["n"] == 0
    finally:
        srv.shutdown()
