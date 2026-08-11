"""Every model module must be reachable from ``Base.metadata``.

A model class only registers on ``Base.metadata`` when its module is imported.
``app/models/__init__.py`` is what performs those imports, so a new module that
nobody adds an import line for is invisible to anything that reasons about the
schema as a whole — ``create_all``, Alembic autogenerate, and the schema-drift
tooling all silently under-report.

This is not hypothetical: ``app/models/system_settings.py`` had been missing from
``__init__.py``, so ``import app.models`` registered 52 tables instead of 53 and
``system_settings`` looked like a table with no model.

Nothing about this needs a database.
"""

from __future__ import annotations

import ast
import importlib
import pkgutil
from pathlib import Path

MODELS_DIR = Path(__file__).resolve().parents[2] / "app" / "models"


def _modules_declaring_a_table() -> dict[str, set[str]]:
    """{module_name: {tablenames}} for every module under app/models.

    Parsed with ast rather than imported, so this can tell "declares a table" from
    "was imported", which is the whole point of the test.
    """
    found: dict[str, set[str]] = {}
    for path in sorted(MODELS_DIR.glob("*.py")):
        if path.name == "__init__.py":
            continue
        tables: set[str] = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if (
                    isinstance(target, ast.Name)
                    and target.id == "__tablename__"
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)
                ):
                    tables.add(node.value.value)
        if tables:
            found[path.stem] = tables
    return found


def test_importing_app_models_registers_every_table():
    """`import app.models` alone must make Base.metadata complete."""
    import app.models  # noqa: F401  registration side effect is the subject
    from app.db.base import Base

    registered = set(Base.metadata.tables)
    expected = _modules_declaring_a_table()

    missing = {
        module: sorted(tables - registered)
        for module, tables in expected.items()
        if tables - registered
    }
    assert not missing, (
        "these model modules declare tables that are NOT on Base.metadata after "
        f"`import app.models`: {missing}. Add the import to app/models/__init__.py "
        "— without it, autogenerate and create_all silently omit the table."
    )


def test_every_model_module_is_imported_by_init():
    """The import line itself must exist, not merely happen via another module.

    Registration through a transitive import is luck: it breaks the moment the
    intermediate module stops importing it. `mixins` is excluded because it
    declares no table.
    """
    init_source = (MODELS_DIR / "__init__.py").read_text(encoding="utf-8")
    imported = {
        node.module.split(".")[-1]
        for node in ast.walk(ast.parse(init_source))
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module
    }

    not_imported = sorted(set(_modules_declaring_a_table()) - imported)
    assert not not_imported, (
        f"model modules declaring a table but not imported in app/models/__init__.py: "
        f"{not_imported}"
    )


def test_all_model_modules_are_importable():
    """A module that raises on import takes the whole registration down with it."""
    import app.models

    failures = {}
    for module in pkgutil.iter_modules(app.models.__path__):
        try:
            importlib.import_module(f"app.models.{module.name}")
        except Exception as exc:  # noqa: BLE001 - report, don't mask
            failures[module.name] = f"{type(exc).__name__}: {exc}"
    assert not failures, f"model modules failed to import: {failures}"
