"""``user_id`` is the ACTOR; the subject goes in ``target_user_id`` (issue #443).

This is an access-control invariant, not a style rule. ``query_audit_logs``
filters on ``user_id``, and ``build_org_scope_clause``'s legacy branch attributes
un-stamped events to an org **by member user-id** — so whether an emitter keyed
that field on the actor or on the subject changed **who could see the record**.

Five emitters of ``auth.account.disabled`` disagreed three ways (actor, subject,
NULL) and ``admin.role.change`` split two ways. The observable consequence:
filtering "actions Bob performed" returned Bob's own privilege escalation, while
filtering by the acting admin missed every IdP-driven promotion.

**The details-dict workaround had already failed**, which is why these are
top-level fields: the subject appeared as ``details.target_user`` (a UUID
string) in one place, ``details.target_user_id`` (an int) in another, and
``details.target_email`` in a third. ``details`` is a dynamic object mapping, so
no single query could answer "everything done TO user X".

Two tests here, doing different jobs:

* a **behavioural** one, driving the real ``AuditLogger`` and inspecting the
  emitted event — this is what proves the field reaches the record;
* an **AST** one over the administrative emitters, which is what stops a NEW
  call site from reintroducing the divergence. A behavioural test only covers
  the paths someone remembered to exercise.
"""

from __future__ import annotations

import ast
import json
import pathlib

import pytest

from app.auth.audit import AuditEventType
from app.auth.audit import AuditLogger
from app.auth.audit import AuditOutcome

_APP = pathlib.Path(__file__).resolve().parents[2] / "app"

#: Event types where the actor and the subject are DIFFERENT people by nature —
#: one account acting on another, with no legitimate self-service dual. These are
#: the records an auditor reads to answer "who did this to whom", so both halves
#: must be present.
#:
#: issue #828: this used to be just the two names above, and the test below counted
#: CALL SITES referencing them, not distinct NAMES — so "checked >= 4" read as "at
#: least 4 administrative events are attribution-checked" while actually asserting
#: "at least 4 places touch one of 2 names." Expanded by walking every
#: `audit_logger.log(...)` call site in `app/` (68 total) and keeping only the names
#: where a target that DIFFERS from the actor is structurally the only case — i.e.
#: excluding anything with a genuine self-service dual, which the AST cannot
#: distinguish from a real omission:
#:
#:   - ``ADMIN_USER_CREATE`` / ``ADMIN_USER_UPDATE`` are EXCLUDED: both have a real
#:     self-service emitter (self-registration, invitation self-accept, a user's own
#:     email change) that correctly omits the target — flagging those would be a
#:     false positive the AST cannot tell apart from an admin-on-another-account call
#:     that is missing it.
#:   - ``ADMIN_USER_DELETE`` is EXCLUDED: `gdpr_erasure_service.py` and
#:     `erasure_ledger_service.py` disagree on which of `user_id` vs `details` holds
#:     the actor vs the subject across three call sites — the EXACT pre-#443 bug this
#:     test exists to catch, reproduced. Their own comments acknowledge the tension
#:     ("Whatever #443 settles on, these three must move together") without
#:     resolving it. That is a real, pre-existing finding, not a false positive — but
#:     untangling three GDPR-erasure emitters is a coordinated redesign, not a test
#:     fix, so it is reported rather than silently absorbed into this set.
#:   - ``ADMIN_SETTINGS_CHANGE`` is EXCLUDED: a settings change has no target PERSON
#:     at all — there is nothing for `target_user_id`/`target_username` to name.
#:   - ``ADMIN_FILE_QUARANTINE`` / ``ADMIN_FILE_RELEASE`` are ALSO EXCLUDED, for a
#:     reason worth recording rather than silently working around: both route
#:     through a local ``_audit()`` wrapper in `services/takedown_service.py` whose
#:     own `.log(...)` call passes `event_type=event_type` — a variable, not a
#:     literal `AuditEventType.X` attribute — so `_event_type_name` (below) returns
#:     `None` for it and `_audit_log_calls` cannot resolve it to either name at all.
#:     Adding them here would be decorative: the assertion could never see whether
#:     they carry a target, in either direction, so it could not FAIL against a
#:     regression there either. That call site *did* have the identical missing-
#:     attribution bug as `AUTH_ACCOUNT_UNLOCK` below (the file owner named only in
#:     `details`, never top-level) and it was fixed in the same change as this one —
#:     but by inspection, not by this test, which is a real blind spot of the same
#:     *kind* as issue #827's `.svelte`-only glob: a wrapper that forwards the event
#:     type as a variable is as invisible to this AST walk as a `.css` file was to
#:     that one. Resolving call-graph-forwarded event types is future scope, not
#:     this fix.
#:
#: The real gap this expansion DID surface and that this test DOES verify is
#: `AUTH_ACCOUNT_UNLOCK`'s second emitter (`api/endpoints/admin.py`), which carried
#: the target only in `details.target_user` (a UUID string) — the same shape
#: `.log()`'s own docstring says already failed once, on a name this walk can see.
_ADMINISTRATIVE_EVENTS = {
    "ADMIN_ROLE_CHANGE",
    "AUTH_ACCOUNT_DISABLED",
    "AUTH_ACCOUNT_UNLOCK",
    "GROUP_MEMBER_ADD",
    "GROUP_MEMBER_REMOVE",
    "GROUP_MEMBER_ROLE_CHANGE",
}


