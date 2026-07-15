"""Optional outbound push (roadmap: notification).

OFF BY DEFAULT — the system stays fully offline at runtime unless `WX_WEBHOOK_URL`
is set. When configured (e.g. to a LAN mesh gateway), a new SAME alert is POSTed
as JSON. Uses stdlib urllib only (no dependency), fires on a daemon thread so a
slow/absent endpoint never blocks capture, and never raises into the pipeline.
"""

from __future__ import annotations

import json
import sys
import threading
import urllib.request

from .config import Config


def post_webhook(cfg: Config, event: str, payload: dict) -> None:
    url = cfg.webhook_url
    if not url:
        return  # disabled -> remain fully offline
    body = json.dumps({"event": event, "data": payload}).encode("utf-8")

    def _post() -> None:
        try:
            req = urllib.request.Request(
                url, data=body, method="POST",
                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=cfg.webhook_timeout_s).close()
        except Exception as e:  # never let a push failure touch the pipeline
            print(f"  ! webhook POST to {url} failed: {e}", file=sys.stderr, flush=True)

    threading.Thread(target=_post, daemon=True).start()
