"""The pre-commit mypy hook must see the same typed world as ``backend/venv`` (issue #1271).

``pre-commit/mirrors-mypy`` runs in an isolated virtualenv holding only the hook's
``additional_dependencies``. A package that ships ``py.typed`` but is missing from that list
resolves to ``Any`` there, so the hook reports *fewer* errors than ``backend/venv/bin/mypy``
(measured at the start of #1271: 0 from the hook against 55 from the venv over the same 2,269
files), and a stub package present on only one side makes the two disagree in either direction.

This module pins the contract instead of a list of names, and derives everything it can:

* the hook's mypy ``rev`` equals the ``mypy==`` pin in ``backend/requirements-dev.txt``;
* every hook dependency is an exact ``name==version`` pin, equal to every pin the
  ``backend/requirements*.txt`` files carry for that package (so one is never bumped without
  the other), and has at least one such pin to be compared against;
* every ``types-*`` stub the hook installs is installed by ``requirements-dev.txt`` too, and
  vice versa, at the same version;
* every third-party module imported anywhere under ``backend/`` whose installed distribution
  ships ``py.typed`` is a hook dependency, or is in ``EXEMPT`` below with a written reason;
* no hook stub package shadows a hook dependency that carries its own inline types (PEP 561
  makes the stub win, which is how ``types-redis`` 4.6 hid ``redis`` 8's real signatures).

``EXEMPT`` is the named gap: those modules are checked by ``backend/venv`` mypy only. It is
short and every entry says why. A module that is not installed where this test runs cannot be
proven untyped, so it fails closed instead of passing silently, except under ``CI`` (see
``_PARTIAL_INSTALL``), where the narrower ``requirements-ci.txt`` install is expected.

Failures raise ``AssertionError`` directly rather than calling ``pytest.fail``, the same
convention as ``test_precommit_stage_ci_parity.py``: ``pytest`` is itself a hook dependency now,
but the checkers below are plain functions the self-test feeds in-memory configs.
"""

from __future__ import annotations

import ast
import functools
import importlib.metadata as md
import importlib.util
import os
import re
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND = REPO_ROOT / "backend"
HOOK_CONFIG = REPO_ROOT / ".pre-commit-config.yaml"
REQUIREMENTS_DEV = BACKEND / "requirements-dev.txt"

#: Import name -> reason it is NOT installed into the hook's isolated environment. These stay
#: ``Any`` in the hook and fully typed in ``backend/venv``; ``backend/venv`` mypy is the only
#: thing that checks code through them.
EXEMPT: dict[str, str] = {
    "torch": "GB-scale CUDA wheel; would dominate every contributor's pre-commit cache",
    "transformers": "pulls torch; same cost as torch",
    "sentence_transformers": "pulls torch and transformers; same cost",
    "presidio_analyzer": "pulls spaCy and its model stack",
    "ir_measures": "eval-only extra (requirements-eval.txt, unpinned there); pulls pytrec_eval",
    "pytrec_eval": "eval-only extra; compiles C code at install time",
    "pyannote": "pyannote-audio ships py.typed but pulls torch, lightning and speechbrain",
    "ragas": "eval-judge extra (requirements-eval-judge.txt), absent from the dev venv; pulls langchain",
    "google.cloud": "optional cloud-ASR SDK, installed in neither env (mypy: import-not-found in both)",
    "google.api_core": "same SDK family as google.cloud; installed in neither env",
    "azure.cognitiveservices": "optional cloud-ASR SDK, installed in neither env",
}

#: Namespace packages: ``google`` and ``azure`` are shared by many unrelated distributions, so
#: the top-level name says nothing about which one an import needs. Keyed by two components.
_NAMESPACE_TOPLEVELS = {"google", "azure"}

#: GitHub Actions' ``backend-tests`` job installs ``requirements-ci.txt`` (CPU-only, no eval,
#: e2e or Playwright extras), so a module imported only by those suites is genuinely absent
#: there and cannot be shown untyped. Locally the venv is the full install, where an absent
#: module is a real gap and fails closed. ``CI`` is set by GitHub Actions.
_PARTIAL_INSTALL = bool(os.environ.get("CI"))

