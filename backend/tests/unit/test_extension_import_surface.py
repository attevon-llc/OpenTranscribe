"""The documented extension surface resolves, and every move of it is versioned and documented.

An out-of-tree extension imports core names by path. A refactor that moves one breaks the
extension at import time, and nothing in this repository notices, because the importer lives
elsewhere. ``tests/fixtures/extension_surface.txt`` is the checked-in list of those paths; this
test fails when one stops resolving, when the list and ``CLOUD_SEAM_VERSION`` disagree, or when a
move is not recorded in the downstream-changes document for its seam version.
"""

from __future__ import annotations

import fnmatch
import importlib
import inspect
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.auth.constants import CLOUD_SEAM_VERSION

BACKEND = Path(__file__).resolve().parents[2]
REPO = BACKEND.parent
SURFACE_FILE = BACKEND / "tests" / "fixtures" / "extension_surface.txt"

_ENTRY = re.compile(r"^(?P<module>[\w.]+):(?P<name>\w+\*?)(?:\((?P<params>[^)]*)\))?$")
_MOVED = re.compile(r"^moved (?P<old>\S+) -> (?P<new>\S+) @(?P<version>\d+)$")


@dataclass(frozen=True)
class Ref:
    """A ``module:name`` reference, optionally with the expected parameter names."""

    module: str
    name: str
    params: tuple[str, ...] | None

    @property
    def path(self) -> str:
        return f"{self.module}:{self.name}"


@dataclass(frozen=True)
class Moved:
    """A surface name that moved in seam version ``version``."""

    old: Ref
    new: Ref
    version: int


@dataclass(frozen=True)
class Surface:
    """Parsed contents of the surface file."""

    seam_version: int
    downstream_docs: dict[int, Path]
    entries: tuple[Ref, ...]
    moved: tuple[Moved, ...]


def _parse_ref(text: str) -> Ref:
    match = _ENTRY.match(text)
    if match is None:
        raise ValueError(f"malformed surface reference: {text!r}")
    params = match["params"]
    return Ref(
        module=match["module"],
        name=match["name"],
        params=None if params is None else tuple(p.strip() for p in params.split(",") if p.strip()),
    )


def parse_surface(text: str) -> Surface:
    """Parse the surface file format documented at the top of the file."""
    seam_version: int | None = None
    docs: dict[int, Path] = {}
    entries: list[Ref] = []
    moved: list[Moved] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("seam_version "):
            seam_version = int(line.split()[1])
        elif line.startswith("downstream_doc "):
            _, version, rel = line.split(maxsplit=2)
            docs[int(version)] = REPO / rel
        elif line.startswith("moved "):
            match = _MOVED.match(line)
            if match is None:
                raise ValueError(f"malformed moved line: {line!r}")
            moved.append(
                Moved(_parse_ref(match["old"]), _parse_ref(match["new"]), int(match["version"]))
            )
        else:
            entries.append(_parse_ref(line))
    if seam_version is None:
        raise ValueError("surface file has no seam_version line")
    return Surface(seam_version, docs, tuple(entries), tuple(moved))


def resolve(ref: Ref) -> list[str]:
    """Return why *ref* does not resolve on this tree; an empty list means it does."""
    try:
        module = importlib.import_module(ref.module)
    except ImportError as exc:
        return [f"{ref.path}: module does not import ({exc})"]
    if ref.name.endswith("*"):
        if not fnmatch.filter(vars(module), ref.name):
            return [f"{ref.path}: no name in {ref.module} matches"]
        return []
    if not hasattr(module, ref.name):
        return [f"{ref.path}: {ref.module} has no attribute {ref.name}"]
    if ref.params is not None:
        actual = tuple(inspect.signature(getattr(module, ref.name)).parameters)
        if actual != ref.params:
            return [f"{ref.path}: parameters are {actual}, surface promises {ref.params}"]
    return []


SURFACE = parse_surface(SURFACE_FILE.read_text())


def test_surface_version_matches_cloud_seam_version() -> None:
    """Changing the seam without reviewing the list (or the reverse) is caught here."""
    assert SURFACE.seam_version == CLOUD_SEAM_VERSION


@pytest.mark.parametrize("ref", SURFACE.entries, ids=lambda r: r.path)
def test_surface_entry_resolves(ref: Ref) -> None:
    assert resolve(ref) == []


@pytest.mark.parametrize("move", SURFACE.moved, ids=lambda m: m.old.path)
def test_moved_name_is_versioned_documented_and_gone(move: Moved) -> None:
    """A move needs a seam bump, a downstream-doc row naming both paths, and no shim left behind."""
    assert move.version <= CLOUD_SEAM_VERSION
    assert resolve(move.new) == [], "the new home must resolve"
    assert resolve(move.old) != [], (
        f"{move.old.path} still resolves: drop the moved line or the leftover alias"
    )
    doc = SURFACE.downstream_docs.get(move.version)
    assert doc is not None, f"no downstream_doc line for seam version {move.version}"
    assert doc.is_file(), f"{doc} is missing"
    text = doc.read_text()
    for ref in (move.old, move.new):
        documented = f"{ref.module}.{ref.name.rstrip('*')}"
        assert documented in text, f"{doc.name} does not mention {documented}"


def test_every_listed_downstream_doc_exists() -> None:
    assert SURFACE.downstream_docs
    for version, doc in SURFACE.downstream_docs.items():
        assert doc.is_file(), f"downstream doc for seam v{version} is missing: {doc}"


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("app.auth.constants:CLOUD_SEAM_VERSION", []),
        ("app.auth.constants:NO_SUCH_NAME", ["has no attribute"]),
        ("app.no_such_module:anything", ["does not import"]),
        ("app.auth.direct_auth:create_access_token(data, expires_delta)", []),
        ("app.auth.direct_auth:create_access_token(subject, expires_delta)", ["parameters are"]),
        ("app.core.chat_defaults:DEFAULT_CHAT_*", []),
        ("app.core.constants:DEFAULT_CHAT_*", ["no name"]),
    ],
)
def test_resolver_reports_what_it_should(line: str, expected: list[str]) -> None:
    """The resolver must be able to fail: an always-empty answer would pass every entry."""
    problems = resolve(_parse_ref(line))
    assert len(problems) == len(expected)
    for problem, fragment in zip(problems, expected, strict=True):
        assert fragment in problem
