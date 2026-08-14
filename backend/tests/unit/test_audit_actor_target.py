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
#: one account acting on another. These are the records an auditor reads to
#: answer "who did this to whom", so both halves must be present.
_ADMINISTRATIVE_EVENTS = {
    "ADMIN_ROLE_CHANGE",
    "AUTH_ACCOUNT_DISABLED",
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
    """
    offenders = []
    checked = 0
    for path, call in _audit_log_calls():
        name = _event_type_name(call)
        if name not in _ADMINISTRATIVE_EVENTS:
            continue
        checked += 1
        passed = {kw.arg for kw in call.keywords}
        if "target_user_id" not in passed and "target_username" not in passed:
            offenders.append(f"{path.relative_to(_APP)}:{call.lineno} emits {name}")

    assert checked >= 4, (
        f"only {checked} administrative emitter(s) found — the AST walk or the "
        f"event-name set is wrong, so this test is guarding almost nothing."
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