_SKIPPED_DIRS = {"venv", ".venv", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache"}


def _is_skipped_dir(directory: Path) -> bool:
    """A directory that holds no first-party code: a named cache or ANY virtualenv.

    A virtualenv is recognised by its ``pyvenv.cfg``, not by its name, so ``venv-eval`` (or
    whatever a developer calls the next one) is skipped as well. Its site-packages would
    otherwise be read as first-party imports and fail the "typed import" check on code this
    repository does not own.
    """
    return directory.name in _SKIPPED_DIRS or (directory / "pyvenv.cfg").exists()


def _backend_python_files() -> list[Path]:
    """Every ``*.py`` under ``backend/`` outside skipped directories, without descending into them."""
    found: list[Path] = []
    for root, dirs, files in os.walk(BACKEND):
        dirs[:] = [d for d in dirs if not _is_skipped_dir(Path(root) / d)]
        found.extend(Path(root) / f for f in files if f.endswith(".py"))
    return found


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _split_requirement(spec: str) -> tuple[str, str | None]:
    """``'pydantic==2.13.5'`` -> ``('pydantic', '2.13.5')``; anything not ``==`` -> version None."""
    match = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]*\])?(.*)$", spec.strip())
    if not match:
        raise AssertionError(f"unparseable requirement: {spec!r}")
    exact = re.match(r"^\s*==\s*([^\s;,]+)\s*(;.*)?$", match.group(3))
    return _normalize(match.group(1)), exact.group(1) if exact else None


def _requirement_pins(text: str) -> dict[str, str]:
    """Exact pins in one requirements file. Comments, ``-r`` lines and non-``==`` specs are skipped."""
    pins: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        name, version = _split_requirement(line)
        if version is not None:
            pins[name] = version
    return pins


def _all_requirement_pins() -> dict[str, dict[str, str]]:
    """``normalized name -> {requirements file name: pinned version}`` across the backend files."""
    out: dict[str, dict[str, str]] = {}
    for path in sorted(BACKEND.glob("requirements*.txt")):
        for name, version in _requirement_pins(path.read_text()).items():
            out.setdefault(name, {})[path.name] = version
    return out


def _mypy_hook(config: dict[str, Any]) -> tuple[str, list[str]]:
    """``(rev, additional_dependencies)`` of the mirrors-mypy ``mypy`` hook."""
    for repo in config["repos"]:
        if "mirrors-mypy" in repo["repo"]:
            for hook in repo["hooks"]:
                if hook["id"] == "mypy":
                    return repo["rev"], list(hook.get("additional_dependencies", []))
    raise AssertionError("no mirrors-mypy `mypy` hook in .pre-commit-config.yaml")


def _load_hook() -> tuple[str, list[str]]:
    return _mypy_hook(yaml.safe_load(HOOK_CONFIG.read_text()))


# --- checkers: each returns a list of human-readable problems, [] when clean -------------


def check_rev(rev: str, requirements_dev: dict[str, str]) -> list[str]:
    pinned = requirements_dev.get("mypy")
    if pinned is None:
        return ["backend/requirements-dev.txt has no exact `mypy==` pin to compare the hook with"]
    if rev != f"v{pinned}":
        return [f"hook rev {rev} != backend venv mypy v{pinned}"]
    return []


def check_pins(deps: list[str], all_pins: dict[str, dict[str, str]]) -> list[str]:
    problems = []
    for dep in deps:
        name, version = _split_requirement(dep)
        if version is None:
            problems.append(f"{dep!r} is not an exact `name==version` pin")
            continue
        sources = all_pins.get(name)
        if not sources:
            problems.append(
                f"{dep!r} has no pin in backend/requirements*.txt to be compared against; "
                "pin it there too (requirements-dev.txt if only tests/tooling import it)"
            )
            continue
        for filename, pinned in sorted(sources.items()):
            if pinned != version:
                problems.append(f"{dep!r} != {filename} pin {name}=={pinned}")
    return problems


def check_stub_sets(deps: list[str], requirements_dev: dict[str, str]) -> list[str]:
    hook = {_split_requirement(d)[0] for d in deps if _split_requirement(d)[0].startswith("types-")}
    dev = {n for n in requirements_dev if n.startswith("types-")}
    problems = [f"stub {n} is in the hook but not requirements-dev.txt" for n in sorted(hook - dev)]
    problems += [
        f"stub {n} is in requirements-dev.txt but not the hook" for n in sorted(dev - hook)
    ]
    return problems


