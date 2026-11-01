"""Every site that changes a voiceprint's link to a person stamps ``last_interaction_at``.

``speaker_profile.last_interaction_at`` (v450) is the clock a voiceprint retention schedule
counts from, so a link written without stamping it lets a voiceprint that is in use expire.
Two shapes are scanned in ``backend/app``:

* a function that assigns ``<obj>.profile_id = ...`` (a speaker linked or unlinked), and
* a mutating route on ``/profiles/{profile_uuid}...`` in ``speaker_profiles.py`` (a person
  editing the profile).

Each must call ``touch_profile_interaction`` itself, or be listed in ``EXEMPT`` with a reason.
The definition of an interaction is in ``app/services/speaker_profile_activity.py``.
"""

from __future__ import annotations

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"
PROFILE_ROUTES = APP / "api" / "endpoints" / "speaker_profiles.py"
_TOUCH = "touch_profile_interaction"
_MUTATING = {"post", "put", "patch", "delete"}

#: ``<path relative to app>::<function>`` -> why it does not stamp.
EXEMPT: dict[str, str] = {
    "services/speaker_cleanup.py::unlink_from_profile": (
        "shared helper: profile delete (the profile is gone), Re-cluster taking back a link the "
        "user already unassigned (maintenance), and human callers that stamp themselves "
        "(detach_from_cluster, the verify reject path)"
    ),
    "api/endpoints/speaker_profiles.py::delete_speaker_profile": (
        "the profile row is deleted; there is nothing left to stamp"
    ),
}


def _functions(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            yield node


def _own_nodes(fn: ast.AST):
    """Nodes of *fn* excluding nested function bodies (they are scanned on their own)."""
    stack = list(ast.iter_child_nodes(fn))
    while stack:
        node = stack.pop()
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))


def _assigns_profile_id(fn: ast.AST) -> bool:
    for node in _own_nodes(fn):
        targets = node.targets if isinstance(node, ast.Assign) else []
        if isinstance(node, ast.AnnAssign | ast.AugAssign):
            targets = [node.target]
        for t in targets:
            if isinstance(t, ast.Attribute) and t.attr == "profile_id":
                return True
    return False


def _touches(fn: ast.AST) -> bool:
    for node in _own_nodes(fn):
        if isinstance(node, ast.Call):
            f = node.func
            name = f.id if isinstance(f, ast.Name) else getattr(f, "attr", None)
            if name == _TOUCH:
                return True
    return False


def _is_profile_edit_route(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for dec in fn.decorator_list:
        if (
            isinstance(dec, ast.Call)
            and isinstance(dec.func, ast.Attribute)
            and dec.func.attr in _MUTATING
            and dec.args
            and isinstance(dec.args[0], ast.Constant)
            and str(dec.args[0].value).startswith("/profiles/{")
        ):
            return True
    return False


def find_violations(source: str, rel: str, *, routes: bool) -> list[str]:
    """``rel::function`` for every scanned site that neither stamps nor is exempt."""
    out = []
    for fn in _functions(ast.parse(source)):
        key = f"{rel}::{fn.name}"
        needs = _assigns_profile_id(fn) or (routes and _is_profile_edit_route(fn))
        if needs and not _touches(fn) and key not in EXEMPT:
            out.append(key)
    return out


def _scan() -> tuple[list[str], set[str]]:
    violations: list[str] = []
    sites: set[str] = set()
    for path in sorted(APP.rglob("*.py")):
        rel = path.relative_to(APP).as_posix()
        src = path.read_text()
        tree = ast.parse(src)
        routes = path == PROFILE_ROUTES
        for fn in _functions(tree):
            if _assigns_profile_id(fn) or (routes and _is_profile_edit_route(fn)):
                sites.add(f"{rel}::{fn.name}")
        violations += find_violations(src, rel, routes=routes)
    return violations, sites


def test_every_interaction_site_stamps_the_profile():
    violations, sites = _scan()
    # The scan must find the known sites, or it is matching nothing.
    assert "api/endpoints/speaker_profiles.py::confirm_profile_gender" in sites
    assert "services/speaker_matching_service.py::assign_speaker_to_profile" in sites
    assert len(sites) >= 15
    assert violations == [], (
        "These functions write a speaker's profile link or edit a profile without calling "
        f"{_TOUCH}: {violations}. Stamp the profile, or add an EXEMPT entry with a reason."
    )


def test_every_exemption_is_still_a_site():
    _, sites = _scan()
    stale = sorted(set(EXEMPT) - sites)
    assert stale == [], f"EXEMPT names functions that no longer write a link: {stale}"


_MUST_FIRE = """
def link(speaker, profile):
    speaker.profile_id = profile.id

@router.put("/profiles/{profile_uuid}")
def rename(profile):
    profile.name = "x"
"""

_MUST_STAY_CLEAN = """
def link(db, speaker, profile):
    speaker.profile_id = profile.id
    touch_profile_interaction(db, profile.id, "human_link")

@router.put("/profiles/{profile_uuid}")
def rename(db, profile):
    profile.name = "x"
    activity.touch_profile_interaction(db, profile.id, "human_edit")

@router.get("/profiles/{profile_uuid}")
def view(profile):
    return profile

def outer(speaker):
    def inner(db):
        touch_profile_interaction(db, 1, "human_link")
    return inner
"""


def test_selftest_must_fire():
    assert find_violations(_MUST_FIRE, "x.py", routes=True) == ["x.py::link", "x.py::rename"]


def test_selftest_must_stay_clean():
    assert find_violations(_MUST_STAY_CLEAN, "x.py", routes=True) == []


def test_selftest_a_nested_touch_does_not_cover_its_parent():
    src = """
def outer(db, speaker):
    speaker.profile_id = 1
    def later():
        touch_profile_interaction(db, 1, "human_link")
"""
    assert find_violations(src, "x.py", routes=False) == ["x.py::outer"]
