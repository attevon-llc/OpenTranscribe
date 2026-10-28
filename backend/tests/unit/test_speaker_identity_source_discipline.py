"""Every function that writes a speaker's name, link or suggestion records its origin (#1280).

``Speaker.identity_source`` only means something if no writer forgets it: a writer that sets
``display_name`` / ``profile_id`` without recording the source leaves a voiceprint-made identity
looking like a person's (never hidden while voiceprints are off, never purged) or a person's
looking like a voiceprint's. The same holds for the rest of the record (who made it, when, and
where it came from: ``identity_source_at`` / ``_user_id`` / ``_detail``) and for the origin of a
recommendation (``suggestion_detail``). Reviewing for that by eye is how the first writer would
be missed, so this walks ``backend/app`` and fails on any function that

* assigns ``display_name`` / ``profile_id`` without calling an IDENTITY helper of
  ``utils/speaker_identity_source`` (``record_human_identity``, ``record_machine_identity``,
  ``record_machine_link``, ``after_unlink``, ``after_name_cleared``, ``after_skip``), or
* assigns ``suggested_name`` / ``suggestion_source`` without calling a SUGGESTION helper
  (``record_suggestion``, ``clear_suggestion``, ``mark_review_skipped``), or
* assigns ``identity_source``, its record columns or ``suggestion_detail`` directly (only the
  helpers may; assigning ``identity_source`` alone used to count as recording it and let a
  writer skip the person, the time and the detail).

A reader helper (``hides_link`` ...) does NOT count: it was once accepted by accident.

The check is PER WRITE, not per function. A helper call covers a write only if it runs whenever
the write runs: a helper in one branch of an ``if`` does not cover a write in the other branch or
outside it, and a helper in a loop body or an ``except`` handler covers only what shares that
branch. Writes are found in every shape the code uses: an attribute assignment, ``setattr`` with
a literal name (and with a variable name on a speaker-named object), the keywords and the
dictionary keys (``"display_name"`` or ``Speaker.display_name``) of ``.values()`` / ``.update()``
and of the constructor, and a raw ``UPDATE speaker SET ...`` / ``INSERT INTO speaker (...)``
string.

A function that legitimately does none of this is listed in ``_ALLOWED`` with the reason; an
entry whose function no longer needs it fails the run (a stale exemption hides the next real
writer).
"""

from __future__ import annotations

import ast
import functools
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

APP = Path(__file__).resolve().parents[2] / "app"
HELPER_MODULE = "app.utils.speaker_identity_source"
_IDENTITY_FIELDS = frozenset({"display_name", "profile_id"})
_SUGGESTION_FIELDS = frozenset({"suggested_name", "suggestion_source"})
#: Columns only the helpers may assign.
_RECORD_FIELDS = frozenset(
    {
        "identity_source",
        "identity_source_at",
        "identity_source_user_id",
        "identity_source_detail",
        "suggestion_detail",
    }
)
_FIELDS = _IDENTITY_FIELDS | _SUGGESTION_FIELDS | _RECORD_FIELDS
IDENTITY_HELPERS = frozenset(
    {
        "record_human_identity",
        "record_machine_identity",
        "record_machine_link",
        "after_unlink",
        "after_name_cleared",
        "after_skip",
        # SQL-side twin for the purge's bulk UPDATE: nulls the source and its record together.
        "cleared_provenance_values",
    }
)
SUGGESTION_HELPERS = frozenset(
    {
        "record_suggestion",
        "clear_suggestion",
        "mark_review_skipped",
        # SQL-side twin for the purge's bulk UPDATE: clears name, source, confidence, detail.
        "cleared_suggestion_values",
    }
)
#: Local wrappers that call the helpers on the caller's behalf.
_WRAPPERS = {"_record_identity_source_on_edit": IDENTITY_HELPERS}

#: ``<path under app>::<function>`` -> why it need not record a source.
_ALLOWED: dict[str, str] = {
    "services/ingest_artifacts/service.py::__init__": (
        "not a Speaker: a plain roster entry object holding a label for the facts digest"
    ),
    "services/speaker_profile_rename.py::apply_profile_name_to_speakers": (
        "a person renamed the PROFILE and its linked speakers show the new name; who made each "
        "speaker's identity does not change, so the recorded source, person and detail stay. "
        "The profile's own change history is issue #1336"
    ),
    "services/speaker_status_service.py::add_computed_status": (
        "setattr over the closed set of computed-status columns (_PERSISTED_STATUS_FIELDS); "
        "none of them is a name, a link, a suggestion or the record of either"
    ),
    "tasks/transcription/speaker_processor.py::create_or_get_speaker": (
        "diarization creates a NEW row with display_name=None: no identity yet (NULL is right)"
    ),
}