def _ships_py_typed(dist_name: str) -> bool | None:
    """True/False for an installed distribution, None when it is not installed here."""
    try:
        dist = md.distribution(dist_name)
    except md.PackageNotFoundError:
        return None
    return any(f.name == "py.typed" for f in (dist.files or []))


def check_no_shadowing_stub(deps: list[str]) -> list[str]:
    names = {_split_requirement(d)[0] for d in deps}
    problems = []
    for name in sorted(n for n in names if n.startswith("types-")):
        real = name.removeprefix("types-")
        if real in names and _ships_py_typed(real):
            problems.append(
                f"{name} shadows {real}, which ships its own inline types; PEP 561 makes the "
                f"stub win, so mypy would ignore {real}'s real signatures"
            )
    return problems


@functools.cache
def _first_party_names() -> frozenset[str]:
    """Top-level importable names from every directory ``backend`` code is imported from.

    ``backend/``, every directory under ``backend/tests`` (the root conftest and pytest's
    rootdir handling put them on ``sys.path``) and the two ``scripts/`` directories tests
    import helpers from.
    """
    roots = [BACKEND, BACKEND / "scripts", REPO_ROOT / "scripts"] + [
        p for p in (BACKEND / "tests").rglob("*") if p.is_dir() and not set(p.parts) & _SKIPPED_DIRS
    ]
    names: set[str] = {"tests"}
    for root in [BACKEND / "tests", *roots]:
        for entry in root.iterdir():
            if entry.name in _SKIPPED_DIRS or (entry.is_dir() and _is_skipped_dir(entry)):
                continue
            if entry.is_dir() and not entry.name.startswith("."):
                names.add(entry.name)
            elif entry.suffix == ".py":
                names.add(entry.stem)
    return frozenset(names)


def _module_key(dotted: str) -> str:
    parts = dotted.split(".")
    if parts[0] in _NAMESPACE_TOPLEVELS and len(parts) > 1:
        return ".".join(parts[:2])
    return parts[0]


