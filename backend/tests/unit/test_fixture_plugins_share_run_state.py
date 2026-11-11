"""A fixture plugin's per-run values are the same in every copy of the module.

The root conftest registers ``tests/fixtures/*`` as pytest plugins named ``fixtures.<module>``,
and tests also import them as ``tests.fixtures.<module>``: one file, two module objects. A
random value drawn at a fixture module's top level is drawn twice. ``support_world.PASSWORD``
was ``uuid4().hex``: the plugin's ``support_world`` fixture hashed one value,
``test_compliance_audit_rows`` re-authenticated with the other, and its org-erasure cases were
refused with ``reauth_required`` on every run.

So a registered fixture module never draws a random or time-based value at its top level; it
reads ``tests.run_identity.RUN_TOKEN``. That module is imported as ``tests.run_identity`` and
under no other name, so every copy of every fixture module sees the same token.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

TESTS = Path(__file__).resolve().parents[1]
RUN_IDENTITY = "tests.run_identity"
NONDETERMINISTIC_CALLS = frozenset(
    {"uuid1", "uuid4", "token_hex", "token_urlsafe", "token_bytes", "random", "randint", "now"}
)


def _registered_plugins(conftest: Path) -> set[str]:
    for node in ast.parse(conftest.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "pytest_plugins" for t in node.targets
        ):
            assert isinstance(node.value, ast.List | ast.Tuple)
            return {
                e.value
                for e in node.value.elts
                if isinstance(e, ast.Constant) and isinstance(e.value, str)
            }
    return set()


def _draws_at_top_level(module: Path) -> list[str]:
    drawn: list[str] = []
    for node in ast.parse(module.read_text(encoding="utf-8")).body:
        if not isinstance(node, ast.Assign | ast.AnnAssign) or node.value is None:
            continue
        for call in ast.walk(node.value):
            if not isinstance(call, ast.Call):
                continue
            func = call.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in NONDETERMINISTIC_CALLS:
                drawn.append(f"{module.name}:{node.lineno}: {ast.unparse(node)[:80]}")
    return drawn


def per_import_state(tests_root: Path) -> list[str]:
    """Top-level random/time draws in modules registered as ``fixtures.*`` plugins."""
    found: list[str] = []
    for plugin in sorted(_registered_plugins(tests_root / "conftest.py")):
        module = (tests_root / Path(*plugin.split("."))).with_suffix(".py")
        if plugin.startswith("fixtures.") and module.is_file():
            found += _draws_at_top_level(module)
    return found


def _imported_names(tree: ast.Module) -> Iterator[tuple[int, str]]:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            yield node.lineno, node.module
            yield from ((node.lineno, f"{node.module}.{a.name}") for a in node.names)
        elif isinstance(node, ast.Import):
            yield from ((node.lineno, a.name) for a in node.names)


def run_identity_under_another_name(tests_root: Path) -> list[str]:
    """Imports of the run-identity module by any name but ``tests.run_identity``."""
    leaf = RUN_IDENTITY.rsplit(".", 1)[1]
    found: list[str] = []
    for path in sorted(tests_root.rglob("*.py")):
        for line, name in _imported_names(ast.parse(path.read_text(encoding="utf-8"))):
            if name.split(".")[-1] == leaf and not name.startswith(RUN_IDENTITY):
                found.append(f"{path.relative_to(tests_root)}:{line}: {name}")
    return found


def _world(tmp_path: Path, plugin_body: str, user: str = "") -> Path:
    (tmp_path / "fixtures").mkdir()
    (tmp_path / "conftest.py").write_text('pytest_plugins = ["fixtures.world"]\n')
    (tmp_path / "fixtures" / "world.py").write_text(plugin_body)
    (tmp_path / "test_user.py").write_text(user)
    return tmp_path


def test_flags_a_random_constant_in_a_fixture_plugin(tmp_path):
    root = _world(tmp_path, "import uuid\nPASSWORD = uuid.uuid4().hex\n")
    assert per_import_state(root) == ["world.py:2: PASSWORD = uuid.uuid4().hex"]


def test_a_token_from_run_identity_is_clean(tmp_path):
    root = _world(tmp_path, "from tests.run_identity import RUN_TOKEN\nPASSWORD = RUN_TOKEN\n")
    assert per_import_state(root) == []


def test_a_random_value_drawn_inside_a_function_is_clean(tmp_path):
    root = _world(tmp_path, "import uuid\ndef make():\n    return uuid.uuid4().hex\n")
    assert per_import_state(root) == []


def test_flags_run_identity_imported_by_its_short_name(tmp_path):
    root = _world(tmp_path, "", "from run_identity import RUN_TOKEN\nimport tests.run_identity\n")
    assert run_identity_under_another_name(root) == ["test_user.py:1: run_identity"]


def test_no_fixture_plugin_draws_per_import_state():
    found = per_import_state(TESTS)
    assert found == [], (
        "a fixture plugin is loaded twice (as fixtures.<module> and tests.fixtures.<module>), "
        "so these values differ between the copies; read tests.run_identity.RUN_TOKEN:\n"
        + "\n".join(found)
    )


def test_run_identity_is_imported_under_one_name():
    found = run_identity_under_another_name(TESTS)
    assert found == [], f"import it as {RUN_IDENTITY}, or its token differs:\n" + "\n".join(found)
