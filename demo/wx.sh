#!/usr/bin/env bash
#
# wxparser API demo — pull live weather from a wxparser node.
#
# wxparser listens to NOAA Weather Radio and serves structured, queryable weather
# over a tiny HTTP/JSON API — fully offline. With no arguments this shows a
# live dashboard that refreshes every 30s; it can also call any other endpoint.
#
# Usage:
#   ./wx.sh                 live dashboard, refresh every 30s   (press q to quit)
#   ./wx.sh watch [SECONDS] live dashboard with a custom interval
#   ./wx.sh now             one snapshot, no loop
#   ./wx.sh bulletin        read-on-air net bulletin (plain text)
#   ./wx.sh sitrep          situation report (Winlink/printable)
#   ./wx.sh aprs            APRS weather report + alert bulletins
#   ./wx.sh health          pipeline liveness
#   ./wx.sh cities          cities with data
#   ./wx.sh forecast        latest forecast
#   ./wx.sh almanac         climate recap: sun, YTD precip, degree days
#   ./wx.sh conditions      available conditions
#   ./wx.sh alerts          active SAME alerts
#   ./wx.sh transcripts     last 10 raw transcripts
#   ./wx.sh menu            list every endpoint
#   ./wx.sh get /any/path   call any endpoint and pretty-print
#
# Host:  set WX_HOST to one or more host:port candidates (space-separated,
#        tried in order) — in the environment, or in a .env file next to this
#        script (gitignored, so site addresses stay out of the repo).
#        Defaults to localhost:8080. See .env.example.
# Needs: curl + python3.

set -uo pipefail   # not -e, so the watch loop survives a transient hiccup

here="$(cd "$(dirname "$0")" && pwd)"
# WX_HOST from the environment wins; otherwise a gitignored .env beside the
# script may provide it.
if [ -z "${WX_HOST:-}" ] && [ -f "${here}/.env" ]; then
    . "${here}/.env"
fi
CANDIDATES=(${WX_HOST:-localhost:8080})
HOST="" BASE="" HEALTH=""
INTERVAL="${WX_INTERVAL:-30}"

# Find a live node: first candidate whose /health answers. 503 counts as alive —
# the API deliberately returns 503 when the pipeline is degraded, and "degraded"
# is data, not an outage. Caches the answering host (and its health JSON).
probe() {
    local h resp code
    for h in ${HOST:+"$HOST"} "${CANDIDATES[@]}"; do
        resp=$(curl -sS --max-time 4 -w '\n%{http_code}' "http://${h}/health" 2>/dev/null) || continue
        code="${resp##*$'\n'}"
        case "$code" in
            200|503) HOST="$h"; BASE="http://${h}"; HEALTH="${resp%$'\n'*}"; return 0 ;;
        esac
    done
    HOST="" BASE="" HEALTH=""
    return 1
}

fetch() {
    [ -n "$HOST" ] || probe || return 1
    curl -fsS --max-time 10 "${BASE}${1}" && return 0
    HOST=""            # node went away — re-probe on the next call
    return 1
}

usage() { sed -n '2,31p' "$0" | sed 's/^# \{0,1\}//'; }