@functools.cache
def _imported_modules() -> frozenset[str]:
    modules: set[str] = set()
    for path in _backend_python_files():
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(_module_key(a.name) for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules.add(_module_key(node.module))
    return frozenset(m for m in modules if m.split(".")[0] not in sys.stdlib_module_names)


def _module_dists() -> dict[str, list[str]]:
    """Import key -> installed distributions providing it ([] when it is not installed)."""
    by_top = md.packages_distributions()
    out: dict[str, list[str]] = {}
    for key in _imported_modules():
        top = key.split(".")[0]
        if key != top and importlib.util.find_spec(key) is None:
            out[key] = []
        else:
            out[key] = list(by_top.get(top, []))
    return out


def check_typed_imports(
    modules: set[str],
    first_party: set[str],
    hook_deps: set[str],
    exempt: dict[str, str],
    module_to_dist: dict[str, list[str]],
    *,
    allow_uninstalled: bool = False,
) -> list[str]:
    problems = []
    for module in sorted(modules - first_party):
        if module in exempt:
            continue
        dists = module_to_dist.get(module)
        if not dists:
            if allow_uninstalled or _normalize(module) in hook_deps:
                continue
            problems.append(
                f"{module}: not installed here, so it cannot be shown untyped. Install "
                "backend/requirements-ci.txt, add it to the hook, or add it to EXEMPT with a reason"
            )
            continue
        if any(_normalize(d) in hook_deps for d in dists):
            continue
        if any(_ships_py_typed(d) for d in dists):
            problems.append(
                f"{module} ({dists[0]}) ships py.typed but is not a hook dependency, so the hook "
                "types it as Any while backend/venv mypy sees the real types. Pin it in the "
                "hook (and in backend/requirements-dev.txt if it is not pinned there) or add "
                "it to EXEMPT with a reason"
            )
    return problems


# --- tests against the real repository ----------------------------------------------------


def test_mypy_hook_rev_matches_venv_pin():
    rev, _ = _load_hook()
    problems = check_rev(rev, _requirement_pins(REQUIREMENTS_DEV.read_text()))
    assert not problems, "\n".join(problems)


def test_every_hook_dependency_is_pinned_like_backend_requirements():
    _, deps = _load_hook()
    problems = check_pins(deps, _all_requirement_pins())
    assert not problems, "\n".join(problems)


def test_stub_sets_match_hook_and_requirements_dev():
    _, deps = _load_hook()
    problems = check_stub_sets(deps, _requirement_pins(REQUIREMENTS_DEV.read_text()))
    assert not problems, "\n".join(problems)


def test_no_stub_package_shadows_a_hook_dependency_with_inline_types():
    _, deps = _load_hook()
    problems = check_no_shadowing_stub(deps)
    assert not problems, "\n".join(problems)


def test_every_typed_import_resolves_in_the_hook_env():
    _, deps = _load_hook()
    hook_deps = {_split_requirement(d)[0] for d in deps}
    modules = set(_imported_modules())
    dists = _module_dists()
    # A name that an installed distribution provides is third-party even when a local directory
    # shares it (``backend/alembic`` holds the migrations; ``alembic`` is also the library).
    first_party = set(_first_party_names()) - {m for m, d in dists.items() if d}
    assert modules - first_party, "the import scan found no third-party modules; it is broken"
    problems = check_typed_imports(
        modules, first_party, hook_deps, EXEMPT, dists, allow_uninstalled=_PARTIAL_INSTALL
    )
    assert not problems, "\n".join(problems)


def test_exempt_modules_are_still_imported_somewhere():
    stale = sorted(set(EXEMPT) - _imported_modules())
    assert not stale, f"EXEMPT lists modules nothing imports any more: {stale}"


# --- self-test: every checker must be able to fire, and to stay quiet ---------------------


def test_checkers_fire_on_synthetic_bad_configs_and_stay_clean_on_good_ones():
    pins = {"mypy": "2.3.1", "types-redis": "4.6.0", "requests": "2.34.2"}
    all_pins = {"requests": {"requirements.txt": "2.34.2"}, "types-redis": {"d": "4.6.0"}}

    assert check_rev("v2.1.0", pins), "rev drift must be reported"
    assert not check_rev("v2.3.1", pins)
    assert check_rev("v2.3.1", {}), "a missing venv pin must be reported, not skipped"

    assert check_pins(["requests"], all_pins), "an unpinned hook dep must be reported"
    assert check_pins(["requests>=2"], all_pins), "a range is not a pin"
    assert check_pins(["requests==2.30.0"], all_pins), "a version that differs from requirements"
    assert check_pins(["httpx==0.28.1"], all_pins), "a dep with nothing to compare against"
    assert not check_pins(["requests==2.34.2"], all_pins)

    assert check_stub_sets(["types-redis==4.6.0"], {"mypy": "1"}), "hook-only stub"
    assert check_stub_sets([], {"types-redis": "4.6.0"}), "requirements-only stub"
    assert not check_stub_sets(["types-redis==4.6.0"], {"types-redis": "4.6.0"})

    assert check_no_shadowing_stub(["types-pytest==1", "pytest==9"]), "stub shadows inline types"
    assert not check_no_shadowing_stub(["pytest==9"])

    # pytest itself ships py.typed and is installed wherever this test runs.
    mapping = {"pytest": ["pytest"], "ghost": []}
    assert check_typed_imports({"pytest"}, set(), set(), {}, mapping), "typed + unlisted"
    assert not check_typed_imports({"pytest"}, set(), {"pytest"}, {}, mapping), "listed"
    assert not check_typed_imports({"pytest"}, set(), set(), {"pytest": "r"}, mapping), "exempt"
    assert not check_typed_imports({"pytest"}, {"pytest"}, set(), {}, mapping), "first-party"
    assert check_typed_imports({"ghost"}, set(), set(), {}, mapping), "not installed fails closed"
    assert not check_typed_imports({"ghost"}, set(), set(), {}, mapping, allow_uninstalled=True), (
        "not installed is tolerated on the partial CI install"
    )
    assert not check_typed_imports({"ghost"}, set(), {"ghost"}, {}, mapping), "hook dep by name"