_SQL_UPDATE = re.compile(
    r"\bupdate\s+\"?speaker\"?\s+set\b(?P<rest>.*?)(?:\bwhere\b|$)", re.I | re.S
)
_SQL_INSERT = re.compile(r"\binsert\s+into\s+\"?speaker\"?\s*\((?P<cols>[^)]*)\)", re.I)
#: A write whose field is a variable: any helper (identity or suggestion) is evidence it is handled.
DYNAMIC = "<dynamic>"


@dataclass(frozen=True)
class Write:
    line: int
    field: str
    node: ast.AST


def _own_nodes(func: ast.AST) -> Iterator[ast.AST]:
    """Nodes of ``func`` that are not inside a nested function (those are judged on their own)."""
    stack = list(ast.iter_child_nodes(func))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            stack.extend(ast.iter_child_nodes(node))


def _fields_in_sql(text: str) -> list[str]:
    """Guarded columns a raw ``UPDATE speaker SET`` / ``INSERT INTO speaker`` string writes."""
    found: list[str] = []
    for match in _SQL_UPDATE.finditer(text):
        found += [f for f in _FIELDS if re.search(rf"\b{f}\b", match.group("rest"), re.I)]
    for match in _SQL_INSERT.finditer(text):
        found += [f for f in _FIELDS if re.search(rf"\b{f}\b", match.group("cols"), re.I)]
    return found


def _setattr_write(node: ast.Call) -> Write | None:
    """A ``setattr`` of a guarded field, or of a variable one on a speaker-named object."""
    if len(node.args) < 2:
        return None
    obj, name_arg = node.args[0], node.args[1]
    if isinstance(name_arg, ast.Constant):
        if name_arg.value in _FIELDS:
            return Write(node.lineno, str(name_arg.value), node)
        return None
    if isinstance(obj, ast.Name) and "speaker" in obj.id.lower():
        return Write(node.lineno, DYNAMIC, node)
    return None


def _bulk_writes(node: ast.Call, *, constructor: bool, bulk: bool) -> list[Write]:
    """Guarded fields written by a constructor / ``.values()`` / ``.update()`` call."""
    found: list[Write] = []
    if constructor or bulk:
        found.extend(Write(node.lineno, kw.arg, node) for kw in node.keywords if kw.arg in _FIELDS)
    if bulk:
        for dictionary in (a for a in node.args if isinstance(a, ast.Dict)):
            for key in dictionary.keys:
                if isinstance(key, ast.Constant) and key.value in _FIELDS:
                    found.append(Write(node.lineno, str(key.value), node))
                elif isinstance(key, ast.Attribute) and key.attr in _FIELDS:
                    found.append(Write(node.lineno, key.attr, node))
    return found


