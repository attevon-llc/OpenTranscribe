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
#: The profile route modules (``speaker_profiles.py`` and the routers split out of it).
PROFILE_ROUTES = frozenset((APP / "api" / "endpoints").glob("speaker_profiles*.py"))
_TOUCH = "touch_profile_interaction"
#: Helpers that stamp inside their own commit, so a caller that goes through one is stamped.
#: ``swap_avatar_path`` is pinned to that by ``test_speaker_profile_activity.py``.
_STAMPING_HELPERS = frozenset({_TOUCH, "swap_avatar_path"})
_MUTATING = {"post", "put", "patch", "delete"}

#: ``<path relative to app>::<function>`` -> why it does not stamp.
EXEMPT: dict[str, str] = {
    "services/speaker_identity_moves.py::unlink_from_profile": (
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


def _referenced_callables(fn: ast.AST) -> set[str]:
    """Names *fn* calls, plus bare names it hands to a call as an argument.

    The second shape is an off-loaded call: ``await run_in_threadpool(_helper, db, ...)``
    runs ``_helper`` without a ``Call`` node naming it.
    """
    out: set[str] = set()
    for node in _own_nodes(fn):
        if isinstance(node, ast.Call):
            f = node.func
            name = f.id if isinstance(f, ast.Name) else getattr(f, "attr", None)
            if name:
                out.add(name)
            out.update(a.id for a in node.args if isinstance(a, ast.Name))
    return out


def _local_stampers(tree: ast.AST) -> frozenset[str]:
    """Stamping helpers plus every function of this module that reaches one (transitively)."""
    refs = {fn.name: _referenced_callables(fn) for fn in _functions(tree)}
    stampers = set(_STAMPING_HELPERS)
    changed = True
    while changed:
        changed = False
        for name, called in refs.items():
            if name not in stampers and called & stampers:
                stampers.add(name)
                changed = True
    return frozenset(stampers)


def _touches(fn: ast.AST, stampers: frozenset[str] = _STAMPING_HELPERS) -> bool:
    return bool(_referenced_callables(fn) & stampers)


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
    tree = ast.parse(source)
    stampers = _local_stampers(tree)
    for fn in _functions(tree):
        key = f"{rel}::{fn.name}"
        needs = _assigns_profile_id(fn) or (routes and _is_profile_edit_route(fn))
        if needs and not _touches(fn, stampers) and key not in EXEMPT:
            out.append(key)
    return out


def _scan() -> tuple[list[str], set[str]]:
    violations: list[str] = []
    sites: set[str] = set()
    for path in sorted(APP.rglob("*.py")):
        rel = path.relative_to(APP).as_posix()
        src = path.read_text()
        tree = ast.parse(src)
        routes = path in PROFILE_ROUTES
        for fn in _functions(tree):
            if _assigns_profile_id(fn) or (routes and _is_profile_edit_route(fn)):
                sites.add(f"{rel}::{fn.name}")
        violations += find_violations(src, rel, routes=routes)
    return violations, sites


def test_every_interaction_site_stamps_the_profile():
    violations, sites = _scan()
    # The scan must find the known sites, or it is matching nothing.
    assert "api/endpoints/speaker_profiles_attributes.py::confirm_profile_gender" in sites
    assert "services/speaker_matching/profiles.py::assign_speaker_to_profile" in sites
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

@router.post("/profiles/{profile_uuid}/avatar")
def upload(db, profile):
    previous = swap_avatar_path(db, profile.id, "key")

def outer(speaker):
    def inner(db):
        touch_profile_interaction(db, 1, "human_link")
    return inner
"""


def test_selftest_must_fire():
    assert find_violations(_MUST_FIRE, "x.py", routes=True) == ["x.py::link", "x.py::rename"]


def test_selftest_must_stay_clean():
    assert find_violations(_MUST_STAY_CLEAN, "x.py", routes=True) == []


_OFFLOADED = """
@router.post("/profiles/{profile_uuid}/avatar")
async def upload(db, profile):
    previous = await run_in_threadpool(_swap_or_rollback, db, profile.id, "key")

def _swap_or_rollback(db, profile_id, key):
    return swap_avatar_path(db, profile_id, key)

@router.put("/profiles/{profile_uuid}")
async def rename(db, profile):
    await run_in_threadpool(_rename, db, profile)

def _rename(db, profile):
    profile.name = "x"
"""


def test_selftest_a_stamp_through_an_offloaded_helper_counts_and_a_missing_one_fires():
    assert find_violations(_OFFLOADED, "x.py", routes=True) == ["x.py::rename"]


def test_selftest_a_nested_touch_does_not_cover_its_parent():
    src = """
def outer(db, speaker):
    speaker.profile_id = 1
    def later():
        touch_profile_interaction(db, 1, "human_link")
"""
    assert find_violations(src, "x.py", routes=False) == ["x.py::outer"]
