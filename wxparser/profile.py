"""Station profile — the region-specific knowledge for one NWR deployment.

Everything that differs between deployments (the station callsign + frequency, the
home city, the whisper vocabulary prompt, and the place-name corrections for the
coverage area) lives in a profile JSON, NOT in code. Porting wxparser to a new part
of the country is then a drop-in profile, not a code edit — the pipeline, the SAME
decoder (national FIPS/event tables), and the extraction patterns are all generic.

Select a profile with the WX_PROFILE env var: a bundled name (loads
wxparser/profiles/<name>.json) or a path to a .json file anywhere. Defaults to the
KJY93 / east-central Indiana profile this project was developed against.

To add a region: copy profiles/kjy93_muncie.json, set station/frequency_mhz/
primary_city, replace stt_prompt with your NWS office's counties/towns, and rebuild
place_corrections for your coverage cities (seed it loosely; the proposer tool +
reprocess fill in the station-voice garbles over time).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

PROFILE_DIR = Path(__file__).resolve().parent / "profiles"
DEFAULT_PROFILE = "kjy93_muncie"

_REQUIRED = ("station", "frequency_mhz", "primary_city", "stt_prompt", "place_corrections")


def profile_path(name: str) -> Path:
    """A bundled profile name maps to profiles/<name>.json; an explicit .json path
    is used as-is.
    """
    p = Path(name)
    if p.suffix == ".json":
        return p
    return PROFILE_DIR / f"{name}.json"


def load(name: str | None = None) -> dict:
    name = name or os.environ.get("WX_PROFILE", DEFAULT_PROFILE)
    data = json.loads(profile_path(name).read_text(encoding="utf-8"))
    missing = [k for k in _REQUIRED if k not in data]
    if missing:
        raise ValueError(f"profile {name!r} is missing required keys: {missing}")
    return data


_cache: dict | None = None


def get_profile() -> dict:
    """The active station profile, loaded once on FIRST USE — importing wxparser
    does no disk I/O and reads no env vars; WX_PROFILE is honored up to the
    moment the first Config() is built (or the first place-name lookup runs),
    and a missing/invalid profile fails there instead of at import time in
    whatever module happened to load first.
    """
    global _cache
    if _cache is None:
        _cache = load()
    return _cache
