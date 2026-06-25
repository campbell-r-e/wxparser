"""EmComm output formats (roadmap: net-control bulletin + RF-native formats).

Pure functions over a `/now`-shaped snapshot dict so they're testable without the
DB. Three audiences:
  - net_bulletin(): plain text a net-control operator reads on the air.
  - sitrep():       a fuller situation report — Winlink-pasteable / printable.
  - aprs_weather() / aprs_bulletins(): RF beacon strings (APRS positionless
    weather report + alert bulletins).

The authoritative-vs-advisory distinction is carried into the human formats: SAME
warnings are stated as authoritative; transcribed conditions/forecast are clearly
labelled advisory, so an operator never relays STT as fact.
"""

from __future__ import annotations

_DIR_DEG = {
    "north": 0, "northeast": 45, "east": 90, "southeast": 135,
    "south": 180, "southwest": 225, "west": 270, "northwest": 315,
}


def _by_cond(conditions: list[dict]) -> dict:
    return {c["condition"]: c for c in conditions}


def _zstamp(iso: str) -> str:
    # 2026-06-25T21:45:43Z -> "2026-06-25 2145Z"
    return f"{iso[:10]} {iso[11:13]}{iso[14:16]}Z" if iso else "?"


def _hhmmz(iso) -> str:
    return f"{iso[11:13]}{iso[14:16]}Z" if iso else "?"


def _area(alert: dict) -> str:
    parts = alert.get("counties") or alert.get("areas") or []
    return ", ".join(parts) if parts else "(area n/a)"


def _spotters(alert: dict) -> bool:
    return any(s.get("spotter_activation") for s in (alert.get("spoken") or []))


def _conditions_line(cb: dict) -> str:
    parts = []
    if "temperature_f" in cb:
        parts.append(f"Temp {int(round(cb['temperature_f']['value']))}F")
    if "sky" in cb:
        parts.append(f"Sky {cb['sky']['value']}")
    if "wind" in cb:
        parts.append(f"Wind {cb['wind']['value']}")
    if "humidity_pct" in cb:
        parts.append(f"Humidity {int(round(cb['humidity_pct']['value']))}%")
    if "pressure_in" in cb:
        trend = cb.get("pressure_trend", {}).get("value", "")
        parts.append(f"Pressure {cb['pressure_in']['value']} {trend}".strip())
    return "  ".join(parts) if parts else "no current data"


def _period_line(p: dict) -> str:
    hl = []
    if p.get("high_f") is not None:
        hl.append(f"high {p['high_f']}")
    if p.get("low_f") is not None:
        hl.append(f"low {p['low_f']}")
    if p.get("sky"):
        hl.append(p["sky"])
    if p.get("precip_pct") is not None:
        hl.append(f"rain {p['precip_pct']}%")
    return f"{p['period']}: " + ", ".join(hl) if hl else f"{p['period']}: (no data)"


def _periods(snap: dict) -> list[dict]:
    fc = snap.get("forecast") or []
    return fc[0]["periods"] if fc and fc[0].get("periods") else []


def net_bulletin(snap: dict) -> str:
    """Read-on-air bulletin for a SKYWARN / EmComm net."""
    cb = _by_cond(snap.get("conditions") or [])
    alerts = snap.get("alerts") or []
    L = [f"WX BULLETIN -- {snap.get('station', '?')} {snap.get('city', '')} -- "
         f"{_zstamp(snap.get('generated_at', ''))}", ""]

    if alerts:
        L.append("** ACTIVE WARNINGS (SAME -- authoritative) **")
        for a in alerts:
            tag = "  ** SPOTTERS ACTIVATED **" if _spotters(a) else ""
            L.append(f"  {a.get('event_label', a.get('event', '?')).upper()} -- "
                     f"{_area(a)} -- until {_hhmmz(a.get('expires_at'))}{tag}")
    else:
        L.append("WARNINGS (SAME): NONE ACTIVE")
    L.append("")

    L.append("CURRENT (advisory -- transcribed from voice):")
    L.append("  " + _conditions_line(cb))
    if any(c.get("stale") for c in (snap.get("conditions") or [])):
        L.append("  (* one or more readings past the staleness threshold)")
    L.append("")

    periods = _periods(snap)
    if periods:
        L.append("FORECAST (advisory):")
        for p in periods[:4]:
            L.append("  " + _period_line(p))
    return "\n".join(L) + "\n"