def _writes(func: ast.AST) -> list[Write]:
    """Every assignment of a guarded field made directly inside ``func``."""
    own = list(_own_nodes(func))
    prose = {id(n.value) for n in own if isinstance(n, ast.Expr)}  # docstrings, bare strings
    mentions_speaker = any(isinstance(n, ast.Name) and n.id == "Speaker" for n in own)
    found: list[Write] = []
    for node in own:
        if isinstance(node, ast.Assign | ast.AugAssign | ast.AnnAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            found.extend(
                Write(node.lineno, t.attr, node)
                for t in targets
                if isinstance(t, ast.Attribute) and t.attr in _FIELDS
            )
        elif isinstance(node, ast.Call):
            callee = node.func
            if isinstance(callee, ast.Name) and callee.id == "setattr":
                write = _setattr_write(node)
                found.extend([write] if write else [])
            found.extend(
                _bulk_writes(
                    node,
                    constructor=isinstance(callee, ast.Name) and callee.id == "Speaker",
                    bulk=mentions_speaker
                    and isinstance(callee, ast.Attribute)
                    and callee.attr in ("values", "update"),
                )
            )
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in prose:
                found.extend(Write(node.lineno, f, node) for f in _fields_in_sql(node.value))
    return found


def _helper_calls(func: ast.AST, helper_names: dict[str, str]) -> list[tuple[str, ast.AST]]:
    """``(real helper name, call node)`` for each helper call made directly inside ``func``.

    ``helper_names`` maps each local alias to the helper it imports.
    """
    calls: list[tuple[str, ast.AST]] = []
    for node in _own_nodes(func):
        if not isinstance(node, ast.Call):
            continue
        callee = node.func
        name = callee.id if isinstance(callee, ast.Name) else getattr(callee, "attr", None)
        if name in helper_names:
            calls.append((helper_names[name], node))
        elif name in _WRAPPERS:
            calls.extend((h, node) for h in sorted(_WRAPPERS[name]))
    return calls


def _called_helpers(func: ast.AST, helper_names: dict[str, str]) -> set[str]:
    return {name for name, _ in _helper_calls(func, helper_names)}


def _branch(parent: ast.AST, child: ast.AST) -> str | None:
    """Which conditional branch of ``parent`` ``child`` sits in, or ``None`` when it always runs."""
    if isinstance(parent, ast.If):
        return "body" if child in parent.body else "else" if child in parent.orelse else None
    if isinstance(parent, ast.IfExp):
        return "body" if child is parent.body else "else" if child is parent.orelse else None
    if isinstance(parent, ast.For | ast.AsyncFor | ast.While):
        if child in parent.body:
            return "loop"
        return "loop-else" if child in parent.orelse else None
    if isinstance(parent, ast.Try):
        for index, handler in enumerate(parent.handlers):
            if child is handler:
                return f"except{index}"
        if child in parent.orelse:
            return "try-else"
        return "finally" if child in parent.finalbody else None  # the try body always starts
    if isinstance(parent, ast.match_case):
        return None
    if isinstance(parent, ast.Match):
        for index, case in enumerate(parent.cases):
            if child is case:
                return f"case{index}"
    if isinstance(parent, ast.BoolOp):
        index = next((i for i, v in enumerate(parent.values) if v is child), 0)
        return f"operand{index}" if index else None
    return None


def _guards(node: ast.AST, parents: dict[ast.AST, ast.AST], func: ast.AST) -> tuple[Any, ...]:
    """The conditional branches between ``func`` and ``node``, outermost first."""
    chain: list[tuple[int, str]] = []
    child = node
    while child is not func:
        parent = parents[child]
        label = _branch(parent, child)
        if label is not None:
            chain.append((id(parent), label))
        child = parent
    return tuple(reversed(chain))


def _always(prefix: tuple[Any, ...], chains: list[tuple[Any, ...]]) -> bool:
    """Does a helper run on EVERY path through the region ``prefix`` (a chain of branches)?

    True when one sits directly in the region, or when the region splits on an ``if`` whose two
    branches each always run one (``if x: record_a(...) else: record_b(...)``).
    """
    if prefix in chains:
        return True
    splits: dict[int, set[str]] = {}
    for chain in chains:
        if len(chain) > len(prefix) and chain[: len(prefix)] == prefix:
            node_id, label = chain[len(prefix)]
            splits.setdefault(node_id, set()).add(label)
    return any(
        labels >= {"body", "else"}
        and _always(prefix + ((node_id, "body"),), chains)
        and _always(prefix + ((node_id, "else"),), chains)
        for node_id, labels in splits.items()
    )


def _covered(write: tuple[Any, ...], chains: list[tuple[Any, ...]]) -> bool:
    """Is some region that contains the write one a helper always runs in?"""
    return any(_always(write[:depth], chains) for depth in range(len(write) + 1))


def _needed(field: str) -> frozenset[str] | None:
    """The helpers that satisfy a write of ``field``; ``None`` = nothing may write it."""
    if field in _IDENTITY_FIELDS:
        return IDENTITY_HELPERS
    if field in _SUGGESTION_FIELDS:
        return SUGGESTION_HELPERS
    if field == DYNAMIC:
        return IDENTITY_HELPERS | SUGGESTION_HELPERS
    return None


def _helper_names(tree: ast.AST) -> dict[str, str]:
    """Local alias -> helper name, for every helper the module imports from HELPER_MODULE."""
    names: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == HELPER_MODULE:
            for alias in node.names:
                if alias.name in IDENTITY_HELPERS | SUGGESTION_HELPERS:
                    names[alias.asname or alias.name] = alias.name
    return names


def unmarked_writers(source: str) -> list[tuple[str, int, str]]:
    """``(function, line, field)`` for each WRITE in ``source`` no helper call covers."""
    tree = ast.parse(source)
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    helper_names = _helper_names(tree)
    out: list[tuple[str, int, str]] = []
    for func in ast.walk(tree):
        if not isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        helpers = [
            (name, _guards(call, parents, func)) for name, call in _helper_calls(func, helper_names)
        ]
        for write in _writes(func):
            needed = _needed(write.field)
            chains = [chain for name, chain in helpers if needed and name in needed]
            if needed is None or not _covered(_guards(write.node, parents, func), chains):
                out.append((func.name, write.line, write.field))
    return out


@functools.cache
def _scan() -> tuple[dict[str, list[tuple[int, str]]], set[str]]:
    flagged: dict[str, list[tuple[int, str]]] = {}
    seen_writers: set[str] = set()
    for path in sorted(APP.rglob("*.py")):
        rel = path.relative_to(APP).as_posix()
        if rel.startswith("alembic") or rel == "utils/speaker_identity_source.py":
            continue
        source = path.read_text()
        if not any(f in source for f in _FIELDS):
            continue
        tree = ast.parse(source)
        for func in ast.walk(tree):
            if isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef) and (
                _writes(func) or _called_helpers(func, _helper_names(tree))
            ):
                seen_writers.add(f"{rel}::{func.name}")
        for name, line, field in unmarked_writers(source):
            flagged.setdefault(f"{rel}::{name}", []).append((line, field))
    return flagged, seen_writers


