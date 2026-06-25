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
                    "/alerts/active", "/health"]})
            elif path == "/conditions":
                self._send({"min_sightings": self._min(q),
                            "conditions": self.db.list_conditions(self._min(q))})
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
                self._send({"condition": cond, "min_sightings": m,
                            "cities": self.db.latest_for_condition(cond, m)})
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
                self._send({"alerts": self.db.get_active_alerts()})
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
