"""LAN-only HTTP/JSON query API (PLAN §6) — generic, condition-centric.

Serves structured weather data sourced entirely from the radio, fully offline.
Stdlib http.server only (no FastAPI/uvicorn dependency, §2.1). Read-only.

    GET /now?city=                        -> one-call snapshot: a city's full ob,
                                             the roundup, latest forecast, alerts
    GET /bulletin?city=                   -> plain-text read-on-air net bulletin (EmComm)
    GET /sitrep?city=                     -> plain-text situation report (Winlink/print)
    GET /aprs?city=&format=text           -> APRS weather report + alert bulletins
    GET /cities                           -> cities with data + freshness
    GET /city/{city}                      -> every current condition for one city
    GET /conditions                       -> available conditions (index)
    GET /conditions/{condition}           -> every city's latest value for it
    GET /conditions/history?condition=&city=&from=&to=&limit=&offset=
                                          -> historical readings (paginated)
    GET /almanac                          -> climate recap: sunrise/sunset, YTD
                                             precip + departure, degree days
    GET /forecast                         -> latest forecast (+ staleness)
    GET /forecast/history?from=&to=&city=&limit=&offset=
                                          -> historical forecast predictions
    GET /transcripts?from=&to=&q=&product=&limit=&offset=
                                          -> raw transcript records (newest first)
    GET /export?since=&limit=             -> incremental watermark feed of every
                                             store (observations/forecasts/alerts/
                                             alert_details/transcripts) since a time
    GET /alerts/active                    -> SAME alerts not yet expired
    GET /alerts/history?from=&to=&event=&limit=&offset=
                                          -> all SAME alerts (active + expired)
    GET /alerts/details?from=&to=         -> structured spoken-alert details
    GET /health                           -> liveness + counts

`from`/`to`/`since` are ISO-8601 (e.g. 2026-06-24T12:00:00Z), inclusive (`since`
is exclusive). Paginated endpoints return {total, count, limit, offset,
next_offset}; page until next_offset is null. `/export` returns {next_since,
more}; re-request with since=next_since until more is false.
`{condition}` accepts friendly names (temperature, humidity, pressure, dewpoint,
wind, sky) or the stored keys (temperature_f, humidity_pct, ...).
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .config import CONFIG, Config
from .db import Database
from .formats import aprs_bulletins, aprs_weather, net_bulletin, sitrep
from .health import Heartbeat, assess
from .trust import mark as mark_trust


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

# friendly condition name -> stored key
_CONDITION_ALIASES = {
    "temperature": "temperature_f", "temp": "temperature_f",
    "dewpoint": "dewpoint_f", "humidity": "humidity_pct",
    "pressure": "pressure_in", "wind_speed": "wind_speed_mph",
}


def _canon(condition: str) -> str:
    c = condition.lower()
    return _CONDITION_ALIASES.get(c, c)


class _Handler(BaseHTTPRequestHandler):
    db: Database = None
    cfg: Config = None
    min_sightings: int = 2
    protocol_version = "HTTP/1.1"

    def _min(self, q: dict) -> int:
        try:
            return max(1, int(q.get("min", self.min_sightings)))
        except (ValueError, TypeError):
            return self.min_sightings

    def _stale_after(self, q: dict) -> int:
        try:
            return max(1, int(q.get("stale_after", self.cfg.condition_stale_after_min)))
        except (ValueError, TypeError):
            return self.cfg.condition_stale_after_min

    def _annotate_age(self, rows: list, q: dict) -> list:
        """Add age_minutes + stale to each reading (by captured_at); ?fresh=1
        drops the stale ones. (Conditions already carry an agreement/confidence
        trust block via trust.mark; forecasts get theirs in _annotate_forecast_age.)"""
        threshold = self._stale_after(q)
        now = datetime.now(timezone.utc)
        out = []
        for r in rows:
            ca = r.get("captured_at")
            if ca:
                age = (now - datetime.strptime(ca, "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=timezone.utc)).total_seconds() / 60
                r["age_minutes"] = round(age, 1)
                r["stale"] = age > threshold
            else:  # pragma: no cover - readings always carry captured_at
                r["age_minutes"] = None
                r["stale"] = None
            if q.get("fresh") in ("1", "true") and r.get("stale"):
                continue
            out.append(r)
        return out

    def _trusted(self, rows: list, q: dict, drop_stale: bool = False) -> list:
        """Trust-mark + age-annotate a reading list. Index/snapshot views keep
        stale rows (?fresh= suppressed); pass drop_stale=True to honor it."""
        return mark_trust(self._annotate_age(rows, q if drop_stale else dict(q, fresh="")))

    def _paginate(self, q: dict, default: int, count_fn, rows_fn) -> tuple[list, dict]:
        """Shared limit/offset parsing -> rows -> {total,count,...} envelope."""
        limit, offset = self._page(q, default=default)
        rows = rows_fn(limit, offset)
        return rows, self._paging(count_fn(), len(rows), limit, offset)

    def _page(self, q: dict, default: int = 100, maximum: int = 1000) -> tuple[int, int]:
        try:
            limit = min(maximum, max(1, int(q.get("limit", default))))
        except (ValueError, TypeError):
            limit = default
        try:
            offset = max(0, int(q.get("offset", 0)))
        except (ValueError, TypeError):
            offset = 0
        return limit, offset

    @staticmethod
    def _paging(total: int, returned: int, limit: int, offset: int) -> dict:
        nxt = offset + limit
        return {"total": total, "count": returned, "limit": limit, "offset": offset,
                "next_offset": nxt if nxt < total else None}

    def _serve_sse(self, since: str | None) -> None:
        """Server-Sent Events live feed (LAN-safe push — consumers connect *in*,
        nothing is sent outbound). Polls the since-readers and emits new alerts,
        observations, and forecasts as they land, ordered by capture time."""
        watermark = since or _now_iso()
        try:  # validate before committing a 200 event-stream (else a bad since
            datetime.strptime(watermark, "%Y-%m-%dT%H:%M:%SZ")  # would error mid-stream)
        except ValueError:
            self._send({"error": "since= must be ISO-8601 (e.g. 2026-06-24T12:00:00Z)"}, 400)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                events = (
                    [("alert", a, a["captured_at"]) for a in self.db.alerts_since(watermark, 50)]
                    + [("observation", o, o["captured_at"])
                       for o in self.db.observations_since(watermark, 200)]
                    + [("forecast", f, f["issued_at"])
                       for f in self.db.forecasts_since(watermark, 200)])
                for name, data, ts in sorted(events, key=lambda e: e[2]):
                    self.wfile.write(
                        f"event: {name}\ndata: {json.dumps(data)}\n\n".encode("utf-8"))
                    if ts > watermark:
                        watermark = ts
                self.wfile.write(b": ping\n\n")  # pragma: no cover - keepalive cadence
                self.wfile.flush()               # pragma: no cover
                time.sleep(self.cfg.stream_poll_s)  # pragma: no cover
        except Exception:
            # client went away, or a mid-stream DB error — close the (already
            # committed) stream; never fall through to the generic handler, which
            # would write a second HTTP response onto the event-stream socket.
            return

    def _annotate_forecast_age(self, forecasts: list, q: dict) -> list:
        """Add age_minutes + stale to each forecast issuance (by issued_at)."""
        threshold = self._stale_after(q)
        now = datetime.now(timezone.utc)
        for fc in forecasts:
            fc["source"] = "stt"; fc["advisory"] = True  # transcribed, not SAME
            ia = fc.get("issued_at")
            if ia:  # pragma: no branch - a stored forecast always has issued_at
                age = (now - datetime.strptime(ia, "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=timezone.utc)).total_seconds() / 60
                fc["age_minutes"] = round(age, 1)
                fc["stale"] = age > threshold
            # per-period: which fields the airings disagreed on (low vote agreement)
            for p in fc.get("periods", []):
                conf = p.get("confidence") or {}
                p["uncertain"] = [f for f, c in conf.items() if c < self.cfg.confidence_min]
        return forecasts

    @staticmethod
    def _authoritative(alert: dict) -> dict:
        """Tag a SAME alert as the authoritative (digital, not transcribed) source."""
        alert["source"] = "same"
        alert["authoritative"] = True
        return alert

    def _link_details(self, alert: dict) -> dict:
        """Attach the spoken-detail transcripts that fall in this alert's window
        (a heads-up may precede the SAME burst; the narrative runs to expiry)."""
        alert = self._authoritative(alert)
        try:
            ca = datetime.strptime(alert["captured_at"], "%Y-%m-%dT%H:%M:%SZ")
            frm = (ca - timedelta(seconds=self.cfg.alert_link_pre_buffer_s)
                   ).strftime("%Y-%m-%dT%H:%M:%SZ")
            to = alert.get("expires_at") or ca.strftime("%Y-%m-%dT%H:%M:%SZ")
            alert = dict(alert)
            alert["spoken"] = self.db.alert_details_between(frm, to)
        except (KeyError, ValueError):
            alert = dict(alert, spoken=[])
        return alert

    def _send(self, payload, status: int = 200) -> None:
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text: str, status: int = 200) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _snapshot(self, q: dict) -> dict:
        """The /now data — current ob + roundup + forecast + active alerts — reused
        by the human/RF format endpoints."""
        city = q.get("city", self.cfg.primary_city)
        conds = self._trusted(self.db.all_conditions_for_city(city, self._min(q)), q)
        roundup = self._trusted(
            [r for r in self.db.latest_for_condition("temperature_f", self._min(q))
             if r["city"].lower() != city.lower()], q)
        forecast = self._annotate_forecast_age(self.db.latest_forecasts(), q)
        alerts = [self._link_details(a) for a in self.db.get_active_alerts()]
        almanac = self._trusted(self.db.latest_almanac(self._min(q)), q)
        return {"generated_at": _now_iso(), "station": self.cfg.station, "city": city,
                "conditions": conds, "roundup": roundup, "almanac": almanac,
                "forecast": forecast, "alerts": alerts}

    # Exact-path routing; the two parametrized shapes (/city/{city} and
    # /conditions/{condition}) are matched in do_GET after this table.
    _ROUTES = {
        "/": "_ep_index", "/stream": "_ep_stream", "/now": "_ep_now",
        "/bulletin": "_ep_bulletin", "/sitrep": "_ep_sitrep", "/aprs": "_ep_aprs",
        "/cities": "_ep_cities", "/conditions": "_ep_conditions",
        "/conditions/history": "_ep_condition_history", "/almanac": "_ep_almanac",
        "/forecast": "_ep_forecast", "/forecast/history": "_ep_forecast_history",
        "/transcripts": "_ep_transcripts", "/export": "_ep_export",
        "/alerts/active": "_ep_alerts_active", "/alerts/history": "_ep_alerts_history",
        "/alerts/details": "_ep_alerts_details", "/health": "_ep_health",
    }

    def do_GET(self) -> None:  # noqa: N802
        u = urlsplit(self.path)
        path = u.path.rstrip("/") or "/"
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        parts = [p for p in path.split("/") if p]
        try:
            route = self._ROUTES.get(path)
            if route:
                getattr(self, route)(q)
            elif parts[0] == "city" and len(parts) == 2:
                self._ep_city(q, parts[1])
            elif parts[0] == "conditions" and len(parts) == 2:
                self._ep_condition(q, parts[1])
            else:
                self._send({"error": "not found", "path": path}, 404)
        except Exception as e:  # pragma: no cover - defensive: never crash on a bad read
            self._send({"error": str(e)}, 500)

    def _ep_index(self, q: dict) -> None:
        self._send({"endpoints": [
            "/now", "/bulletin", "/sitrep", "/aprs",
            "/cities", "/city/{city}",
            "/conditions", "/conditions/{condition}", "/conditions/history",
            "/almanac", "/forecast", "/forecast/history",
            "/transcripts", "/export?since=", "/stream",
            "/alerts/active", "/alerts/history", "/alerts/details", "/health"]})

    def _ep_stream(self, q: dict) -> None:
        self._serve_sse(q.get("since"))

    def _ep_now(self, q: dict) -> None:
        self._send(self._snapshot(q))

    def _ep_bulletin(self, q: dict) -> None:
        self._send_text(net_bulletin(self._snapshot(q)))

    def _ep_sitrep(self, q: dict) -> None:
        self._send_text(sitrep(self._snapshot(q)))

    def _ep_aprs(self, q: dict) -> None:
        snap = self._snapshot(q)
        payload = {"station": snap["station"], "generated_at": snap["generated_at"],
                   "weather_report": aprs_weather(snap),
                   "bulletins": aprs_bulletins(snap)}
        if q.get("format") == "text":
            self._send_text("\n".join([payload["weather_report"], *payload["bulletins"]]) + "\n")
        else:
            self._send(payload)

    def _ep_cities(self, q: dict) -> None:
        self._send({"generated_at": _now_iso(), "cities": self.db.cities(self._min(q))})

    def _ep_city(self, q: dict, city: str) -> None:
        m = self._min(q)
        conds = self._trusted(self.db.all_conditions_for_city(city, m), q, drop_stale=True)
        self._send({"city": city, "min_sightings": m,
                    "stale_after_min": self._stale_after(q), "conditions": conds})

    def _ep_conditions(self, q: dict) -> None:
        conds = self.db.list_conditions(self._min(q))
        for c in conds:  # reuse age annotation against each condition's latest
            c["captured_at"] = c.get("latest")
        conds = self._annotate_age(conds, dict(q, fresh=""))  # never drop on index
        self._send({"min_sightings": self._min(q),
                    "stale_after_min": self._stale_after(q), "conditions": conds})

    def _ep_condition_history(self, q: dict) -> None:
        cond = _canon(q.get("condition", ""))
        if not cond:
            self._send({"error": "condition= query param required"}, 400)
            return
        args = (cond, q.get("city"), q.get("from"), q.get("to"))
        rows, paging = self._paginate(
            q, 1000, lambda: self.db.condition_history_count(*args),
            lambda lim, off: self.db.condition_history(*args, lim, off))
        self._send({"condition": cond, "city": q.get("city"),
                    "from": q.get("from"), "to": q.get("to"), "readings": rows, **paging})

    def _ep_condition(self, q: dict, condition: str) -> None:
        cond = _canon(condition)
        m = self._min(q)
        cities = self._trusted(self.db.latest_for_condition(cond, m), q, drop_stale=True)
        self._send({"condition": cond, "min_sightings": m,
                    "stale_after_min": self._stale_after(q), "cities": cities})

    def _ep_almanac(self, q: dict) -> None:
        rows = self._trusted(self.db.latest_almanac(self._min(q)), q)
        self._send({"generated_at": _now_iso(), "min_sightings": self._min(q),
                    "stale_after_min": self._stale_after(q), "almanac": rows})

    def _ep_forecast(self, q: dict) -> None:
        self._send({"forecasts": self._annotate_forecast_age(self.db.latest_forecasts(), q)})

    def _ep_forecast_history(self, q: dict) -> None:
        args = (q.get("from"), q.get("to"), q.get("city"))
        rows, paging = self._paginate(
            q, 1000, lambda: self.db.forecast_history_count(*args),
            lambda lim, off: self.db.forecast_history(*args, lim, off))
        self._send({"from": q.get("from"), "to": q.get("to"), "city": q.get("city"),
                    "forecasts": rows, **paging})

    def _ep_transcripts(self, q: dict) -> None:
        args = dict(frm=q.get("from"), to=q.get("to"), q=q.get("q"), product=q.get("product"))
        rows, paging = self._paginate(
            q, 100, lambda: self.db.count_raw_reports(**args),
            lambda lim, off: self.db.query_raw_reports(limit=lim, offset=off, **args))
        self._send({"from": q.get("from"), "to": q.get("to"),
                    "q": q.get("q"), "product": q.get("product"),
                    "transcripts": rows, **paging})

    def _ep_export(self, q: dict) -> None:
        since = q.get("since")
        if not since:
            self._send({"error": "since= query param required (ISO-8601)"}, 400)
            return
        limit, _ = self._page(q, default=500, maximum=2000)
        obs = self.db.observations_since(since, limit)
        fcs = self.db.forecasts_since(since, limit)
        als = self.db.alerts_since(since, limit)
        ads = self.db.alert_details_since(since, limit)
        alm = self.db.almanac_since(since, limit)
        trs = self.db.raw_reports_since(since, limit)
        sections = {"observations": obs, "forecasts": fcs, "alerts": als,
                    "alert_details": ads, "almanac": alm, "transcripts": trs}
        stamps = ([r["captured_at"] for r in obs]
                  + [r["issued_at"] for r in fcs]
                  + [r["captured_at"] for r in als]
                  + [r["captured_at"] for r in ads]
                  + [r["captured_at"] for r in alm]
                  + [r.get("captured_at", "") for r in trs])
        stamps = [t for t in stamps if t]
        self._send({"since": since,
                    "next_since": max(stamps) if stamps else since,
                    "limit": limit,
                    "more": any(len(v) >= limit for v in sections.values()),
                    **sections})

    def _ep_alerts_active(self, q: dict) -> None:
        alerts = self.db.get_active_alerts()
        if q.get("details", "1") != "0":
            alerts = [self._link_details(a) for a in alerts]
        self._send({"alerts": alerts})

    def _ep_alerts_history(self, q: dict) -> None:
        args = (q.get("from"), q.get("to"), q.get("event"))
        rows, paging = self._paginate(
            q, 100, lambda: self.db.alerts_history_count(*args),
            lambda lim, off: self.db.alerts_history(*args, lim, off))
        if q.get("details") in ("1", "true"):
            rows = [self._link_details(a) for a in rows]
        else:
            rows = [self._authoritative(a) for a in rows]
        self._send({"from": q.get("from"), "to": q.get("to"),
                    "event": q.get("event"), "alerts": rows, **paging})

    def _ep_alerts_details(self, q: dict) -> None:
        now = datetime.now(timezone.utc)
        to = q.get("to") or now.strftime("%Y-%m-%dT%H:%M:%SZ")
        frm = q.get("from") or (now - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._send({"from": frm, "to": to,
                    "details": self.db.alert_details_between(frm, to)})

    def _ep_health(self, q: dict) -> None:
        health = assess(Heartbeat.read(self.cfg), self.cfg)
        health.update({"generated_at": _now_iso(),
                       "station": self.cfg.station,
                       "conditions": len(self.db.list_conditions()),
                       "cities": len(self.db.cities()),
                       "active_alerts": len(self.db.get_active_alerts()),
                       "total_alerts": self.db.alerts_history_count(None, None, None),
                       "forecast_cities": len(self.db.latest_forecasts()),
                       "almanac_fields": len(self.db.latest_almanac())})
        # fail loud: non-200 so a monitor can alarm on HTTP status alone.
        code = 200 if health["status"] == "ok" else 503
        self._send(health, code)

    def log_message(self, *args) -> None:
        return


def serve(cfg: Config = CONFIG) -> None:  # pragma: no cover - blocking server bootstrap
    _Handler.db = Database(cfg)
    _Handler.cfg = cfg
    _Handler.min_sightings = cfg.api_min_sightings
    server = ThreadingHTTPServer((cfg.api_host, cfg.api_port), _Handler)
    print(
        f"wxparser-api: serving on {cfg.api_host}:{cfg.api_port} "
        f"from {cfg.pg_user}@{cfg.pg_host}:{cfg.pg_port}/{cfg.pg_database}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover
        pass
    finally:
        server.server_close()


def main() -> int:  # pragma: no cover - CLI entry
    serve(CONFIG)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
