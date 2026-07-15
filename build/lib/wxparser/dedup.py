"""Text-level dedup — the second-line guard (PLAN §5).

The audio-fingerprint gate (fingerprint.py) catches the bulk of loop repeats, but
boundary shifts between loops can let a near-identical transcript through. This
compares each new transcript against recently-saved reports and:

  * drops it as a duplicate if it's text-identical to a recent report, or
  * marks it an update (sets `supersedes`) if it's a changed version of a recent
    same-type report, or
  * passes it as new.

Uses stdlib difflib (no compiled dep); rapidfuzz could drop in as a faster
backend if ever needed.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass
from difflib import SequenceMatcher

from .config import Config

_NON_CONTENT = re.compile(r"[^a-z0-9%. ]+")
_WS = re.compile(r"\s+")


def normalize(text: str) -> str:
    t = text.lower()
    t = _NON_CONTENT.sub(" ", t)
    return _WS.sub(" ", t).strip()


def similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


@dataclass
class DedupResult:
    kind: str               # "new" | "update" | "duplicate"
    supersedes: str | None  # id of the report this updates (kind == "update")


class TextDeduper:
    """In-memory rolling window of recent reports for fuzzy comparison."""

    def __init__(self, cfg: Config):
        self.dup_threshold = cfg.text_dup_threshold
        self.update_threshold = cfg.text_update_threshold
        self.history: deque[tuple[str, str, str]] = deque(maxlen=cfg.text_history)

    def prime(self, reports: list[dict]) -> None:
        """Seed history from already-saved transcript reports (skip obs/alerts)."""
        for r in reports:
            if "text" not in r:  # observation / same_alert records have no transcript
                continue
            self.history.append((r["id"], r.get("product_type", "unknown"), normalize(r["text"])))

    def consider(self, report: dict) -> DedupResult:
        norm = normalize(report["text"])
        best_id: str | None = None
        best_pt = "unknown"
        best = 0.0
        for rid, pt, rnorm in self.history:
            s = similarity(norm, rnorm)
            if s > best:
                best, best_id, best_pt = s, rid, pt

        if best >= self.dup_threshold:
            return DedupResult("duplicate", best_id)

        if best >= self.update_threshold and best_pt == report.get("product_type"):
            result = DedupResult("update", best_id)
        else:
            result = DedupResult("new", None)
        self.history.append((report["id"], report.get("product_type", "unknown"), norm))
        return result
