"""Outbound webhook push tests (opt-in; off by default)."""

from __future__ import annotations

import json
import time

from wxparser import notify
from wxparser.config import Config


def _cfg(url: str) -> Config:
    return Config(webhook_url=url, webhook_timeout_s=2)


def test_webhook_is_noop_when_url_unset():
    # disabled -> stays fully offline, no thread, no error
    sent = []
    orig = notify.urllib.request.urlopen
    notify.urllib.request.urlopen = lambda *a, **k: sent.append(1)
    try:
        notify.post_webhook(_cfg(""), "alert", {"x": 1})
        time.sleep(0.1)
    finally:
        notify.urllib.request.urlopen = orig
    assert sent == []


def test_webhook_posts_event_envelope_when_url_set():
    captured: dict = {}
    orig = notify.urllib.request.urlopen

    def fake(req, timeout=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["body"] = json.loads(req.data)

        class _R:
            def close(self):
                pass
        return _R()

    notify.urllib.request.urlopen = fake
    try:
        notify.post_webhook(_cfg("http://gateway.local/hook"), "alert", {"id": "a1"})
        for _ in range(100):  # the POST runs on a daemon thread
            if captured:
                break
            time.sleep(0.02)
    finally:
        notify.urllib.request.urlopen = orig
    assert captured["url"] == "http://gateway.local/hook"
    assert captured["method"] == "POST"
    assert captured["body"] == {"event": "alert", "data": {"id": "a1"}}
