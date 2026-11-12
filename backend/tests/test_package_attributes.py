"""A module imported afresh by one test does not stay behind as the
package's attribute for the next (conftest.align_package_attributes).

Test files import a module afresh (delete it from sys.modules, import it
again) and restore sys.modules afterwards, but not the package attribute
the import set. A later ``import backend.x as x`` or ``patch("backend.x
.name")`` then reached the stale copy, not the module the code under test
uses: test_window_scope's token-count stub missed after
test_prompt_caching and the real count went to the provider (#633).
"""
import sys
from unittest.mock import MagicMock

import backend
import backend.constants as real

from backend.tests.conftest import align_package_attributes


def _fresh_copy(monkeypatch):
    """What those test files do: a fresh import, sys.modules restored."""
    monkeypatch.setattr(backend, "constants", real)  # undone at the end
    with monkeypatch.context() as m:
        m.delitem(sys.modules, "backend.constants")
        import backend.constants as fresh
    assert sys.modules["backend.constants"] is real
    return fresh


def test_a_stale_fresh_copy_gives_way_to_the_module_in_sys_modules(
        monkeypatch):
    fresh = _fresh_copy(monkeypatch)
    assert fresh is not real
    assert backend.constants is fresh  # the drift
    align_package_attributes()
    import backend.constants as again
    assert again is real
    assert backend.constants is real


def test_a_stand_in_in_sys_modules_does_not_replace_the_module(
        monkeypatch):
    monkeypatch.setattr(backend, "constants", real)
    monkeypatch.setitem(sys.modules, "backend.constants", MagicMock())
    align_package_attributes()
    assert backend.constants is real


def test_a_stand_in_set_as_the_attribute_stays(monkeypatch):
    stand_in = MagicMock()
    monkeypatch.setattr(backend, "constants", stand_in)
    align_package_attributes()
    assert backend.constants is stand_in


def test_a_drift_left_by_an_earlier_test(monkeypatch):
    """Part one of two: leaves the drift behind (no restore of the
    attribute), as the test files above do."""
    with monkeypatch.context() as m:
        m.delitem(sys.modules, "backend.constants")
        import backend.constants  # noqa: F401
    assert backend.constants is not sys.modules["backend.constants"]


def test_is_gone_when_the_next_test_starts():
    """Part two: conftest aligned the attribute before this test."""
    assert backend.constants is sys.modules["backend.constants"]
