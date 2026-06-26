#!/usr/bin/env python3
"""Automatic gain control backstop for the wxparser capture input.

The decoder only transcribes audio that clears the VAD floor (WX_VAD_DBFS, default
-35 dBFS) without clipping. The analog level into the ALC269 Front Mic jack drifts
— radio volume, or a card reset reverting "Front Mic Boost" to its saved value —
and a too-quiet feed silently goes "deaf": no segments form, nothing transcribes.
This keeps the capture gain in the decoder's sweet spot.

It does NOT open the audio device (arecord holds it exclusively). It reads the
per-segment level the pipeline publishes to health.json and adjusts the ALSA mixer
with `amixer`, persisting with `alsactl store` so the fix survives a reboot. Run
periodically from a systemd timer.

Design: one small step per run + a wide dead band => stable, no oscillation.
Decisions use a short window of segments (not one noisy segment). When the feed is
fully deaf (no recent segment => no level to read) it ratchets gain up one step per
run until segments resume, then fine-tunes — and stops at max gain rather than
amplifying a noise floor when there is genuinely no signal (a dead radio/cable,
which is a hardware problem AGC can't fix).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from statistics import median

CARD = os.environ.get("WX_ALSA_CARD", "0")
CAPTURE = "Capture"          # ADC gain, 0-31, fine
BOOST = "Front Mic Boost"    # coarse, 0-3, ~+10 dB/step

# Targets in dBFS, measured on the published per-segment level. The VAD floor is
# -35; we keep speech comfortably above it without letting peaks clip.
PEAK_CLIP = float(os.environ.get("WX_AGC_PEAK_CLIP", "-2.0"))   # peak above -> lower
RMS_LOW = float(os.environ.get("WX_AGC_RMS_LOW", "-30.0"))      # speech below -> raise
RMS_HIGH = float(os.environ.get("WX_AGC_RMS_HIGH", "-14.0"))    # speech above -> lower
SILENT_MIN = float(os.environ.get("WX_AGC_SILENT_MIN", "5.0"))  # no segment -> deaf
STALE_MIN = float(os.environ.get("WX_AGC_STALE_MIN", "5.0"))    # health.json too old

CAP_MIN, CAP_MAX, CAP_STEP, CAP_MID = 0, 31, 3, 18
BOOST_MIN, BOOST_MAX = 0, 3


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}] agc: {msg}", flush=True)


def amixer(*args: str) -> str:
    return subprocess.run(["amixer", "-c", CARD, *args],
                          capture_output=True, text=True).stdout


def get_ctl(ctl: str) -> int:
    m = re.search(r"(\d+) \[\d{1,3}%\]", amixer("sget", ctl))
    return int(m.group(1)) if m else 0


def set_ctl(ctl: str, value: int) -> None:
    subprocess.run(["amixer", "-c", CARD, "sset", ctl, str(value)],
                   capture_output=True, text=True)


# Front Mic Boost is the effective control on this card (~+10 dB/step); Capture
# (~2 dB across its whole 0-31 range) barely moves the level, so it's only a fine
# trim once Boost is railed. Driving Boost first means a hot/quiet feed (e.g. a
# battery swap) converges in a step or two instead of crawling uselessly through
# the Capture range. When Boost moves, Capture is re-centred so it has fine-trim
# headroom both ways at the rails.
def raise_gain(cap: int, boost: int) -> tuple[int, int]:
    if boost < BOOST_MAX:
        return CAP_MID, boost + 1
    if cap + CAP_STEP <= CAP_MAX:
        return cap + CAP_STEP, boost    # Boost maxed: fine-trim up with Capture
    return cap, boost                   # already at max gain


def lower_gain(cap: int, boost: int) -> tuple[int, int]:
    if boost > BOOST_MIN:
        return CAP_MID, boost - 1
    if cap - CAP_STEP >= CAP_MIN:
        return cap - CAP_STEP, boost     # Boost at 0: fine-trim down with Capture
    return cap, boost


def _age_min(ts: str | None) -> float | None:
    if not ts:
        return None
    then = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - then).total_seconds() / 60.0


def read_health(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def collect_levels(path: Path, window_s: float, poll_s: float) -> list[tuple[float, float]]:
    """Sample distinct segment levels over a short window so one quiet/loud segment
    can't swing the gain. Keyed by last_segment_at so repeats aren't double-counted."""
    samples: dict[str, tuple[float, float]] = {}
    end = time.monotonic() + window_s
    while True:
        hb = read_health(path)
        if hb and hb.get("last_segment_dbfs") is not None:
            samples[hb.get("last_segment_at")] = (
                hb["last_segment_dbfs"], hb.get("last_segment_peak_dbfs"))
        if time.monotonic() >= end:
            break
        time.sleep(poll_s)
    return list(samples.values())


def decide(rms_med: float, peak_max: float) -> str | None:
    if peak_max is not None and peak_max > PEAK_CLIP:
        return "lower"   # clipping — back off regardless of rms
    if rms_med < RMS_LOW:
        return "raise"
    if rms_med > RMS_HIGH:
        return "lower"
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="wxparser capture AGC backstop")
    ap.add_argument("--health", default=os.path.join(
        os.environ.get("WX_OUT_DIR", "transcripts"), "health.json"))
    ap.add_argument("--window", type=float, default=45.0, help="sampling window (s)")
    ap.add_argument("--poll", type=float, default=15.0, help="poll interval (s)")
    ap.add_argument("--dry-run", action="store_true", help="log the decision, change nothing")
    args = ap.parse_args()
    path = Path(args.health)

    hb = read_health(path)
    if hb is None:
        log(f"no health.json at {path} — pipeline not running? no change")
        return 0
    if (hb_age := _age_min(hb.get("updated_at"))) is None or hb_age > STALE_MIN:
        log(f"health stale ({hb_age}m) — capture down? no change")
        return 0

    cap, boost = get_ctl(CAPTURE), get_ctl(BOOST)
    seg_age = _age_min(hb.get("last_segment_at"))

    if seg_age is None or seg_age > SILENT_MIN:
        # Deaf: no recent segment, so no level to read. Ratchet up blindly until
        # audio clears the VAD floor and segments (and a level) reappear (Boost
        # first, so a silent feed recovers in a couple of runs).
        new_cap, new_boost = raise_gain(cap, boost)
        if (new_cap, new_boost) == (cap, boost):
            log(f"deaf (last segment {seg_age}m ago) at max gain "
                f"(Capture={cap}, Boost={boost}) — no signal? likely radio/cable")
            return 0
        log(f"deaf (last segment {seg_age}m ago): raising gain "
            f"Capture {cap}->{new_cap}, Boost {boost}->{new_boost}")
    else:
        levels = collect_levels(path, args.window, args.poll)
        if not levels:
            log("no fresh level samples — no change")
            return 0
        rms_med = median(r for r, _ in levels)
        peaks = [p for _, p in levels if p is not None]
        peak_max = max(peaks) if peaks else None
        action = decide(rms_med, peak_max)
        log(f"{len(levels)} segs: rms~{rms_med:.1f} peak~{peak_max} dBFS "
            f"(Capture={cap}, Boost={boost}) -> {action or 'in sweet spot'}")
        if action is None:
            return 0
        new_cap, new_boost = (raise_gain if action == "raise" else lower_gain)(cap, boost)
        if (new_cap, new_boost) == (cap, boost):
            log(f"want to {action} but already at gain limit — no change")
            return 0

    if args.dry_run:
        log(f"dry-run: would set Capture={new_cap}, Boost={new_boost}")
        return 0
    if new_boost != boost:
        set_ctl(BOOST, new_boost)
    if new_cap != cap:
        set_ctl(CAPTURE, new_cap)
    subprocess.run(["alsactl", "store"], capture_output=True, text=True)
    log(f"set Capture={new_cap}, Boost={new_boost} (persisted)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
