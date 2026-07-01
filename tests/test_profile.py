"""profile: the region-specific knowledge loaded from a JSON, not code."""

from __future__ import annotations

import json

import pytest

from wxparser import profile
from wxparser.config import CONFIG
from wxparser.data.place_names import PLACE_CORRECTIONS


def test_default_profile_drives_config_and_places():
    # the default (KJY93) profile must preserve the original hard-coded behaviour
    assert CONFIG.station == "KJY93" and CONFIG.primary_city == "Muncie"
    assert abs(CONFIG.frequency_mhz - 162.425) < 1e-6
    assert "Muncie" in PLACE_CORRECTIONS and "Edmondsee" in PLACE_CORRECTIONS["Muncie"]
    assert "Indiana" in CONFIG.whisper_prompt


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

    monkeypatch.setattr(pn, "ROUNDUP_LEADINS", {})
    assert pn.resolve_slot(None, "an unknown roundup city here", 12) is None
