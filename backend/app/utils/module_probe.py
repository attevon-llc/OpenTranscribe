"""Answer "is this optional dependency installed?" without importing it."""

from __future__ import annotations

import importlib.util


def module_available(name: str) -> bool:
    """Return True when ``name`` (dotted allowed) can be located, without importing it.

    ``importlib.util.find_spec`` imports the *parent* packages of a dotted name and raises
    ``ModuleNotFoundError`` when one is absent, so that case is folded into ``False`` here.
    """
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False
