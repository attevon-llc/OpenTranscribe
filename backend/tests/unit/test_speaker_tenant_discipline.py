"""No code path may take a speaker's tenant from the speaker row's own ``organization_id``.

The diarization pipeline creates ``Speaker`` rows without stamping ``organization_id``, so
the column is NULL for essentially every speaker. A speaker's tenant is its FILE's
(``media_file.organization_id``; see ``PurgeScope.speaker_filters`` and
``services/CLAUDE.md``). A filter on the speaker column matches nothing in an organization
and everything in the personal workspace; an authorization check fed the column evaluates an
in-organization row as personal.

This gate AST-scans ``app/`` for the shapes that reintroduce it:

  * ``Speaker.organization_id`` (the mapped column, in a filter or a join);
  * ``<speaker-row variable>.organization_id`` where the variable is named for a speaker
    row (``speaker``, ``target_speaker``, ...). Names that also say ``profile``,
    ``cluster``, ``collection`` or ``doc`` are tenant-stamped records and are not matched;
    ``speaker.media_file.organization_id`` reads the file and is not matched;
  * ``getattr(<speaker-row variable>, "organization_id")``;
  * ``Speaker`` handed to a generic tenant helper (``scope_to_context``, ``sql_filters``).

A legitimate use must be allowlisted under ``"<path relative to app/>::<enclosing function>"``
with a written reason. The list is empty: there is no legitimate use today. A STALE entry
fails the run, so the list cannot rot into a blanket exemption.

Limits, stated so the gate is not mistaken for proof: a speaker row held in a variable whose
name does not say "speaker" is not recognised, and ``filter_by(organization_id=...)`` on a
``Speaker`` query is not either.
"""

from __future__ import annotations

import ast
from functools import cache
from pathlib import Path

_APP_ROOT = Path(__file__).resolve().parents[2] / "app"

#: Identifier fragments marking a record that DOES carry a stamped ``organization_id``.
_STAMPED_RECORD_FRAGMENTS = ("profile", "cluster", "collection", "doc", "hit", "payload")

_GENERIC_TENANT_HELPERS = {"scope_to_context", "sql_filters"}


def _enclosing_function_names(tree: ast.AST) -> dict[int, str]:
    names: dict[int, str] = {}

    def visit(node: ast.AST, current: str) -> None:
        for child in ast.iter_child_nodes(node):
            inner = current
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                inner = child.name
            names[id(child)] = inner
            visit(child, inner)

    visit(tree, "<module>")
    return names


def _is_speaker_row_name(node: ast.AST) -> bool:
    """A bare variable or attribute whose final identifier names a speaker ROW."""
    if isinstance(node, ast.Name):
        ident = node.id
    elif isinstance(node, ast.Attribute):
        ident = node.attr
    else:
        return False
    lowered = ident.lower()
    return "speaker" in lowered and not any(f in lowered for f in _STAMPED_RECORD_FRAGMENTS)


def _is_speaker_class(node: ast.AST) -> bool:
    return isinstance(node, ast.Name) and node.id == "Speaker"


def _call_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _is_speaker_tenant_read(node: ast.AST) -> bool:
    if isinstance(node, ast.Attribute):
        return (
            node.attr == "organization_id"
            and isinstance(node.ctx, ast.Load)
            and (_is_speaker_class(node.value) or _is_speaker_row_name(node.value))
        )
    if not isinstance(node, ast.Call):
        return False
    name = _call_name(node)
    if name == "getattr":
        return (
            len(node.args) >= 2
            and _is_speaker_row_name(node.args[0])
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == "organization_id"
        )
    if name in _GENERIC_TENANT_HELPERS:
        return any(_is_speaker_class(arg) for arg in node.args)
    return False


def scan_source(source: str, rel_path: str) -> tuple[set[str], int]:
    """Return ``("<path>::<function>" keys of every offending site, organization_id reads seen)``."""
    tree = ast.parse(source)
    enclosing = _enclosing_function_names(tree)
    found = {
        f"{rel_path}::{enclosing.get(id(node), '<module>')}"
        for node in ast.walk(tree)
        if _is_speaker_tenant_read(node)
    }
    reads = sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "organization_id"
    )
    return found, reads


@cache
def scan_tree() -> tuple[set[str], int]:
    found: set[str] = set()
    reads = 0
    for path in sorted(_APP_ROOT.rglob("*.py")):
        keys, count = scan_source(path.read_text(), path.relative_to(_APP_ROOT).as_posix())
        found |= keys
        reads += count
    return found, reads


#: ``key -> reason``. Empty on purpose; see the module docstring.
ALLOWLIST: dict[str, str] = {}


def test_no_code_takes_a_speakers_tenant_from_the_speaker_row():
    unexplained = sorted(scan_tree()[0] - set(ALLOWLIST))
    assert not unexplained, (
        "These sites filter or authorize on the speaker row's own organization_id, which the "
        "pipeline never stamps. Derive the tenant from the speaker's file "
        "(media_file.organization_id, or Speaker.media_file_id.in_(file_ids_in_scope(...))), or "
        "allowlist the site with a written reason:\n  " + "\n  ".join(unexplained)
    )


def test_no_allowlist_entry_is_stale():
    stale = sorted(set(ALLOWLIST) - scan_tree()[0])
    assert not stale, "Delete these ALLOWLIST entries; the site is gone:\n  " + "\n  ".join(stale)


def test_every_allowlist_entry_carries_a_reason():
    thin = [key for key, reason in ALLOWLIST.items() if len(reason.strip()) < 40]
    assert not thin, "Write a real reason for:\n  " + "\n  ".join(thin)


def test_the_scan_is_not_vacuous():
    """The tree has many tenant reads; a scan that saw almost none is looking at nothing."""
    assert scan_tree()[1] > 200


# --- the scanner itself: each shape must fire, and each look-alike must stay clean -------


def _hits(source: str) -> set[str]:
    return scan_source(source, "x.py")[0]


def test_scanner_fires_on_the_mapped_column():
    assert _hits("def f(q):\n    return q.filter(Speaker.organization_id == 3)\n") == {"x.py::f"}


def test_scanner_fires_on_a_speaker_row_variable():
    assert _hits("def g(speaker):\n    return speaker.organization_id\n") == {"x.py::g"}
    assert _hits("def g(t):\n    return t.target_speaker.organization_id\n") == {"x.py::g"}


def test_scanner_fires_on_getattr_of_the_column():
    assert _hits('def h(speaker):\n    return getattr(speaker, "organization_id", None)\n') == {
        "x.py::h"
    }


def test_scanner_fires_when_the_class_is_handed_to_a_generic_tenant_helper():
    assert _hits("def k(q, ctx):\n    return scope_to_context(q, Speaker, ctx)\n") == {"x.py::k"}
    assert _hits("def k(scope):\n    return scope.sql_filters(Speaker)\n") == {"x.py::k"}


def test_scanner_stays_clean_on_stamped_records_and_on_the_file():
    clean = (
        "def m(speaker, speaker_profile, cluster, SpeakerProfile, SpeakerCluster):\n"
        "    a = speaker.media_file.organization_id\n"
        "    b = speaker_profile.organization_id\n"
        "    c = SpeakerProfile.organization_id\n"
        "    d = SpeakerCluster.organization_id\n"
        "    e = cluster.organization_id\n"
        "    return scope_to_context(None, SpeakerProfile, None)\n"
    )
    assert _hits(clean) == set()


def test_scanner_ignores_a_store_to_the_column():
    assert _hits("def n(speaker):\n    speaker.organization_id = 4\n") == set()
