"""module_available answers import-availability without importing."""

import sys

import pytest

from app.utils.module_probe import module_available


def test_installed_module_is_available():
    assert module_available("json")
    assert module_available("os.path")


def test_missing_top_level_module_is_unavailable():
    assert not module_available("definitely_not_a_real_module_xyz")


def test_missing_parent_of_dotted_name_is_unavailable_not_an_error():
    assert not module_available("definitely_not_a_real_module_xyz.sub")


def test_module_blocked_in_sys_modules_is_unavailable(monkeypatch):
    monkeypatch.setitem(sys.modules, "json", None)
    assert not module_available("json")


@pytest.mark.parametrize("name", ["json", "definitely_not_a_real_module_xyz"])
def test_probe_does_not_import(name, monkeypatch):
    monkeypatch.delitem(sys.modules, name, raising=False)
    module_available(name)
    assert name not in sys.modules
