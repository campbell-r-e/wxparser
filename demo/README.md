# wxparser API demo

A one-file shell script for pulling live weather from a **wxparser** node.
wxparser listens to NOAA Weather Radio and serves structured, queryable weather
over a small HTTP/JSON API — **fully offline**.

With no arguments it shows a **live dashboard that refreshes every 30 seconds**
(press **q** to quit); it can also call any other endpoint.

## Point it at your node

The script never hardcodes an address. Give it one via the `WX_HOST` env var,
or a `.env` file next to the script (gitignored, so site addresses stay out of
the repo):

```bash
cp demo/.env.example demo/.env     # then edit WX_HOST
# or one-off:
WX_HOST=mynode:8080 demo/wx.sh
```

`WX_HOST` may list several `host:port` candidates (space-separated); they are
tried in order and the first live one wins — handy for "VPN address first,
LAN address as fallback". The environment variable takes precedence over the
`.env` file. Default is `localhost:8080`.

## Run

```bash
demo/wx.sh                 # live dashboard, refresh every 30s   (q to quit)
demo/wx.sh watch 10        # live dashboard, custom interval (seconds)
demo/wx.sh now             # one snapshot, no loop
```

In the dashboard, **q** quits and any other key refreshes immediately.

## All endpoints

```bash
demo/wx.sh bulletin        # read-on-air net bulletin (plain text, EmComm/SKYWARN)
demo/wx.sh sitrep          # situation report (Winlink-pasteable / printable)
demo/wx.sh aprs            # APRS weather report + alert bulletins
demo/wx.sh health          # pipeline liveness (is the node still hearing the radio?)
demo/wx.sh cities          # cities with data
demo/wx.sh forecast        # latest forecast
demo/wx.sh almanac         # climate recap: sunrise/sunset, YTD precip, degree days
demo/wx.sh conditions      # available conditions
demo/wx.sh alerts          # active SAME alerts
demo/wx.sh transcripts     # last 10 raw transcripts
demo/wx.sh menu            # list every endpoint
demo/wx.sh get /any/path   # call ANY endpoint and pretty-print
```

`get` reaches everything, e.g.:

```bash
demo/wx.sh get /city/Muncie
demo/wx.sh get '/conditions/temperature_f'
demo/wx.sh get '/export?since=2026-06-26T00:00:00Z'
```

Only needs `curl` and `python3`. The node must be reachable on your network or
VPN (the API binds `0.0.0.0:8080`). Note: `/health` intentionally answers
**503** when the pipeline is degraded — the script treats that as a live
(◐ degraded) node, not an outage.

## Example dashboard

```
  wxparser  ·  http://mynode:8080
  ─────────────────────────────────────────────────
  ● health: ok   station KJY93   (as of 2026-07-04T23:39:13Z)

  CURRENT — Muncie   (advisory · transcribed from the radio)
     Temperature  78°F
     Sky          partly sunny
     Wind         west at 7
     Humidity     63%
     Pressure     29.96 in (rising)

  REGIONAL TEMPERATURES
     Anderson 76   Bloomington 78   Champaign 76   Chicago 78 ...

  ALMANAC   (advisory)
     Sun          rise 6:17 AM · set 9:15 PM
     Precip YTD   17.64 in (3.58 in below normal)
     Degree days  heating 0 · cooling 20

  FORECAST   (advisory · '?' = airings disagreed)
     Tonight          low 61, mostly cloudy, rain 50%?
     Saturday         high 78, partly cloudy, rain 30%
     ...

  ✓ no active NWS warnings

  ⟳ refreshing every 30s — press q to quit (any other key refreshes now)
```

> SAME alerts are **authoritative** (decoded from the digital signal); the
> transcribed conditions/forecast are **advisory** — the API tags every field so
> you always know which is which.