# ---- pretty dashboard for /now (+ /health) ------------------------------- #
render() {   # args: <health-json> <now-json>
    WX_HEALTH="$1" WX_NOW="$2" python3 - <<'PY'
import json, os

h = json.loads(os.environ["WX_HEALTH"])
d = json.loads(os.environ["WX_NOW"])

dot = {"ok": "●", "degraded": "◐", "down": "○"}.get(h.get("status"), "?")
print(f"  wxparser  ·  {os.environ.get('WX_BASE','')}")
print("  " + "─" * 49)
print(f"  {dot} health: {h.get('status')}   station {d.get('station')}   "
      f"(as of {d.get('generated_at')})")

cb = {c["condition"]: c for c in d.get("conditions", [])}
def g(k): return cb[k]["value"] if k in cb else None
print(f"\n  CURRENT — {d.get('city')}   (advisory · transcribed from the radio)")
keymap = {"Temperature": "temperature_f", "Sky": "sky", "Wind": "wind",
          "Humidity": "humidity_pct", "Pressure": "pressure_in"}
rows = [
    ("Temperature", f"{g('temperature_f')}°F" if g('temperature_f') is not None else None),
    ("Sky",         g("sky")),
    ("Wind",        g("wind")),
    ("Humidity",    f"{g('humidity_pct')}%" if g('humidity_pct') is not None else None),
    ("Pressure",    (f"{g('pressure_in')} in"
                     + (f" ({g('pressure_trend')})" if g('pressure_trend') else ""))
                    if g('pressure_in') is not None else None),
]
for label, val in rows:
    if val is not None:
        flag = " *stale" if cb.get(keymap[label], {}).get("stale") else ""
        print(f"     {label:<12} {val}{flag}")

ru = d.get("roundup", [])
if ru:
    print("\n  REGIONAL TEMPERATURES")
    print("     " + "   ".join(f"{r['city']} {int(round(r['value']))}" for r in ru[:12]))

alm = {a["field"]: a["value"] for a in d.get("almanac", [])}
if alm:
    lines = []
    if alm.get("sunrise") or alm.get("sunset"):
        lines.append(("Sun", " · ".join(x for x in (
            f"rise {alm['sunrise']}" if alm.get("sunrise") else None,
            f"set {alm['sunset']}" if alm.get("sunset") else None) if x)))
    if alm.get("precip_year_in") is not None:
        dep = alm.get("precip_departure_in")
        extra = (f" ({abs(dep)} in {'below' if dep < 0 else 'above'} normal)"
                 if dep is not None else "")
        lines.append(("Precip YTD", f"{alm['precip_year_in']} in{extra}"))
    dd = [f"{k.split('_')[0]} {alm[k]}" for k in
          ("heating_degree_days", "cooling_degree_days") if alm.get(k) is not None]
    if dd:
        lines.append(("Degree days", " · ".join(dd)))
    if lines:
        print("\n  ALMANAC   (advisory)")
        for label, val in lines:
            print(f"     {label:<12} {val}")

fc = d.get("forecast", [])
periods = fc[0]["periods"] if fc and fc[0].get("periods") else []
if periods:
    print("\n  FORECAST   (advisory · '?' = airings disagreed)")
    # freshness: issued = when the voted content last changed; heard on-air =
    # the newest airing, unchanged repeats included (drives the stale flag)
    def age(mins):
        return f"{int(round(mins))}m" if mins < 90 else f"{mins / 60:.1f}h"
    meta, bits = fc[0], []
    if meta.get("age_minutes") is not None:
        bits.append(f"issued {age(meta['age_minutes'])} ago")
    if meta.get("confirmed_age_minutes") is not None:
        bits.append(f"heard on-air {age(meta['confirmed_age_minutes'])} ago")
    if bits:
        flag = " *stale" if meta.get("stale") else ""
        print(f"     {' · '.join(bits)}{flag}")
    for p in periods[:6]:
        unc = set(p.get("uncertain") or [])
        def q(field): return "?" if field in unc else ""
        bits = []
        if p.get("high_f") is not None: bits.append(f"high {p['high_f']}{q('high_f')}")
        if p.get("low_f")  is not None: bits.append(f"low {p['low_f']}{q('low_f')}")
        if p.get("sky"):                bits.append(f"{p['sky']}{q('sky')}")
        if p.get("precip_pct") is not None: bits.append(f"rain {p['precip_pct']}%{q('precip_pct')}")
        print(f"     {p['period']:<16} {', '.join(bits)}")

alerts = d.get("alerts", [])
print()
if alerts:
    print("  ⚠ ACTIVE WARNINGS  (SAME · authoritative)")
    for a in alerts:
        area = ", ".join(a.get("counties") or a.get("areas") or [])
        print(f"     {a.get('event_label', a.get('event'))} — {area} — until {a.get('expires_at')}")
else:
    print("  ✓ no active NWS warnings")
PY
}

snapshot() {
    local now
    probe || { echo "  ✖ no wxparser node answering (tried: ${CANDIDATES[*]}) — set WX_HOST or demo/.env"; return 1; }
    now=$(fetch /now) || { echo "  ✖ ${BASE}/now failed"; return 1; }
    WX_BASE="$BASE" render "$HEALTH" "$now"
}

watch_loop() {
    local retry=3 wait_s                              # fast re-poll while the node is unreachable
    while true; do
        printf '\033[2J\033[3J\033[H'                 # clear screen + scrollback
        if snapshot; then
            wait_s="$INTERVAL"
            printf '\n  ⟳ refreshing every %ss — press q to quit (any other key refreshes now)\n' "$INTERVAL"
        else
            wait_s="$retry"                           # node down (e.g. mid-deploy) — reconnect quickly
            printf '\n  ⟳ reconnecting every %ss — press q to quit\n' "$retry"
        fi
        if read -r -s -n 1 -t "$wait_s" key; then
            case "$key" in q|Q) printf '\n  bye.\n'; return 0 ;; esac
        fi
    done
}

# ---- generic endpoint call (JSON pretty-printed, text passed through) ----- #
call() {
    local path="$1" resp ct
    # probe here, not inside the $(fetch) subshell, so HOST/BASE persist
    probe || { echo "  ✖ no wxparser node answering (tried: ${CANDIDATES[*]}) — set WX_HOST or demo/.env"; return 1; }
    resp=$(fetch "$path") || { echo "  request to ${path} failed"; return 1; }
    ct=$(curl -fsS -o /dev/null -w '%{content_type}' "${BASE}${path}" 2>/dev/null || true)
    case "$ct" in
        application/json*) printf '%s' "$resp" | python3 -m json.tool ;;
        *)                 printf '%s\n' "$resp" ;;
    esac
}

health_cmd() {   # /health answers 503 when degraded — still JSON worth showing
    probe || { echo "  ✖ no wxparser node answering (tried: ${CANDIDATES[*]}) — set WX_HOST or demo/.env"; return 1; }
    printf '%s' "$HEALTH" | python3 -m json.tool
}

cmd="${1:-watch}"
case "$cmd" in
    watch)              INTERVAL="${2:-$INTERVAL}"; watch_loop ;;
    now)                snapshot ;;
    bulletin)           call /bulletin ;;
    sitrep)             call /sitrep ;;
    aprs)               call "/aprs?format=text" ;;
    health)             health_cmd ;;
    cities)             call /cities ;;
    forecast)           call /forecast ;;
    almanac)            call /almanac ;;
    conditions)         call /conditions ;;
    alerts)             call /alerts/active ;;
    transcripts)        call "/transcripts?limit=10" ;;
    menu|endpoints)     call / ;;
    get)                call "${2:?usage: ./wx.sh get /path}" ;;
    -h|--help|help)     usage ;;
    /*)                 call "$cmd" ;;                # a raw /path also works
    *)                  echo "unknown command: $cmd"; echo; usage; exit 1 ;;
esac
