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

A function that legitimately does none of this is listed in ``_ALLOWED`` with the reason; an
entry whose function no longer needs it fails the run (a stale exemption hides the next real
writer).
"""

from __future__ import annotations

import ast
from pathlib import Path

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
    "tasks/transcription/speaker_processor.py::create_or_get_speaker": (
        "diarization creates a NEW row with display_name=None: no identity yet (NULL is right)"
    ),
}


def _writes(func: ast.AST) -> list[tuple[int, str]]:
    """``(line, field)`` of every assignment of a guarded field inside ``func``."""
    mentions_speaker = any(isinstance(n, ast.Name) and n.id == "Speaker" for n in ast.walk(func))
    found: list[tuple[int, str]] = []
    for node in ast.walk(func):
        if isinstance(node, ast.Assign | ast.AugAssign | ast.AnnAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Attribute) and target.attr in _FIELDS:
                    found.append((node.lineno, target.attr))
        elif isinstance(node, ast.Call):
            callee = node.func
            if (
                isinstance(callee, ast.Name)
                and callee.id == "setattr"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value in _FIELDS
            ):
                found.append((node.lineno, str(node.args[1].value)))
            is_constructor = isinstance(callee, ast.Name) and callee.id == "Speaker"
            is_bulk = isinstance(callee, ast.Attribute) and callee.attr in ("values", "update")
            if is_constructor or (is_bulk and mentions_speaker):
                found.extend((node.lineno, kw.arg) for kw in node.keywords if kw.arg in _FIELDS)
            if is_bulk and mentions_speaker:
                for arg in node.args:
                    if isinstance(arg, ast.Dict):
                        found.extend(
                            (node.lineno, str(k.value))
                            for k in arg.keys
                            if isinstance(k, ast.Constant) and k.value in _FIELDS
                        )
    return found


def _called_helpers(func: ast.AST, helper_names: dict[str, str]) -> set[str]:
    """Helpers of speaker_identity_source (or local wrappers) ``func`` calls, by real name.

    ``helper_names`` maps each local alias to the helper it imports.
    """
    called: set[str] = set()
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        callee = node.func
        name = callee.id if isinstance(callee, ast.Name) else getattr(callee, "attr", None)
        if name in helper_names:
            called.add(helper_names[name])
        elif name in _WRAPPERS:
            called |= _WRAPPERS[name]
    return called


def _needed(field: str) -> frozenset[str] | None:
    """The helpers that satisfy a write of ``field``; ``None`` = nothing may write it."""
    if field in _IDENTITY_FIELDS:
        return IDENTITY_HELPERS
    if field in _SUGGESTION_FIELDS:
        return SUGGESTION_HELPERS
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
    """``(function, line, field)`` for each writer in ``source`` that records no source."""
    tree = ast.parse(source)
    helper_names = _helper_names(tree)
    out: list[tuple[str, int, str]] = []
    for func in ast.walk(tree):
        if not isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        # A nested function is judged on its own; its parent is judged without it.
        called = _called_helpers(func, helper_names)
        for line, field in _writes(func):
            needed = _needed(field)
            if needed is None or not (called & needed):
                out.append((func.name, line, field))
    return out


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
