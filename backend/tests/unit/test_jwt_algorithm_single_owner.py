"""There is exactly ONE place that decides which algorithm signs a token, and ONE
that decides which are accepted. This fails if either decision reappears elsewhere.

Modelled on ``tests/unit/test_oidc_naming_invariant.py``: a closed allow-list, every
entry carrying a written reason, a hard failure for anything else, and a stale-entry
check so the list cannot quietly accumulate permission.

Why a static guard on top of the behavioural one. ``test_jwt_issuer_verifier_
agreement.py`` proves the issuers and verifiers *agree today*. It cannot prove there
are not five copies of the decision that happen to agree — which is precisely the
state this repo was in:

* ``token_service.create_refresh_token`` gated on ``FIPS_VERSION`` alone (so every
  deployment signed refresh tokens HS512),
* ``token_service.create_token`` had the same branch inline with a different gate,
* ``token_service.token_needs_upgrade`` had a third,
* ``core.security.verify_token`` had its own accept list,
* the two request-path verifiers hardcoded a fourth.

Four of the five were individually defensible. The defect was that there were five.

Scope: **Python source under ``backend/app/``**. Tests are excluded — a test that
pins a literal algorithm name is doing its job.
"""

from __future__ import annotations

import ast
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[2] / "app"

#: The functions that own each question. Everything else must call them.
SIGNING_OWNER = "signing_algorithm"
ACCEPTANCE_OWNER = "accepted_algorithms"

#: ``core/security.py`` defines both owners; ``core/config.py`` declares the settings
#: they read. Neither is a second copy.
OWNER_FILES = frozenset({"core/security.py", "core/config.py"})

#: Settings that exist ONLY to answer "which JWT algorithm?". A module that reads one
#: of these is making the decision itself.
JWT_ALGORITHM_SETTINGS = frozenset({"JWT_ALGORITHM_V3", "FIPS_MIGRATION_MODE"})

#: The FIPS gate. ``settings.fips_140_3_active`` is the one supported reader; the raw
#: setting defaults to ``"140-3"`` on every deployment, so reading it alone is a
#: condition that is always true — the shape of the refresh-token defect.
FIPS_GATE_SETTING = "FIPS_VERSION"

#: Path (relative to ``backend/app``) -> why reading ``FIPS_VERSION`` directly is
#: legitimate there.
FIPS_GATE_ALLOWED: dict[str, str] = {
    "auth/mfa.py": (
        "FOLLOW-UP. The MFA backup-code HASHING profile, not the JWT question — a "
        "different decision that happens to read the same gate. It is already "
        "correctly written as `FIPS_MODE and FIPS_VERSION == '140-3'`, so it is not "
        "the always-true form this guard exists to catch; collapsing it onto "
        "settings.fips_140_3_active is a pure simplification with no behaviour change, "
        "deferred only because this change did not own that file."
    ),
}

#: Path -> why a hand-built ``algorithms=`` list is legitimate there. Every entry
#: below is HS256-only and internally consistent (it signs and verifies with the same
#: ``settings.JWT_ALGORITHM``), so none of them can disagree with itself — but each is
#: a copy of the decision, and the reason has to say what the residual risk is.
ALGORITHM_LIST_ALLOWED: dict[str, str] = {
    "auth/direct_auth.py": (
        "FOLLOW-UP. THE production access-token issuer (every login path imports it). "
        "It signs with settings.JWT_ALGORITHM, which is exactly what "
        "signing_algorithm() returns, so it agrees with every verifier today — and "
        "test_jwt_issuer_verifier_agreement.py mints from this very function in nine "
        "configurations, so a divergence fails a test rather than a deployment. It "
        "would drift only if signing_algorithm ever branched per token type."
    ),
    "api/endpoints/auth/mfa_tokens.py": (
        "FOLLOW-UP. Mints AND verifies the MFA half-token with the same "
        "settings.JWT_ALGORITHM, so the pair cannot disagree with itself. The issuer "
        "is covered by test_jwt_issuer_verifier_agreement.py. Residual gap: under "
        "FIPS_MIGRATION_MODE=compatible it accepts less than accepted_algorithms(), "
        "so a half-token minted seconds before an operator changed JWT_ALGORITHM is "
        "refused — bounded by the token's ~5 minute TTL."
    ),
    "api/endpoints/auth/mfa_enrollment.py": (
        "FOLLOW-UP. A peek-decode of a token that mfa_tokens.py minted, with the same "
        "hardcoded setting, so it matches its issuer. Same bounded residual gap as "
        "mfa_tokens.py above."
    ),
    "api/endpoints/auth/sessions.py": (
        "FOLLOW-UP, and the one with a real residual gap. The logout handler decodes "
        "purpose-agnostically ('logging out with a refresh token is still a logout') "
        "but with [settings.JWT_ALGORITHM] rather than accepted_algorithms(). While "
        "create_refresh_token signed HS512 this silently revoked NOTHING for that "
        "case; it works now that refresh tokens follow JWT_ALGORITHM, but a refresh "
        "token issued before the upgrade still fails to revoke on logout until it "
        "expires. Widening this to accepted_algorithms(TOKEN_TYPE_REFRESH) closes it."
    ),
    "auth/oidc/claims.py": (
        "Not this decision at all. These are the IDENTITY PROVIDER's signing "
        "algorithms for an inbound ID token, negotiated from the provider's discovery "
        "document and verified against its JWKS — asymmetric keys we do not hold and "
        "an algorithm set we do not choose. Routing it through signing_algorithm() "
        "would be a category error. Permanent exemption, not a follow-up."
    ),
}