def test_every_writer_of_a_speakers_name_or_link_records_its_source():
    flagged, _ = _scan()
    missing = {k: v for k, v in flagged.items() if k not in _ALLOWED}
    assert not missing, (
        "These functions write a speaker's name, link, suggestion or the record of either "
        f"without the matching helper from {HELPER_MODULE} (line, field): {missing}. Record "
        "who wrote it, or add the function to _ALLOWED with the reason it needs no record."
    )


def test_no_exemption_outlives_its_reason():
    flagged, _ = _scan()
    stale = sorted(k for k in _ALLOWED if k not in flagged)
    assert not stale, f"_ALLOWED entries that no longer need it (delete them): {stale}"


def test_the_scan_sees_the_known_writers():
    """Guard the guard: a walker that matched nothing would pass everything."""
    _, writers = _scan()
    for known in (
        "services/speaker_matching_service.py::_handle_speaker_match",
        "services/speaker_cleanup.py::join_cluster_identity",
        "services/speaker_cleanup.py::unlink_from_profile",
        "api/endpoints/speakers.py::_accept_speaker_profile_match",
        "api/endpoints/speaker_update.py::_apply_high_confidence_match",
        # the writers of a recommendation
        "api/endpoints/speaker_update.py::_persist_match_results",
        "services/speaker_matching_service.py::_handle_no_speaker_match",
        "tasks/speaker_identification_task.py::_store_speaker_predictions",
        "scripts/batch_speaker_matching.py::batch_process_speaker_matches",
        "services/privacy/purge_voiceprints.py::_revert_machine_identities",
    ):
        assert known in writers, f"the scan no longer finds {known}"


def test_detector_fires_on_an_unmarked_assignment():
    source = (
        "def rename(speaker, name):\n"
        "    speaker.display_name = name\n"
        "def link(speaker, pid):\n"
        "    setattr(speaker, 'profile_id', pid)\n"
        "def bulk(db):\n"
        "    db.execute(update(Speaker).values(display_name=None))\n"
    )
    assert {(n, f) for n, _, f in unmarked_writers(source)} == {
        ("rename", "display_name"),
        ("link", "profile_id"),
        ("bulk", "display_name"),
    }


