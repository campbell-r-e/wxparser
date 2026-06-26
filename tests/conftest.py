"""Shared test fixtures."""

from __future__ import annotations

import pytest

import wxparser.main as main


@pytest.fixture(autouse=True)
def _reset_stop_flag():
    """The capture loop's stop signal is a module global; reset it around every
    test so a run_live/worker test can't leak its shutdown state into the next."""
    main._STOP.clear()
    yield
    main._STOP.clear()
