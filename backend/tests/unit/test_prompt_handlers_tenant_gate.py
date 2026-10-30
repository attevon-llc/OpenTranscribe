"""Every prompt route that can reach another user's row consults a tenant gate.

The prompt routes are split across ``prompts.py``, ``prompts_active.py`` and
``prompts_sharing.py`` while the gates (``_in_tenant``, ``_shared_in_tenant``, ``_usable_by``,
``_is_usable``) live in ``prompts_access.py``. A handler that stops calling its gate would
still import cleanly and pass any test that only uses the caller's own prompts, so this
reads each handler's body and demands the gate by name. Owner-only routes are listed with
the reason they need none; a new route in any of the modules must be added to one table.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ENDPOINTS = Path(__file__).resolve().parents[2] / "app" / "api" / "endpoints"
MODULES = ("prompts", "prompts_active", "prompts_sharing")

# handler -> the gate names, any one of which must appear in its body
GATED = {
    ("prompts", "get_prompts"): {"_shared_in_tenant"},
    ("prompts", "get_prompts_by_content_type"): {"_shared_in_tenant"},
    ("prompts", "get_prompt"): {"_is_usable"},
    ("prompts", "clone_prompt"): {"_is_usable"},
    ("prompts_active", "get_active_prompt"): {"_usable_by"},
    ("prompts_active", "set_active_prompt"): {"_is_usable"},
    ("prompts_sharing", "get_shared_prompt_library"): {"_shared_in_tenant"},
    ("prompts_sharing", "share_prompt"): {"_in_tenant"},
}
# Owner-only: the row is filtered by the caller's own user_id, never another user's.
OWNER_ONLY = {
    ("prompts", "create_prompt"),
    ("prompts", "update_prompt"),
    ("prompts", "delete_prompt"),
}


def _handlers() -> dict[tuple[str, str], ast.FunctionDef]:
    found: dict[tuple[str, str], ast.FunctionDef] = {}
    for module in MODULES:
        tree = ast.parse((ENDPOINTS / f"{module}.py").read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and any(
                isinstance(d, ast.Call)
                and isinstance(d.func, ast.Attribute)
                and isinstance(d.func.value, ast.Name)
                and d.func.value.id == "router"
                for d in node.decorator_list
            ):
                found[(module, node.name)] = node
    return found


def test_every_prompt_route_is_classified() -> None:
    assert set(_handlers()) == set(GATED) | OWNER_ONLY


@pytest.mark.parametrize(("key", "gates"), sorted(GATED.items()))
def test_handler_calls_its_tenant_gate(key: tuple[str, str], gates: set[str]) -> None:
    names = {n.id for n in ast.walk(_handlers()[key]) if isinstance(n, ast.Name)}
    assert names & gates, f"{key[0]}.{key[1]} no longer consults {sorted(gates)}"
