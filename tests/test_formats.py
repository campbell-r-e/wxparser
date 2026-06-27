"""EmComm output-format tests (net bulletin, sitrep, APRS)."""

from __future__ import annotations

from wxparser.formats import aprs_bulletins, aprs_weather, net_bulletin, sitrep


def _snap(alerts=None):
    return {
        "generated_at": "2026-06-25T21:45:43Z", "station": "KJY93", "city": "Muncie",
        "conditions": [
            {"condition": "temperature_f", "value": 81, "stale": False},
            {"condition": "sky", "value": "mostly cloudy", "stale": False},
            {"condition": "wind", "value": "southwest at 15", "stale": True},
            {"condition": "wind_speed_mph", "value": 15, "stale": True},
            {"condition": "humidity_pct", "value": 54, "stale": True},
            {"condition": "pressure_in", "value": 29.94, "stale": True},
            {"condition": "pressure_trend", "value": "falling", "stale": True},
        ],
        "roundup": [{"city": "Anderson", "value": 78}, {"city": "Marion", "value": 74}],
        "forecast": [{"city": "Muncie", "periods": [
            {"period": "Tonight", "low_f": 61, "precip_pct": 50, "sky": "mostly cloudy"},
            {"period": "Friday", "high_f": 75, "precip_pct": 90, "sky": "mostly cloudy"},
        ]}],
        "alerts": alerts or [],
    }


_TOR = {"event": "TOR", "event_label": "Tornado Warning",
        "counties": ["Delaware County, IN"], "expires_at": "2026-06-25T22:30:00Z",
        "spoken": [{"spotter_activation": True, "until": "6:30 PM", "threats": ["tornado"]}]}


def test_net_bulletin_no_alerts():
    out = net_bulletin(_snap())
    assert "WX BULLETIN -- KJY93 Muncie -- 2026-06-25 2145Z" in out
    assert "WARNINGS (SAME): NONE ACTIVE" in out
    assert "Temp 81F" in out and "Wind southwest at 15" in out
    assert "Tonight: low 61, mostly cloudy, rain 50%" in out


def test_net_bulletin_with_tornado_and_spotters():
    out = net_bulletin(_snap([_TOR]))
    assert "ACTIVE WARNINGS (SAME -- authoritative)" in out
    assert "TORNADO WARNING -- Delaware County, IN -- until 2230Z" in out
    assert "SPOTTERS ACTIVATED" in out


def test_sitrep_structure():
    out = sitrep(_snap([_TOR]))
    assert out.startswith("WEATHER SITUATION REPORT")
    assert "== ACTIVE WARNINGS (SAME) ==" in out
    assert "Tornado Warning | Delaware County, IN | until 2230Z" in out
    assert "Anderson 78, Marion 74" in out
    assert "Temperature (F): 81" in out


def test_aprs_weather_report_format():
    s = aprs_weather(_snap())
    # _MDDHHMM c<dir> s<spd> g... t<temp> h<hum> b<baro tenths-mb>
    assert s.startswith("_06252145")
    assert "c225" in s          # southwest -> 225 deg
    assert "s015" in s          # 15 mph
    assert "t081" in s          # 81 F
    assert "h54" in s
    assert "b10139" in s        # 29.94 inHg -> 1013.9 mb -> 10139


def test_aprs_bulletins():
    assert aprs_bulletins(_snap())[0].startswith(":BLN1WX   :No active NWS warnings")
    b = aprs_bulletins(_snap([_TOR]))[0]
    assert b.startswith(":BLN1WX   :")
    assert "Tornado Warning" in b and "til 2230Z" in b and "SPOTTERS" in b
    assert len(b.split(":", 2)[2]) <= 67   # APRS bulletin body cap


def test_formatters_tolerate_missing_values():
    # present-but-absent / empty conditions must not crash the formatters
    from wxparser.formats import _conditions_line, _period_line, aprs_weather
    assert _conditions_line({}) == "no current data"          # temp/humidity None branches
    snap = {"conditions": [], "generated_at": "2026-06-26T18:00:00Z", "station": "X"}
    assert aprs_weather(snap).startswith("_")                  # no temp/humidity/pressure
    assert _period_line({}) == "?: (no data)"                 # missing period key