def _python_sources() -> list[Path]:
    return sorted(p for p in APP_ROOT.rglob("*.py") if "__pycache__" not in p.parts)


# ── detectors (pure functions over source text, so they can be proven to fire) ──


def settings_attributes_read(source: str, names: frozenset[str] | set[str]) -> set[str]:
    """Return which of *names* appear as ``settings.<NAME>`` reads in *source*.

    AST-based on purpose: a docstring that mentions ``JWT_ALGORITHM_V3`` in prose is
    documentation, not a second implementation, and a text search cannot tell the
    difference. Several modules legitimately explain the decision they delegate.
    """
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Attribute)
            and node.attr in names
            and isinstance(node.value, ast.Name)
            and node.value.id == "settings"
        ):
            found.add(node.attr)
    return found


def _names_bound_to_owner(tree: ast.AST) -> set[str]:
    """Locals assigned from a call to :data:`SIGNING_OWNER`."""
    bound: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        func = node.value.func
        callee = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if callee != SIGNING_OWNER:
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                bound.add(target.id)
    return bound


def unowned_algorithm_lists(source: str) -> list[int]:
    """Return the line numbers of ``algorithms=`` arguments not sourced from an owner.

    Accepted shapes:

    * ``algorithms=accepted_algorithms(...)`` — the acceptance owner, directly.
    * ``algorithms=[name]`` / ``algorithms=name`` where *name* was assigned from
      ``signing_algorithm(...)`` in the same module. This is the encode shape: one
      variable feeds both the ``alg`` header and the algorithm list.
    """
    tree = ast.parse(source)
    bound = _names_bound_to_owner(tree)
    offenders: list[int] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.keyword) or node.arg != "algorithms":
            continue
        value = node.value

        if isinstance(value, ast.Call):
            func = value.func
            callee = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if callee == ACCEPTANCE_OWNER:
                continue

        candidate = value
        if isinstance(value, ast.List) and len(value.elts) == 1:
            candidate = value.elts[0]
        if isinstance(candidate, ast.Name) and candidate.id in bound:
            continue

        offenders.append(getattr(value, "lineno", 0))

    return offenders


# ── the guards ─────────────────────────────────────────────────────────────────


def test_only_the_owner_reads_the_jwt_algorithm_settings():
    """``JWT_ALGORITHM_V3`` and ``FIPS_MIGRATION_MODE`` answer one question each.

    Consequence prevented: a module deciding for itself what "FIPS mode" means for
    JWTs. That is how ``verify_token`` came to accept an algorithm no issuer produces
    while the HTTP verifiers accepted a different set entirely.
    """
    offenders: dict[str, set[str]] = {}
    for path in _python_sources():
        rel = path.relative_to(APP_ROOT).as_posix()
        if rel in OWNER_FILES:
            continue
        read = settings_attributes_read(path.read_text(encoding="utf-8"), JWT_ALGORITHM_SETTINGS)
        if read:
            offenders[rel] = read

    assert not offenders, (
        f"These modules decide the JWT algorithm for themselves: {offenders}. Call "
        f"core.security.{SIGNING_OWNER}() or core.security.{ACCEPTANCE_OWNER}() "
        "instead — a second copy of this decision is how a deployment ends up signing "
        "with an algorithm one of its own verifiers refuses."
    )


