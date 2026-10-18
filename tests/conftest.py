"""Shared test configuration.

Most tests pin down the *rule table's* behaviour, so by default they run with no
eld surface (the bundled snapshot of what ld.eld 24.0 accepts). Tests about the
surface itself opt back in with the `bundled_surface` fixture.
"""
import pytest

from eldconv import surface


@pytest.fixture(autouse=True)
def _no_surface():
    tok = surface._ACTIVE.set(None)
    yield
    surface._ACTIVE.reset(tok)


@pytest.fixture
def bundled_surface():
    s = surface.bundled()
    assert s is not None, "eldconv/data/eld-surface.json missing"
    tok = surface._ACTIVE.set(s)
    yield s
    surface._ACTIVE.reset(tok)
