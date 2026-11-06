"""Release-rehearsal scripts that run inside the backend container must import real names.

``scripts/release-tests`` pipes Python (and shell heredocs) into the backend image.
pytest never imports them, so a module split that moves a function leaves them
failing only during the release rehearsal. This resolves every
``from app.X import Y`` statically (AST, no import side effects).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
APP_ROOT = REPO_ROOT / "backend"
PROBE_ROOT = REPO_ROOT / "scripts" / "release-tests"
_SH_IMPORT = re.compile(r"^\s*from\s+(app\.[\w.]+)\s+import\s+([\w, ]+?)\s*(?:#.*)?$")


def _module_path(module: str) -> Path | None:
    base = APP_ROOT.joinpath(*module.split("."))
    if base.with_suffix(".py").is_file():
        return base.with_suffix(".py")
    if (base / "__init__.py").is_file():
        return base / "__init__.py"
    return None


def _defined_names(tree: ast.Module) -> tuple[set[str], bool]:
    names: set[str] = set()
    dynamic = False
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
            dynamic = dynamic or node.name == "__getattr__"
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update((a.asname or a.name).split(".")[0] for a in node.names)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names.update(t.id for t in targets if isinstance(t, ast.Name))
    return names, dynamic


def _problems(module: str, wanted: list[str]) -> list[str]:
    path = _module_path(module)
    if path is None:
        return [f"module {module} does not exist"]
    names, dynamic = _defined_names(ast.parse(path.read_text()))
    if dynamic:
        return []
    out = []
    for name in wanted:
        if name in names:
            continue
        if path.name == "__init__.py" and _module_path(f"{module}.{name}"):
            continue
        out.append(f"{module} has no {name}")
    return out


def _collect() -> list[tuple[str, str, list[str]]]:
    found: list[tuple[str, str, list[str]]] = []
    for path in sorted(PROBE_ROOT.rglob("*")):
        rel = str(path.relative_to(REPO_ROOT))
        if path.suffix == ".py":
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("app."):
                    found.append((rel, node.module or "", [a.name for a in node.names]))
        elif path.suffix == ".sh":
            for line in path.read_text().splitlines():
                m = _SH_IMPORT.match(line)
                if m:
                    found.append((rel, m.group(1), [n.strip() for n in m.group(2).split(",")]))
    return found


def test_probe_scripts_import_names_that_exist() -> None:
    imports = _collect()
    assert imports, "found no app imports under scripts/release-tests; collector is broken"
    problems = [f"{rel}: {p}" for rel, mod, names in imports for p in _problems(mod, names)]
    assert not problems, "\n".join(problems)


def test_resolver_flags_a_missing_name() -> None:
    assert _problems("app.core.embedding_models", ["no_such_function_xyz"])
    assert not _problems("app.core.embedding_models", ["get_speaker_index_v3"])
