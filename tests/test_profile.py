"""profile: the region-specific knowledge loaded from a JSON, not code."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from wxparser import profile
from wxparser.config import Config
from wxparser.data.place_names import place_corrections


def test_default_profile_drives_config_and_places():
    # the default (KJY93) profile must preserve the original hard-coded behaviour
    cfg = Config()
    assert cfg.station == "KJY93" and cfg.primary_city == "Muncie"
    assert abs(cfg.frequency_mhz - 162.425) < 1e-6
    corrections = place_corrections()
    assert "Muncie" in corrections and "Edmondsee" in corrections["Muncie"]
    assert "Indiana" in cfg.whisper_prompt


def test_import_does_no_profile_io():
    # importing wxparser must not read env vars or the profile JSON — the
    # profile loads on FIRST USE (Config() or a place-name lookup), so WX_*
    # overrides set before construction are always honored. Checked in a fresh
    # interpreter because this suite has long since triggered the load.
    code = ("import wxparser.config, wxparser.data.place_names, wxparser.profile as p; "
            "raise SystemExit(0 if p._cache is None else 1)")
    assert subprocess.run([sys.executable, "-c", code]).returncode == 0


def test_load_default_and_by_name():
    assert profile.load()["station"] == "KJY93"            # name=None -> env default
    assert profile.load("kjy93_muncie")["primary_city"] == "Muncie"


def test_load_by_path_swaps_region(tmp_path):
    custom = {"station": "WXL58", "frequency_mhz": 162.55, "primary_city": "Buffalo",
              "stt_prompt": "NOAA Weather Radio for western New York.",
              "place_corrections": {"Buffalo": ["Buffaloh"]}}
    f = tmp_path / "wny.json"
    f.write_text(json.dumps(custom), encoding="utf-8")
    p = profile.load(str(f))
    assert p["station"] == "WXL58" and p["primary_city"] == "Buffalo"
    assert p["place_corrections"]["Buffalo"] == ["Buffaloh"]


def test_missing_required_keys_raise(tmp_path):
    f = tmp_path / "bad.json"
    f.write_text('{"station": "X"}', encoding="utf-8")
    with pytest.raises(ValueError):
        profile.load(str(f))


def test_profile_path_resolution():
    assert profile.profile_path("indiana") == profile.PROFILE_DIR / "indiana.json"
    assert profile.profile_path("/tmp/wny.json").name == "wny.json"   # explicit path used as-is


def test_resolve_slot_without_leadins_returns_none(monkeypatch):
    # a profile that configures no lead-in phrases (roundup_leadins absent) must
    # skip the phrase scan entirely: an unknown entry with no anchoring prev_city
    # can't be recovered, so resolve_slot returns None.
    from wxparser.data import place_names as pn

    pn._ensure_loaded()  # load first, so the lazy init can't overwrite the patch
    monkeypatch.setattr(pn, "ROUNDUP_LEADINS", {})
    assert pn.resolve_slot(None, "an unknown roundup city here", 12) is None


def test_default_profile_carries_station_term_corrections():
    # the callsign correction is station-specific, so it must live in the profile
    # (keeping "port to a new region = drop-in profile"), not in stt_terms code.
    from wxparser.data import stt_terms
    assert profile.load()["term_corrections"]["KJY93"] == ["KJ193"]
    assert "KJY93" not in stt_terms.TERM_CORRECTIONS