@pytest.mark.parametrize(
    "body",
    [
        "    speaker.display_name = name\n    record_human_identity(speaker, user_id=1, origin='typed')\n",
        "    speaker.profile_id = 1\n    after_unlink(speaker)\n",
        "    speaker.suggested_name = name\n    record_suggestion(speaker, source='s', name=name, confidence=1, detail={})\n",
        "    speaker.suggested_name = None\n    clear_suggestion(speaker)\n",
        "    speaker.suggestion_source = 'user_skipped'\n    mark_review_skipped(speaker)\n",
        "    speaker.display_name = name\n    speaker.suggested_name = name\n"
        "    record_human_identity(speaker, user_id=1, origin='typed')\n    clear_suggestion(speaker)\n",
        "    speaker.display_name = name\n    _record_identity_source_on_edit(speaker, u)\n",
    ],
)
def test_detector_stays_clean_on_a_marked_one(body):
    source = (
        f"from {HELPER_MODULE} import record_human_identity, after_unlink, record_suggestion, "
        "clear_suggestion, mark_review_skipped\n"
        f"def write(speaker, name, u=None):\n{body}"
    )
    assert unmarked_writers(source) == []


def test_detector_stays_clean_through_an_import_alias():
    source = (
        f"from {HELPER_MODULE} import record_human_identity as rec\n"
        "def write(speaker, name):\n"
        "    speaker.display_name = name\n"
        "    rec(speaker, user_id=1, origin='typed')\n"
    )
    assert unmarked_writers(source) == []


@pytest.mark.parametrize(
    ("body", "field"),
    [
        # the old escape hatch: assigning identity_source alone is no longer "recording" it
        (
            "    speaker.display_name = name\n    speaker.identity_source = 'human'\n",
            "display_name",
        ),
        # a READER helper from the same module must not satisfy a writer
        ("    speaker.display_name = name\n    hides_link('human', True)\n", "display_name"),
        # a suggestion needs a suggestion helper; an identity helper is the wrong one
        (
            "    speaker.suggested_name = name\n    record_human_identity(speaker, user_id=1, origin='typed')\n",
            "suggested_name",
        ),
        ("    speaker.suggestion_source = 'llm_analysis'\n", "suggestion_source"),
        ("    setattr(speaker, 'suggested_name', name)\n", "suggested_name"),
        # the record columns are the helpers' alone, even next to a helper call
        (
            "    record_human_identity(speaker, user_id=1, origin='typed')\n    speaker.identity_source_user_id = 2\n",
            "identity_source_user_id",
        ),
        (
            "    record_suggestion(speaker, source='s', name=name, confidence=1, detail={})\n    speaker.suggestion_detail = {}\n",
            "suggestion_detail",
        ),
    ],
)
def test_detector_fires_on_what_the_helpers_do_not_cover(body, field):
    source = (
        f"from {HELPER_MODULE} import record_human_identity, record_suggestion, hides_link\n"
        f"def write(speaker, name):\n{body}"
    )
    assert field in {f for _, _, f in unmarked_writers(source)}


def test_detector_fires_on_an_unmarked_bulk_suggestion_write():
    source = (
        "def purge(db):\n"
        "    db.execute(update(Speaker).values(suggested_name=None, suggestion_source=None))\n"
    )
    assert {f for _, _, f in unmarked_writers(source)} == {"suggested_name", "suggestion_source"}


# --- the shapes an earlier per-function scan could not see --------------------------------------

_IMPORTS = (
    f"from {HELPER_MODULE} import record_human_identity, record_machine_identity, "
    "cleared_suggestion_values, clear_suggestion, record_suggestion\n"
)


