"""Shared test fixtures."""

from __future__ import annotations

import pytest

import pg8000.exceptions
import pg8000.native

import wxparser.main as main
from wxparser.config import Config
from wxparser.db import Database


@pytest.fixture(scope="session", autouse=True)
def _ensure_test_db():
    """Create the wxparser_test database if it doesn't exist, so `pytest` is one
    command on a fresh machine (a running Postgres with a CREATEDB-capable role
    is still required — that's the store the suite deliberately tests for real).
    """
    cfg = Config()
    try:
        pg8000.native.Connection(
            user=cfg.pg_user, host=cfg.pg_host, port=cfg.pg_port,
            database="wxparser_test", password=cfg.pg_password or None).close()
    except pg8000.exceptions.DatabaseError:
        # "does not exist" -> create it via the maintenance DB. Any other
        # failure (Postgres down, bad auth) re-raises on the create attempt
        # with the real error, which is the loudest message we can give.
        conn = pg8000.native.Connection(
            user=cfg.pg_user, host=cfg.pg_host, port=cfg.pg_port,
            database="postgres", password=cfg.pg_password or None)
        try:
            conn.run("CREATE DATABASE wxparser_test")
        finally:
            conn.close()


@pytest.fixture(autouse=True)
def _reset_stop_flag():
    """The capture loop's stop signal is a module global; reset it around every
    test so a run_live/worker test can't leak its shutdown state into the next.
    """
    main._STOP.clear()
    yield
    main._STOP.clear()


@pytest.fixture
def make_cfg(tmp_path):
    """Config factory pinned to the test database and this test's tmp out_dir —
    the triad every DB-touching test used to spell out inline.
    """
    def _make(**overrides) -> Config:
        return Config(out_dir=tmp_path, pg_database="wxparser_test", **overrides)
    return _make


@pytest.fixture
def wxdb(make_cfg):
    """A Database on the test store with the structured tables cleared. clear()
    never touches raw_reports — tests that need it empty TRUNCATE it themselves.
    """
    db = Database(make_cfg())
    db.clear()
    return db
