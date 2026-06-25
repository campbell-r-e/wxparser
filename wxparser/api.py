"""LAN-only HTTP/JSON query API (PLAN §6) — generic, condition-centric.

Serves structured weather data sourced entirely from the radio, fully offline.
Stdlib http.server only (no FastAPI/uvicorn dependency, §2.1). Read-only.

    GET /conditions                       -> available conditions (index)
    GET /conditions/{condition}           -> every city's latest value for it
    GET /conditions/history?condition=&city=&from=&to=&limit=
                                          -> historical readings between times
    GET /forecast                         -> latest forecast for all heard cities
    GET /forecast/history?from=&to=&city= -> historical forecast predictions
    GET /transcripts?from=&to=&q=&product=&limit=
                                          -> raw transcript records (newest first)
    GET /alerts/active                    -> SAME alerts not yet expired
    GET /health                           -> liveness + counts

`from`/`to` are ISO-8601 (e.g. 2026-06-24T12:00:00Z), inclusive.
`{condition}` accepts friendly names (temperature, humidity, pressure, dewpoint,
wind, sky) or the stored keys (temperature_f, humidity_pct, ...).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .config import CONFIG, Config
from .db import Database
from .store import query_reports

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
        except ValueError:
            return self.min_sightings

    def _stale_after(self, q: dict) -> int:
        try:
            return max(1, int(q.get("stale_after", self.cfg.condition_stale_after_min)))
        except (ValueError, TypeError):
            return self.cfg.condition_stale_after_min

    def _annotate_age(self, rows: list, q: dict) -> list:
        """Add age_minutes + stale to each reading (by captured_at); ?fresh=1
        drops the stale ones."""
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
            else:
                r["age_minutes"] = None
                r["stale"] = None
            if q.get("fresh") in ("1", "true") and r.get("stale"):
                continue
            out.append(r)
        return out

    def _link_details(self, alert: dict) -> dict:
        """Attach the spoken-detail transcripts that fall in this alert's window
        (a heads-up may precede the SAME burst; the narrative runs to expiry)."""
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

    def do_GET(self) -> None:  # noqa: N802
        u = urlsplit(self.path)
        path = u.path.rstrip("/") or "/"
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        parts = [p for p in path.split("/") if p]
        try:
            if path == "/":
                self._send({"endpoints": [
                    "/conditions", "/conditions/{condition}", "/conditions/history",
                    "/forecast", "/forecast/history", "/transcripts",
                    "/alerts/active", "/alerts/details", "/health"]})
            elif path == "/conditions":
                conds = self.db.list_conditions(self._min(q))
                for c in conds:  # reuse age annotation against each condition's latest
                    c["captured_at"] = c.get("latest")
                conds = self._annotate_age(conds, dict(q, fresh=""))  # never drop on index
                self._send({"min_sightings": self._min(q),
                            "stale_after_min": self._stale_after(q), "conditions": conds})
            elif parts[:2] == ["conditions", "history"]:
                cond = _canon(q.get("condition", ""))
                if not cond:
                    self._send({"error": "condition= query param required"}, 400)
                    return
                self._send({"condition": cond, "city": q.get("city"),
                            "from": q.get("from"), "to": q.get("to"),
                            "readings": self.db.condition_history(
                                cond, q.get("city"), q.get("from"), q.get("to"),
                                int(q.get("limit", 1000)))})
            elif parts[0] == "conditions" and len(parts) == 2:
                cond = _canon(parts[1])
                m = self._min(q)
                cities = self._annotate_age(self.db.latest_for_condition(cond, m), q)
                self._send({"condition": cond, "min_sightings": m,
                            "stale_after_min": self._stale_after(q), "cities": cities})
            elif path == "/forecast":
                self._send({"forecasts": self.db.latest_forecasts()})
            elif parts[:2] == ["forecast", "history"]:
                self._send({"from": q.get("from"), "to": q.get("to"), "city": q.get("city"),
                            "forecasts": self.db.forecast_history(
                                q.get("from"), q.get("to"), q.get("city"))})
            elif path == "/transcripts":
                try:
                    limit = min(1000, max(1, int(q.get("limit", 100))))
                except ValueError:
                    limit = 100
                reports = query_reports(
                    self.cfg, limit=limit, frm=q.get("from"), to=q.get("to"),
                    q=q.get("q"), product=q.get("product"))
                self._send({"from": q.get("from"), "to": q.get("to"),
                            "q": q.get("q"), "product": q.get("product"),
                            "count": len(reports), "transcripts": reports})
            elif path == "/alerts/active":
                alerts = self.db.get_active_alerts()
                if q.get("details", "1") != "0":
                    alerts = [self._link_details(a) for a in alerts]
                self._send({"alerts": alerts})
            elif parts[:2] == ["alerts", "details"]:
                now = datetime.now(timezone.utc)
                to = q.get("to") or now.strftime("%Y-%m-%dT%H:%M:%SZ")
                frm = q.get("from") or (now - timedelta(hours=24)).strftime(
                    "%Y-%m-%dT%H:%M:%SZ")
                self._send({"from": frm, "to": to,
                            "details": self.db.alert_details_between(frm, to)})
            elif path == "/health":
                self._send({"status": "ok",
                            "conditions": len(self.db.list_conditions()),
                            "active_alerts": len(self.db.get_active_alerts()),
                            "forecast_cities": len(self.db.latest_forecasts())})
            else:
                self._send({"error": "not found", "path": path}, 404)
        except Exception as e:  # never crash on a bad read
            self._send({"error": str(e)}, 500)

    def log_message(self, *args) -> None:
        return


def serve(cfg: Config = CONFIG) -> None:
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


def main() -> int:
    serve(CONFIG)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