def _fires(body: str) -> set[tuple[str, str]]:
    return {(n, f) for n, _, f in unmarked_writers(_IMPORTS + body)}


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        # an attribute-keyed dictionary, the form SQLAlchemy accepts beside the string keys
        (
            "def a(db):\n    db.query(Speaker).update({Speaker.display_name: None})\n",
            {("a", "display_name")},
        ),
        (
            "def a(db):\n    db.execute(update(Speaker).values({Speaker.suggested_name: None}))\n",
            {("a", "suggested_name")},
        ),
        # raw SQL
        (
            "def a(db):\n    db.execute(text('UPDATE speaker SET display_name = NULL WHERE id = 1'))\n",
            {("a", "display_name")},
        ),
        (
            "def a(db):\n    db.execute(text('update \"speaker\" set profile_id = 3, name = x'))\n",
            {("a", "profile_id")},
        ),
        (
            "def a(db):\n    db.execute(text('INSERT INTO speaker (name, suggested_name) VALUES (1, 2)'))\n",
            {("a", "suggested_name")},
        ),
        # a field named by a variable on a speaker
        (
            "def a(speaker, field, value):\n    setattr(speaker, field, value)\n",
            {("a", "<dynamic>")},
        ),
        # one helper call in one branch does not record a write in another
        (
            "def a(speaker, c):\n"
            "    if c:\n        speaker.display_name = 'x'\n"
            "    else:\n        speaker.display_name = 'y'\n"
            "        record_human_identity(speaker, user_id=1, origin='typed')\n",
            {("a", "display_name")},
        ),
        (
            "def a(speaker, c):\n"
            "    speaker.display_name = 'x'\n"
            "    if c:\n        record_human_identity(speaker, user_id=1, origin='typed')\n",
            {("a", "display_name")},
        ),
        (
            "def a(speaker, rows):\n"
            "    for r in rows:\n        record_human_identity(speaker, user_id=1, origin='typed')\n"
            "    speaker.display_name = 'x'\n",
            {("a", "display_name")},
        ),
        (
            "def a(speaker):\n"
            "    try:\n        speaker.display_name = 'x'\n"
            "    except ValueError:\n        record_human_identity(speaker, user_id=1, origin='t')\n",
            {("a", "display_name")},
        ),
        # a helper on one side only leaves the other side's write bare
        (
            "def a(speaker, c):\n"
            "    if c:\n        speaker.display_name = 'x'\n"
            "        record_human_identity(speaker, user_id=1, origin='typed')\n"
            "    else:\n        speaker.profile_id = 2\n",
            {("a", "profile_id")},
        ),
    ],
)
def test_detector_fires_on_the_shapes_a_per_function_scan_missed(body, expected):
    assert _fires(body) == expected


@pytest.mark.parametrize(
    "body",
    [
        "def a(db):\n    db.execute(text('UPDATE speaker_profile SET name = 1 WHERE id = 2'))\n",
        "def a(db):\n    db.execute(text('UPDATE speaker SET computed_status = 1 WHERE id = 2'))\n",
        "def a(db):\n    db.execute(text('SELECT display_name, profile_id FROM speaker'))\n",
        "def a(db):\n    '''UPDATE speaker SET display_name = NULL is described here.'''\n",
        "def a(db):\n    db.query(Speaker).update({Speaker.computed_status: 1})\n",
        "def a(config, key, value):\n    setattr(config, key, value)\n",
        "def a(speaker):\n    setattr(speaker, 'computed_status', 1)\n",
        # helper and write in the same branch
        "def a(speaker, c):\n"
        "    if c:\n        speaker.display_name = 'x'\n"
        "        record_human_identity(speaker, user_id=1, origin='typed')\n",
        # a helper after the branches covers a write in each
        "def a(speaker, c):\n"
        "    if c:\n        speaker.display_name = 'x'\n"
        "    else:\n        speaker.display_name = 'y'\n"
        "    record_human_identity(speaker, user_id=1, origin='typed')\n",
        # a helper in EACH branch of an if covers a write made before it
        "def a(speaker, c):\n"
        "    speaker.display_name = 'x'\n"
        "    if c:\n        record_human_identity(speaker, user_id=1, origin='typed')\n"
        "    else:\n        record_machine_identity(speaker, previous_display_name=None, detail={})\n",
        # a field named by a variable, with the helper beside the loop
        "def a(speaker, items):\n"
        "    for field, value in items:\n        setattr(speaker, field, value)\n"
        "    record_human_identity(speaker, user_id=1, origin='typed')\n",
        # a try body always starts: its helper covers a write after it
        "def a(speaker):\n"
        "    try:\n        record_human_identity(speaker, user_id=1, origin='typed')\n"
        "    except ValueError:\n        pass\n"
        "    speaker.display_name = 'x'\n",
        # the SQL-side twin of the suggestion helpers
        "def a(db):\n"
        "    db.execute(update(Speaker).values(**cleared_suggestion_values()))\n"
        "    db.execute(update(Speaker).values({Speaker.suggested_name: None}))\n",
    ],
)
def test_detector_stays_clean_on_what_is_recorded_or_not_a_speaker_name(body):
    assert unmarked_writers(_IMPORTS + body) == []
