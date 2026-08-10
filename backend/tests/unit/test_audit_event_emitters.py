"""Every declared ``AuditEventType`` member must actually be emitted somewhere.

The dead-surface shape this closes: an event type declared in the enum, never
passed to ``audit_logger.log(...)`` anywhere in the app. It reads as a documented
compliance control ("we log X") while nothing produces X — an auditor, or an
incident responder, searching the audit index for it finds nothing, forever.
``AUTH_SESSION_LIMIT_EXCEEDED`` and ``ADMIN_USER_DELETE`` were exactly this shape
before being wired; this is the guard so a member never regresses to it silently.

Why AST and not grep
---------------------
A grep for the member name matches its own declaration line in ``audit.py``, a
docstring reference (this file has several), a comment. An emitter has to be a
real ``AuditEventType.<MEMBER>`` attribute access — the class-body declaration
(``AUTH_LOGIN_SUCCESS = "..."``) is an assignment to a bare name, not an attribute
access, so it can never satisfy this by itself, even though ``auth/audit.py`` is
scanned like every other module (its own convenience wrappers —
``log_login_success``, ``log_token_refresh``, ...— are legitimate emitters that
happen to live there). Same reasoning ``test_auth_config_has_readers.py`` uses for
config keys and ``test_capability_contract.py`` uses for capability strings.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.auth.audit import AuditEventType

_APP_ROOT = Path(__file__).resolve().parents[2] / "app"


def _declared_members() -> set[str]:
    return {member.name for member in AuditEventType}


def _scan_emitters() -> dict[str, set[str]]:
    """Map event-type member name -> set of module paths (repo-relative) that
    reference ``AuditEventType.<member>`` as a value.

    ``auth/audit.py`` is scanned too, not excluded: the enum's own convenience
    wrappers (``log_login_success``, ``log_token_refresh``, ...) are legitimate
    emitters that pass the member to ``self.log(...)`` from inside that module.
    Only the class-body *declaration* (``AUTH_LOGIN_SUCCESS = "..."``) is immune
    to matching here — it is an ``ast.Assign`` to a bare name, never an
    ``AuditEventType.<member>`` attribute access, so it cannot be mistaken for
    an emission regardless of which file it is in.
    """
    emitters: dict[str, set[str]] = {}
    declared = _declared_members()

    for path in sorted(_APP_ROOT.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - a syntax error fails elsewhere
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute) or node.attr not in declared:
                continue
            # Require the receiver to actually be the enum name — an
            # attribute named e.g. AUTH_LOGOUT on an unrelated object must not
            # count as an emission.
            if isinstance(node.value, ast.Name) and node.value.id == "AuditEventType":
                emitters.setdefault(node.attr, set()).add(str(path.relative_to(_APP_ROOT.parent)))

    return emitters


_EMITTERS = _scan_emitters()
_ALL_MEMBERS = sorted(_declared_members())


@pytest.mark.parametrize("member", _ALL_MEMBERS)
def test_event_type_has_an_emitter(member: str) -> None:
    assert member in _EMITTERS, (
        f"AuditEventType.{member} is declared but never referenced as "
        "AuditEventType.<member> anywhere in the app — nothing emits it. Wire a "
        "real audit_logger.log(...) call site, or remove the member if the event "
        "no longer applies."
    )


def test_scanner_finds_a_known_emitter() -> None:
    """Guard the guard: a scanner that silently matches nothing passes everything."""
    assert "AUTH_LOGIN_SUCCESS" in _EMITTERS, (
        "AST scan found no emitter for a member known to be wired"
    )
    assert any("app/api/endpoints" in p or "app/auth" in p for p in _EMITTERS["AUTH_LOGIN_SUCCESS"])
    assert _ALL_MEMBERS, "AuditEventType declares no members — scan is blind"