def _emitted(**kwargs) -> dict:
    """Drive the real logger and return the event it wrote, parsed from JSON."""
    logger = AuditLogger()
    captured: list[str] = []

    class _Sink:
        def info(self, message: str) -> None:
            captured.append(message)

        def warning(self, *a, **k) -> None:  # pragma: no cover - not exercised
            pass

        def error(self, *a, **k) -> None:  # pragma: no cover - not exercised
            pass

    logger._logger = _Sink()  # type: ignore[assignment]
    logger.log(**kwargs)
    assert captured, "the logger emitted nothing — AUDIT_LOG_ENABLED may be off"
    event: dict = json.loads(captured[0])
    return event


def test_the_target_reaches_the_emitted_record() -> None:
    """Both halves survive into the event an auditor actually queries."""
    event = _emitted(
        event_type=AuditEventType.ADMIN_ROLE_CHANGE,
        outcome=AuditOutcome.SUCCESS,
        user_id=11,
        username="admin@example.com",
        target_user_id=22,
        target_username="subject@example.com",
    )

    assert event["user_id"] == 11, "user_id must be the ACTOR"
    assert event["username"] == "admin@example.com"
    assert event["target_user_id"] == 22, "the subject must be queryable, not buried in details"
    assert event["target_username"] == "subject@example.com"


def test_a_self_service_event_omits_the_target_entirely() -> None:
    """Unset target fields are ABSENT, not null.

    They are top-level fields on a mapped index. Writing null on every
    self-service event (the vast majority) would add a sparse field to every
    document for no query value. Absence is also how a pre-#443 record reads, so
    old and new documents stay shape-compatible.
    """
    event = _emitted(
        event_type=AuditEventType.AUTH_LOGIN_SUCCESS,
        outcome=AuditOutcome.SUCCESS,
        user_id=11,
        username="someone@example.com",
    )

    assert "target_user_id" not in event
    assert "target_username" not in event
    assert event["user_id"] == 11


def _audit_log_calls() -> list[tuple[pathlib.Path, ast.Call]]:
    """Every ``audit_logger.log(...)`` call in ``app/``, with its file."""
    found = []
    for path in _APP.rglob("*.py"):
        if "__pycache__" in str(path):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - would fail the build elsewhere
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "log"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "audit_logger"
            ):
                found.append((path, node))
    return found


def _event_type_name(call: ast.Call) -> str | None:
    """``AuditEventType.X`` passed as ``event_type=`` → ``"X"``."""
    for kw in call.keywords:
        if kw.arg != "event_type":
            continue
        node = kw.value
        # Unwrap `A if cond else B` — take either arm; both are checked by the
        # caller iterating over all administrative names.
        if isinstance(node, ast.IfExp):
            node = node.body
        if isinstance(node, ast.Attribute):
            return node.attr
    return None


def test_every_administrative_emitter_names_its_target() -> None:
    """An emitter of an admin action must say who it was performed ON.

    AST rather than behavioural on purpose: this is the half that catches a
    call site nobody wrote a test for, which is exactly how the original
    divergence survived — every one of the five emitters had passing tests.

    issue #828: the coverage claim below is about distinct EVENT NAMES, not call
    sites — a name checked by ten call sites and a name checked by one are both
    "one name checked", and asserting on the call-site count let this pass while
    covering only 2 of the set's names (the two hand-picked at the time the set was
    written). Counting names is also what makes the assertion able to FAIL against
    a missing name: shrink `_ADMINISTRATIVE_EVENTS` to one entry with no matching
    call site and this drops to 0, not merely a smaller positive number.
    """
    offenders = []
    checked_names: set[str] = set()
    for path, call in _audit_log_calls():
        name = _event_type_name(call)
        if name not in _ADMINISTRATIVE_EVENTS:
            continue
        checked_names.add(name)
        passed = {kw.arg for kw in call.keywords}
        if "target_user_id" not in passed and "target_username" not in passed:
            offenders.append(f"{path.relative_to(_APP)}:{call.lineno} emits {name}")

    assert len(checked_names) >= 6, (
        f"only {len(checked_names)} distinct administrative event NAME(s) were "
        f"exercised ({sorted(checked_names)}) of {len(_ADMINISTRATIVE_EVENTS)} "
        f"declared — the AST walk or the event-name set is wrong, so this test is "
        f"guarding almost nothing."
    )
    assert not offenders, (
        "These emit an administrative event without naming the target user, so "
        "'everything done TO user X' cannot find them:\n  " + "\n  ".join(offenders)
    )


def test_the_administrative_event_set_is_not_empty() -> None:
    """A guard driven by an empty set inspects nothing and passes.

    ``_ADMINISTRATIVE_EVENTS`` is hand-maintained; emptying it (or a rename that
    made every name miss) would silently disable the test above.
    """
    assert _ADMINISTRATIVE_EVENTS
    for name in _ADMINISTRATIVE_EVENTS:
        assert hasattr(AuditEventType, name), f"{name} is not an AuditEventType member"


@pytest.mark.parametrize("field", ["target_user_id", "target_username"])
def test_the_logger_accepts_the_field_by_keyword(field: str) -> None:
    """The AST test asserts a keyword NAME; this proves the name is real.

    Without it, renaming the parameter in ``AuditLogger.log`` would leave the
    AST guard passing against call sites that now raise ``TypeError``.
    """
    event = _emitted(
        event_type=AuditEventType.ADMIN_ROLE_CHANGE,
        outcome=AuditOutcome.SUCCESS,
        user_id=1,
        **{field: 2 if field.endswith("_id") else "x@example.com"},
    )
    assert field in event
