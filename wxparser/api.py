"""LAN-only HTTP/JSON query API (PLAN §6).

Serves the structured weather data other services ask for — sourced entirely from
the radio, fully offline. Stdlib http.server only (no FastAPI/uvicorn dependency,
honours §2.1). Read-only: it opens the SQLite store the capture service writes.

    GET /current        -> latest voted current conditions
    GET /forecast       -> latest zone-forecast periods (with valid windows)
    GET /alerts/active  -> SAME alerts not yet expired
    GET /health         -> liveness + counts
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import CONFIG, Config
from .db import Database


def _current_payload(db: Database) -> dict:
    obs = db.get_current()
    if obs is None:
        return {"captured_at": None, "conditions": {}, "detail": {}}
    return {
        "captured_at": obs["captured_at"],
        "station": obs["station"],
        "conditions": obs["conditions"],  # typed values from promoted columns
        "detail": obs["fields"],          # vote counts / provenance (jsonb)
    }


class _Handler(BaseHTTPRequestHandler):
    db: Database = None  # set on the server class
    protocol_version = "HTTP/1.1"

    def _send(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        try:
            if path == "/current":
                self._send(_current_payload(self.db))
            elif path == "/forecast":
                self._send(self.db.get_forecast())
            elif path == "/alerts/active":
                self._send({"alerts": self.db.get_active_alerts()})
            elif path == "/health":
                self._send({
                    "status": "ok",
                    "has_current": self.db.get_current() is not None,
                    "active_alerts": len(self.db.get_active_alerts()),
                    "forecast_periods": len(self.db.get_forecast()["periods"]),
                })
            elif path == "/":
                self._send({"endpoints": ["/current", "/forecast", "/alerts/active", "/health"]})
            else:
                self._send({"error": "not found", "path": path}, status=404)
        except Exception as e:  # never crash the server on a bad read
            self._send({"error": str(e)}, status=500)

    def log_message(self, *args) -> None:  # quiet; journal handles logging
        return


def serve(cfg: Config = CONFIG) -> None:
    _Handler.db = Database(cfg)
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