def test_the_fips_gate_is_read_through_the_settings_property():
    """``FIPS_VERSION`` alone is always true; ``settings.fips_140_3_active`` is the gate.

    Consequence prevented: exactly the live defect this guard was written after — a
    non-FIPS deployment running a FIPS-profile credential because a condition that
    reads only ``FIPS_VERSION`` can never be false.
    """
    offenders: list[str] = []
    for path in _python_sources():
        rel = path.relative_to(APP_ROOT).as_posix()
        if rel in OWNER_FILES or rel in FIPS_GATE_ALLOWED:
            continue
        if settings_attributes_read(path.read_text(encoding="utf-8"), {FIPS_GATE_SETTING}):
            offenders.append(rel)

    assert not offenders, (
        f"These modules read settings.{FIPS_GATE_SETTING} directly: {offenders}. It "
        "defaults to '140-3' on every deployment, so a gate that reads it alone is "
        "unconditionally true. Read settings.fips_140_3_active, or add an entry to "
        "FIPS_GATE_ALLOWED with a written reason."
    )


def test_every_algorithm_list_comes_from_the_owner():
    """No hand-built ``algorithms=`` list outside the allow-list.

    Consequence prevented: a verifier that narrows or widens acceptance without the
    issuers moving with it. Under FIPS strict, the two request-path verifiers and the
    WebSocket verifier disagreed exactly this way — HTTP worked, WebSocket handshakes
    were refused, and no test noticed because each half was self-consistent.
    """
    offenders: dict[str, list[int]] = {}
    for path in _python_sources():
        rel = path.relative_to(APP_ROOT).as_posix()
        if rel in OWNER_FILES or rel in ALGORITHM_LIST_ALLOWED:
            continue
        lines = unowned_algorithm_lists(path.read_text(encoding="utf-8"))
        if lines:
            offenders[rel] = lines

    assert not offenders, (
        f"Hand-built algorithm lists at {offenders}. Pass "
        f"core.security.{ACCEPTANCE_OWNER}(token_type) when decoding and a variable "
        f"from core.security.{SIGNING_OWNER}(token_type) when encoding, or add an "
        "entry to ALGORITHM_LIST_ALLOWED with a written reason."
    )


def test_the_request_path_verifiers_delegate():
    """The specific pair the WebSocket/HTTP split came from, named explicitly.

    ``test_every_algorithm_list_comes_from_the_owner`` above would also catch this —
    but only for as long as ``dependencies.py`` stays off the allow-list. This asserts
    the positive: those two decode sites call the acceptance owner. It is also what
    licenses ``test_jwt_issuer_verifier_agreement.py`` to reproduce the request-path
    decode rather than driving a live FastAPI dependency.
    """
    source = (APP_ROOT / "api/endpoints/auth/dependencies.py").read_text(encoding="utf-8")
    calls = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == ACCEPTANCE_OWNER
    ]

    assert len(calls) == 2, (
        f"dependencies.py calls {ACCEPTANCE_OWNER}() {len(calls)} times; expected 2 "
        "(get_current_user and get_optional_current_user). If a third verifier was "
        "added, update this count; if one stopped calling it, that verifier has "
        "forked the decision again."
    )


# ── guard the guard ────────────────────────────────────────────────────────────


def test_the_settings_detector_fires():
    """A detector that matches nothing reports zero findings, which is
    indistinguishable from a clean tree."""
    assert settings_attributes_read("x = settings.JWT_ALGORITHM_V3\n", JWT_ALGORITHM_SETTINGS) == {
        "JWT_ALGORITHM_V3"
    }
    assert settings_attributes_read("y = settings.FIPS_VERSION\n", {FIPS_GATE_SETTING}) == {
        "FIPS_VERSION"
    }


def test_the_settings_detector_ignores_prose():
    """Must-stay-clean: a docstring naming the setting is documentation."""
    source = '"""Delegates instead of reading settings.JWT_ALGORITHM_V3."""\nx = 1\n'

    assert settings_attributes_read(source, JWT_ALGORITHM_SETTINGS) == set()


