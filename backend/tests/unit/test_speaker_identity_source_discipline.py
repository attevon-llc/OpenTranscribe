"""Every function that writes a speaker's name or profile link records who wrote it (#1280).

``Speaker.identity_source`` only means something if no writer forgets it: a writer that sets
``display_name`` / ``profile_id`` without recording the source leaves a voiceprint-made identity
looking like a person's (never hidden while voiceprints are off, never purged) or a person's
looking like a voiceprint's. Reviewing for that by eye is how the first writer would be missed, so
this walks ``backend/app`` and fails on any function that assigns either field on a speaker and
neither calls a helper from ``utils/speaker_identity_source`` nor sets ``identity_source`` itself.

A function that legitimately does neither is listed in ``_ALLOWED`` with the reason; an entry
whose function no longer needs it fails the run (a stale exemption hides the next real writer).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[2] / "app"
HELPER_MODULE = "app.utils.speaker_identity_source"
_FIELDS = frozenset({"display_name", "profile_id"})
#: Local wrappers that call the helpers on the caller's behalf.
_WRAPPERS = frozenset({"_record_identity_source_on_edit"})

#: ``<path under app>::<function>`` -> why it need not record a source.
_ALLOWED: dict[str, str] = {
    "services/speaker_profile_rename.py::apply_profile_name_to_speakers": (
        "renaming a PROFILE rewrites the name of the speakers linked to it; it neither confirms a "
        "machine link nor makes a person's name the machine's, so each speaker keeps its source"
    ),
    "services/ingest_artifacts/service.py::__init__": (
        "not a Speaker: a plain roster entry object holding a label for the facts digest"
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


def _records_source(func: ast.AST, helper_names: set[str]) -> bool:
    for node in ast.walk(func):
        if isinstance(node, ast.Call):
            callee = node.func
            name = callee.id if isinstance(callee, ast.Name) else getattr(callee, "attr", None)
            if name in helper_names or name in _WRAPPERS:
                return True
        if isinstance(node, ast.Assign):
            if any(
                isinstance(t, ast.Attribute) and t.attr == "identity_source" for t in node.targets
            ):
                return True
        if isinstance(node, ast.keyword) and node.arg == "identity_source":
            return True
    return False


def unmarked_writers(source: str) -> list[tuple[str, int, str]]:
    """``(function, line, field)`` for each writer in ``source`` that records no source."""
    tree = ast.parse(source)
    helper_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == HELPER_MODULE:
            helper_names.update(a.asname or a.name for a in node.names)
    out: list[tuple[str, int, str]] = []
    for func in ast.walk(tree):
        if not isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        # A nested function is judged on its own; its parent is judged without it.
        for line, field in _writes(func):
            if not _records_source(func, helper_names):
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
            if isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef) and _writes(func):
                seen_writers.add(f"{rel}::{func.name}")
        for name, line, field in unmarked_writers(source):
            flagged.setdefault(f"{rel}::{name}", []).append((line, field))
    return flagged, seen_writers


def test_every_writer_of_a_speakers_name_or_link_records_its_source():
    flagged, _ = _scan()
    missing = {k: v for k, v in flagged.items() if k not in _ALLOWED}
    assert not missing, (
        "These functions assign Speaker.display_name / profile_id without calling a helper from "
        f"{HELPER_MODULE} or setting identity_source (line, field): {missing}. Record who wrote "
        "it, or add the function to _ALLOWED with the reason it needs no source."
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
        "    speaker.display_name = name\n    record_human_identity(speaker)\n",
        "    speaker.display_name = name\n    speaker.identity_source = 'human'\n",
        "    speaker.profile_id = 1\n    after_unlink(speaker)\n",
    ],
)
def test_detector_stays_clean_on_a_marked_one(body):
    source = (
        f"from {HELPER_MODULE} import record_human_identity, after_unlink\n"
        f"def write(speaker, name):\n{body}"
    )
    assert unmarked_writers(source) == []