def sitrep(snap: dict) -> str:
    """Fuller situation report — Winlink-pasteable / printable."""
    cb = _by_cond(snap.get("conditions") or [])
    alerts = snap.get("alerts") or []
    L = ["WEATHER SITUATION REPORT",
         f"Source: NOAA Weather Radio {snap.get('station', '?')} "
         f"({snap.get('city', '')}) via wxparser",
         f"Generated: {_zstamp(snap.get('generated_at', ''))}",
         "NOTE: SAME alerts are authoritative; conditions/forecast are transcribed"
         " (advisory).", "",
         "== ACTIVE WARNINGS (SAME) =="]
    if alerts:
        for a in alerts:
            L.append(f"- {a.get('event_label', a.get('event', '?'))} | {_area(a)} | "
                     f"until {_hhmmz(a.get('expires_at'))}"
                     + ("  [SPOTTERS ACTIVATED]" if _spotters(a) else ""))
            for s in (a.get("spoken") or []):
                if s.get("until") or s.get("threats"):
                    L.append(f"    spoken: until {s.get('until', '?')}; "
                             f"threats {', '.join(s.get('threats') or []) or 'n/a'}")
    else:
        L.append("NONE")
    L += ["", f"== CURRENT CONDITIONS -- {snap.get('city', '')} =="]
    label = {"temperature_f": "Temperature (F)", "sky": "Sky",
             "wind": "Wind", "wind_speed_mph": "Wind speed (mph)",
             "humidity_pct": "Humidity (%)", "pressure_in": "Pressure (in)",
             "pressure_trend": "Pressure trend", "dewpoint_f": "Dewpoint (F)"}
    for cond in ("temperature_f", "sky", "wind", "humidity_pct", "pressure_in",
                 "pressure_trend", "dewpoint_f"):
        if cond in cb:
            flag = " *" if cb[cond].get("stale") else ""
            L.append(f"  {label[cond]}: {cb[cond]['value']}{flag}")
    L.append("  (* value past the staleness threshold)")

    roundup = snap.get("roundup") or []
    if roundup:
        L += ["", "== REGIONAL TEMPERATURES (F) =="]
        L.append("  " + ", ".join(f"{r['city']} {int(round(r['value']))}" for r in roundup))

    periods = _periods(snap)
    if periods:
        L += ["", "== FORECAST =="]
        for p in periods:
            L.append("  " + _period_line(p))
    return "\n".join(L) + "\n"


def _t3(temp) -> str:
    if temp is None:
        return "..."
    t = int(round(temp))
    return f"-{abs(t):02d}" if t < 0 else f"{t:03d}"


def aprs_weather(snap: dict) -> str:
    """APRS positionless weather report (beaconable as-is)."""
    cb = _by_cond(snap.get("conditions") or [])
    iso = snap.get("generated_at", "")
    mdhm = (iso[5:7] + iso[8:10] + iso[11:13] + iso[14:16]) if iso else "00000000"

    def f3(v):
        return f"{int(round(v)):03d}" if v is not None else "..."

    deg = _DIR_DEG.get((cb.get("wind", {}).get("value", "").split() or [""])[0].lower())
    spd = cb.get("wind_speed_mph", {}).get("value")
    s = f"_{mdhm}c{f3(deg)}s{f3(spd)}g...t{_t3(cb.get('temperature_f', {}).get('value'))}"
    if "humidity_pct" in cb:
        h = int(round(cb["humidity_pct"]["value"]))
        s += f"h{0 if h >= 100 else h:02d}"
    if "pressure_in" in cb:
        s += f"b{int(round(cb['pressure_in']['value'] * 33.8639 * 10)):05d}"
    return s


def aprs_bulletins(snap: dict) -> list[str]:
    """APRS bulletin lines (`:BLNnWX   :text`, body <= 67 chars)."""
    alerts = snap.get("alerts") or []
    station = snap.get("station", "")
    if not alerts:
        return [f":{'BLN1WX'.ljust(9)}:No active NWS warnings {station}"[:76]]
    out = []
    for i, a in enumerate(alerts[:9]):
        area = (a.get("counties") or a.get("areas") or [""])[0]
        spot = " SPOTTERS" if _spotters(a) else ""
        body = f"{a.get('event_label', a.get('event', 'WX'))} {area} til "\
               f"{_hhmmz(a.get('expires_at'))}{spot}"[:67]
        out.append(f":{f'BLN{i + 1}WX'.ljust(9)}:{body}")
    return out