def test_the_algorithm_list_detector_fires():
    assert unowned_algorithm_lists(
        "jwt.decode(token, key, algorithms=[settings.JWT_ALGORITHM])\n"
    ) == [1]
    assert unowned_algorithm_lists('jwt.decode(t, k, algorithms=["HS256", "HS512"])\n') == [1]


def test_the_algorithm_list_detector_accepts_the_owner():
    """Must-stay-clean, both sanctioned shapes."""
    assert (
        unowned_algorithm_lists(
            "jwt.decode(token, key, algorithms=accepted_algorithms(TOKEN_TYPE_ACCESS))\n"
        )
        == []
    )
    assert (
        unowned_algorithm_lists(
            "algorithm = signing_algorithm(TOKEN_TYPE_REFRESH)\n"
            'jwt.encode({"alg": algorithm}, data, key, algorithms=[algorithm])\n'
        )
        == []
    )


def test_the_algorithm_list_detector_rejects_a_lookalike_binding():
    """A local named ``algorithm`` that did NOT come from the owner is still a copy."""
    assert unowned_algorithm_lists(
        'algorithm = "HS512" if settings.FIPS_VERSION == "140-3" else "HS256"\n'
        'jwt.encode({"alg": algorithm}, data, key, algorithms=[algorithm])\n'
    ) == [2]


# ── allow-list hygiene ─────────────────────────────────────────────────────────


def test_every_allowed_file_still_needs_its_exemption():
    """A stale exemption is how an allow-list stops meaning anything."""
    stale: list[str] = []
    for rel in FIPS_GATE_ALLOWED:
        path = APP_ROOT / rel
        assert path.exists(), f"FIPS_GATE_ALLOWED names {rel}, which does not exist"
        if not settings_attributes_read(path.read_text(encoding="utf-8"), {FIPS_GATE_SETTING}):
            stale.append(f"FIPS_GATE_ALLOWED::{rel}")

    for rel in ALGORITHM_LIST_ALLOWED:
        path = APP_ROOT / rel
        assert path.exists(), f"ALGORITHM_LIST_ALLOWED names {rel}, which does not exist"
        if not unowned_algorithm_lists(path.read_text(encoding="utf-8")):
            stale.append(f"ALGORITHM_LIST_ALLOWED::{rel}")

    assert not stale, f"These exemptions are no longer needed and must be removed: {stale}"


def test_every_exemption_carries_a_reason():
    """Mirrors the KNOWN_PUBLIC convention in test_route_privilege_tiers.py."""
    entries = {f"FIPS_GATE_ALLOWED::{rel}": reason for rel, reason in FIPS_GATE_ALLOWED.items()} | {
        f"ALGORITHM_LIST_ALLOWED::{rel}": reason for rel, reason in ALGORITHM_LIST_ALLOWED.items()
    }

    # Outside the loop: an empty allow-list would run the body zero times and pass,
    # which is the shape scripts/audit-tests.py's `loop-only` detector exists for.
    assert len(entries) == len(FIPS_GATE_ALLOWED) + len(ALGORITHM_LIST_ALLOWED)
    assert entries, "no exemptions under test"

    unreasoned = {key for key, reason in entries.items() if len(reason) <= 80}

    assert not unreasoned, f"These exemptions need a real written reason: {sorted(unreasoned)}"


def test_the_deferred_exemptions_are_counted_not_forgotten():
    """``FOLLOW-UP`` entries are tracked work, not a clean tree.

    Same convention as ``scripts/audit-tests.py``'s ``BACKLOG`` reasons: a green gate
    must never read as "nothing left to do". These four modules each hold a copy of
    the algorithm decision and were outside the file ownership of the change that
    introduced this guard.
    """
    deferred = sorted(
        rel
        for entries in (FIPS_GATE_ALLOWED, ALGORITHM_LIST_ALLOWED)
        for rel, reason in entries.items()
        if reason.startswith("FOLLOW-UP")
    )

    assert deferred == [
        "api/endpoints/auth/mfa_enrollment.py",
        "api/endpoints/auth/mfa_tokens.py",
        "api/endpoints/auth/sessions.py",
        "auth/direct_auth.py",
        "auth/mfa.py",
    ], (
        f"The deferred set changed: {deferred}. Shrinking it is progress — update this "
        "list. Growing it means a new copy of the decision was accepted rather than "
        "delegated."
    )
