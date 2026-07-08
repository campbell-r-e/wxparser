"""Shared test fixtures."""

from __future__ import annotations

import pytest

import wxparser.main as main
from wxparser.config import Config
from wxparser.db import Database


@pytest.fixture(autouse=True)
def _reset_stop_flag():
    """The capture loop's stop signal is a module global; reset it around every
    test so a run_live/worker test can't leak its shutdown state into the next."""
    main._STOP.clear()
    yield
    main._STOP.clear()


@pytest.fixture
def make_cfg(tmp_path):
    """Config factory pinned to the test database and this test's tmp out_dir —
    the triad every DB-touching test used to spell out inline."""
    def _make(**overrides) -> Config:
        return Config(out_dir=tmp_path, pg_database="wxparser_test", **overrides)
    return _make


@pytest.fixture
def wxdb(make_cfg):
    """A Database on the test store with the structured tables cleared. clear()
    never touches raw_reports — tests that need it empty TRUNCATE it themselves."""
    db = Database(make_cfg())
    db.clear()
    return db
