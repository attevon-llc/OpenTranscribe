"""JWT/bcrypt behaviour that no test asserted (issue #446, mutation survivors).

Written from the 95 surviving mutants of ``app/core/security.py`` (measured
2026-08-14, 79% coverage of the module by ``MODULE_TESTS[security]`` -- the lowest
coverage floor of the six mutated modules, per the handoff). The coverage pre-flight
distinguishes "never runs" from "runs but unchecked" only at the line level, and 57
of the 95 survivors sit in two functions -- ``_create_password_context`` (37) and
``needs_rehash_for_fips_v3`` (20) -- that ARE fully executed by the existing suite
(``pwd_context = _create_password_context()`` runs at import; ``test_fips_140_3.py``
calls ``needs_rehash_for_fips_v3`` directly); what was missing was ASSERTIONS on the
exact values, not EXECUTION of the lines. Registering this file moved coverage
79% -> 92% and the survivor count 95 -> **19** (measured; see
``scripts/mutation-baselines.tsv``).

This is the JWT-verification and password-hashing module. An inverted comparison or
a downgraded algorithm here is a direct authentication bypass, so -- per the handoff
-- classification below is biased hard toward ``real``, and every ``equivalent``
verdict was corrected by MEASURING rather than trusting the diff: **19 mutants
survived a first pass of tests written for 82 "real" + 13 "equivalent" (95 total),
not the 13 predicted** -- six ``_create_password_context`` scheme-name-casing
mutants (``_create_password_context__mutmut_15/17/19/40/42/44``, e.g.
``schemes=["pbkdf2_sha256", ...]`` -> ``["PBKDF2_SHA256", ...]``) were first
classified real on the assumption that ``ctx.schemes()`` would reflect the changed
string, and given a test asserting exact scheme-name equality. Re-measuring showed
they still survived; tracing the actual passlib behaviour (confirmed by direct
interpreter experiment, not the docs) showed ``CryptContext`` resolves scheme names
through its handler registry, which is **case-insensitive** -- ``ctx.schemes()``
returns the canonical lowercase name regardless of the casing passed in, so
``"PBKDF2_SHA256"`` and ``"pbkdf2_sha256"`` construct byte-identical contexts. This
is provably different from the OTHER string-mutation family in the same list
(``"XXpbkdf2_sha256XX"`` etc.), which is not a case variant of a registered handler
name at all and fails construction outright with ``KeyError`` -- confirmed real, and
already killed by this file's schemes-tuple assertion. The exact same
``ctx.schemes()``/``ctx.hash()`` assertions that correctly killed every OTHER
schemes-string mutant were kept as a correctness pin for these six (see
``TestCreatePasswordContextFipsMode``/``StandardMode`` -- they do not distinguish
these six from the real code, the same non-load-bearing-but-worth-keeping shape
``test_lockout_survivor_mutants.py`` documents for its own re-measured mutant). Final
split below is the MEASURED one: **76 real** (all killed by this file, re-confirmed
by the 95 -> 19 run) and **19 equivalent**.

Full triage of the 95:

* **76 real.** Five families:

  - **``_create_password_context``'s exact ``CryptContext`` configuration (29 of
    37 -- see the correction above for the other 6).** Every
    ``schemes=``/``default=``/``deprecated=``/``*__rounds=`` keyword
    to both branches' ``CryptContext(...)`` call was either untested or tested only
    by side effect (a hash happens to round-trip). Verified here by constructing
    the context with the real function and reading back ``.schemes()``,
    ``.default_scheme()``, ``.needs_update()`` on scheme-specific hashes, and the
    exact rounds/iterations embedded in a hash produced by each scheme -- not by
    reading the passlib object's repr, which does not expose per-scheme rounds.
  - **``needs_rehash_for_fips_v3`` (20 of 20).** The existing test
    (``test_fips_140_3.py::test_needs_rehash_for_fips_v3``) only ever reaches this
    function's FIRST early return (``pwd_context.needs_update(...)``), never its own
    parsing/threshold logic -- confirmed by tracing passlib's ``needs_update``,
    which independently flags ANY rounds mismatch (over OR under the configured
    value) as needing update, so a real, well-formed low-iteration hash is already
    caught before this function's own ``$``-split/threshold code ever runs. The
    only way to reach that code at all is a hash whose rounds exactly match the
    ambient context's OWN configured rounds while still falling short of the FIPS
    140-3 threshold -- which is precisely the scenario the function exists to
    answer (does an app running non-FIPS iteration counts have hashes that would
    already satisfy FIPS 140-3). Tested here by substituting a stub ``pwd_context``
    whose ``needs_update`` never fires, isolating the function's own logic from
    passlib's.
  - **``authenticate_user``'s ``allow_local_fallback`` argument reaching
    ``local_password_allowed`` (5 of 8).** The existing suite
    (``test_local_auth_policy.py``) tests ``local_password_allowed`` directly and
    that ``authenticate_user`` DELEGATES to it (by source inspection), but no test
    drove a real PKI/OIDC-typed, ``allow_local_fallback=True`` user through
    ``authenticate_user`` end to end -- so nothing distinguished the real per-user
    flag from a mutant that silently forces it to ``False`` (dropping the argument,
    wrapping ``bool(None)``, reading the wrong attribute name, or reading the flag
    off ``None`` instead of ``user``).
  - **``create_access_token``'s claims (3 of 4).** ``jti`` collapsing to the
    literal string ``"None"`` on every token (a fixed JTI breaks per-token
    revocation), and ``expires_delta`` being dropped or subtracted instead of
    added (an immortal-until-crash or already-expired token).
  - **``verify_token``'s response bodies and audit record (19 of 24).** Two
    ``raise HTTPException(...)`` sites share the identical ``detail``/``headers``
    literals; the existing suite happens to assert ``detail`` exactly at one site
    (``test_jwt_algorithm_downgrade.py``) and ``headers`` exactly at the OTHER
    (``test_verify_token_claims.py``), so each site's untested half survived. The
    FIPS legacy-algorithm-fallback audit call's ``event_type``/``outcome``/dict
    keys were asserted only for one field (``details["warning"]``,
    ``test_jwt_algorithm_downgrade.py``) -- the rest is a structured, queryable
    audit-classification record an operator's compliance tooling reads by field
    name, not free-text log output, so a renamed/dropped key or a dropped
    ``event_type``/``outcome`` is real, observable behaviour, not noise.

* **19 equivalent**, every one verified by reading the exact code path (or, for the
  passlib-specific ones, by direct interpreter experiments against the pinned
  ``passlib`` version) rather than guessed at:

  - **6 in ``_create_password_context`` are the case-only scheme-name mutants**
    described in the correction above (``mutmut_15/17/19`` FIPS branch,
    ``mutmut_40/42/44`` non-FIPS branch) -- ``CryptContext`` resolves scheme names
    case-insensitively, so ``"PBKDF2_SHA256"``/``"BCRYPT_SHA256"``/``"BCRYPT"``
    construct a context identical to the lowercase original in every observable way
    (``.schemes()``, ``.hash()``, ``.needs_update()``). The DIFFERENT string-mutation
    family in the same lists (mutmut IDs wrapping the literal in ``"XX...XX"``) is
    NOT equivalent -- that is not a case variant of any registered handler name and
    fails construction outright (confirmed: ``KeyError: no crypt handler found for
    algorithm: 'xxbcryptxx'``), which is why those mutants (``14/16/18`` etc.) are
    correctly in the 76 real and already killed.

  - **5 in ``verify_token`` collapse to the SAME already-proven fact**: the module's
    own ``signing_algorithm`` does ``del token_type; return settings.JWT_ALGORITHM``
    -- it discards its argument unconditionally -- and ``accepted_algorithms``'s
    only use of ITS argument is passing it straight to ``signing_algorithm``. So
    ``token_purpose = expected_type or TOKEN_TYPE_ACCESS`` -> ``None``/
    ``expected_type and TOKEN_TYPE_ACCESS``, and both ``accepted_algorithms(token_
    purpose)``/``signing_algorithm(token_purpose)`` -> ``(None)``, can never change
    either function's return value (4 mutants: ``mutmut_9/10/12/14``). This is the
    SAME fact ``tests/unit/test_dependencies_survivor_mutants.py`` independently
    measured for the identical functions -- re-derived here by reading, not
    re-assumed. A 5th (``mutmut_1``, ``token_algorithm = None`` -> ``""``) is
    equivalent for an unrelated reason: the only two places that variable is read
    are a truthiness check (``if is_fips_140_3 and token_algorithm and ...``) and,
    only once that check is true, a dict value -- and ``None``/``""`` are equally
    falsy, so whichever fallback survives (only reachable when the header-parsing
    ``contextlib.suppress(JoseError)`` actually fires) it is never read as a value,
    only tested for truthiness, where it behaves identically either way.
  - **2 in ``_create_password_context``** (``mutmut_9`` FIPS branch, ``mutmut_34``
    non-FIPS branch): both drop the ``default=`` keyword entirely rather than set it
    to ``None``. Verified empirically against the pinned passlib: omitting
    ``default`` makes ``CryptContext`` fall back to the FIRST scheme in the
    ``schemes=`` list as the default -- which is ``"pbkdf2_sha256"`` in the FIPS
    branch and ``"bcrypt_sha256"`` in the non-FIPS branch, i.e. exactly the value
    the explicit keyword sets in each branch. (Explicit ``default=None``, by
    contrast, is NOT equivalent -- passlib raises ``TypeError: default must be str,
    not None`` at construction, which is why THOSE mutants, e.g. ``mutmut_3``, are
    classified real: any test that merely constructs the context already kills
    them.)
  - **1 in ``accepted_algorithms``** (``mutmut_2``, ``signing_algorithm(token_type)``
    -> ``signing_algorithm(None)``): the same discarded-argument fact as above,
    read directly off this module's own ``signing_algorithm``.
  - **1 in ``create_access_token``** (``mutmut_9``,
    ``signing_algorithm(TOKEN_TYPE_ACCESS)`` -> ``signing_algorithm(None)``): the
    identical fact a third time, at the token-issuing call site.
  - **1 in ``_bcrypt_rounds``** (``mutmut_10``, ``os.environ.get("TESTING", "")``
    -> ``os.environ.get("TESTING", "XXXX")``): the default is only read when
    ``TESTING`` is genuinely unset, and the line immediately lower-cases it and
    compares to the literal ``"true"`` -- ``""`` and ``"xxxx"`` both compare
    unequal to ``"true"``, so ANY default string that does not itself lower-case to
    ``"true"`` produces the identical `False` outcome. (This is a narrower, related
    proof to the one the ``dependencies`` module's own baseline note records for a
    DIFFERENT ``TESTING``-default mutant with a ``None`` default -- that one is NOT
    equivalent, because ``None.lower()`` raises. The distinguishing fact here is
    that the mutated default is a valid, if wrong, string.)
  - **3 in ``authenticate_user``** (``mutmut_17/20/23`` -- the ``getattr(user,
    "allow_local_fallback", <default>)`` default changed to ``None``, dropped
    entirely, or ``True``): the same fact this module's OWN dependencies-pass
    equivalence class already established for 18 other ``getattr(user, "<mapped
    column>", DEFAULT)`` sites -- ``allow_local_fallback`` is a real, non-nullable
    SQLAlchemy-mapped ``Boolean`` column of ``User`` (``models/user.py``), and every
    call site in ``authenticate_user`` receives a genuine, persisted ``User`` row
    from ``db.query(User)...first()``. A mapped column is never ABSENT on a real
    instance (SQLAlchemy's instrumented attribute returns the column's actual
    value, ``None`` included, rather than raising), so the ``default`` argument to
    ``getattr`` is dead code regardless of its value.

No production bug was found in ``authenticate_user``, ``create_access_token``,
``accepted_algorithms``, or ``_bcrypt_rounds`` -- every real gap there is existing,
correct behaviour that lacked an assertion. ``needs_rehash_for_fips_v3`` is unusual:
it has no production caller at all (``grep`` finds only ``test_fips_140_3.py`` and
this file) -- it may be intended for a not-yet-built FIPS-readiness admin tool. Its
own behaviour, as written, is internally consistent (verified above), so this is
noted as a scope observation, not a defect.
"""

# mypy: disable-error-code="arg-type"
# This suite passes a stub `pwd_context` (an object with only `needs_update`, not a
# real `CryptContext`) to isolate `needs_rehash_for_fips_v3`'s own logic from
# passlib's -- the same reasoning as `test_dependencies_survivor_mutants.py`'s
# suppression for structural stand-ins.
from __future__ import annotations

import time
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi import HTTPException
from joserfc import jwt as _jwt
from joserfc.jwk import OctKey

from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.auth.constants import AUTH_TYPE_PKI
from app.auth.constants import TOKEN_TYPE_ACCESS
from app.auth.constants import TOKEN_TYPE_REFRESH
from app.core import security
from app.core.config import settings

PROBE_PASSWORD = "correct horse battery staple"  # noqa: S105 - test fixture literal, not a secret


# ── accepted_algorithms / _bcrypt_rounds: equivalence proofs, no test needed ─────
#
# See the module docstring: `accepted_algorithms`'s `signing_algorithm(token_type)`
# -> `signing_algorithm(None)` mutant, and `_bcrypt_rounds`'s `os.environ.get(
# "TESTING", "")` -> `os.environ.get("TESTING", "XXXX")` mutant, are both provably
# equivalent by reading the function bodies. Nothing to assert; a test that cannot
# fail is worse than no test.


# ── _create_password_context: every explicit CryptContext keyword ────────────────


class TestCreatePasswordContextFipsMode:
    """The ``FIPS_MODE`` branch: ``schemes``, ``default``, ``deprecated``, and every
    ``*__rounds``/``*__default_rounds`` keyword, each pinned to a value a mutant
    cannot reproduce by falling back to passlib's own built-in default.

    ``BCRYPT_DEFAULT_ROUNDS`` (12) happens to equal passlib's OWN built-in default
    for both ``bcrypt`` and ``bcrypt_sha256`` (verified:
    ``passlib.hash.bcrypt.default_rounds == passlib.hash.bcrypt_sha256.default_rounds
    == 12``), so asserting against the real constant would not distinguish a
    ``bcrypt_sha256__default_rounds=BCRYPT_DEFAULT_ROUNDS`` -> ``None`` mutant from
    the real code. Monkeypatching the module constant to a value passlib would never
    pick on its own (9) removes that coincidence.
    """

    def _ctx(self, monkeypatch, *, iterations: int = 600321, bcrypt_default_rounds: int = 9):
        monkeypatch.setattr(security.settings, "FIPS_MODE", True)
        monkeypatch.setattr(security, "_get_pbkdf2_iterations", lambda: iterations)
        monkeypatch.setattr(security, "BCRYPT_DEFAULT_ROUNDS", bcrypt_default_rounds)
        return security._create_password_context()

    def test_schemes_default_and_deprecation(self, monkeypatch):
        ctx = self._ctx(monkeypatch)

        assert ctx.schemes() == ("pbkdf2_sha256", "bcrypt_sha256", "bcrypt")
        assert ctx.default_scheme() == "pbkdf2_sha256"

        bcrypt_sha256_hash = ctx.hash(PROBE_PASSWORD, scheme="bcrypt_sha256")
        bcrypt_hash = ctx.hash(PROBE_PASSWORD, scheme="bcrypt")
        pbkdf2_hash = ctx.hash(PROBE_PASSWORD)  # the default scheme

        assert ctx.needs_update(bcrypt_sha256_hash) is True, "bcrypt_sha256 must be deprecated"
        assert ctx.needs_update(bcrypt_hash) is True, "plain bcrypt must be deprecated"
        assert ctx.needs_update(pbkdf2_hash) is False, "the default scheme is not deprecated"

    def test_every_rounds_kwarg_is_wired_to_its_own_source(self, monkeypatch):
        ctx = self._ctx(monkeypatch, iterations=600321, bcrypt_default_rounds=9)

        pbkdf2_hash = ctx.hash(PROBE_PASSWORD)
        assert int(pbkdf2_hash.split("$")[2]) == 600321

        bcrypt_sha256_hash = ctx.hash(PROBE_PASSWORD, scheme="bcrypt_sha256")
        rounds = int(bcrypt_sha256_hash.split("r=")[1].split("$")[0])
        assert rounds == 9

        bcrypt_hash = ctx.hash(PROBE_PASSWORD, scheme="bcrypt")
        assert int(bcrypt_hash.split("$")[2]) == 9


class TestCreatePasswordContextStandardMode:
    """The non-``FIPS_MODE`` branch, same shape. ``_bcrypt_rounds()`` is
    monkeypatched directly (rather than via ``TESTING``/``is_hardened``) so the
    sentinel rounds value is unambiguous regardless of the ambient test env.
    """

    def _ctx(self, monkeypatch, *, iterations: int = 210777, bcrypt_rounds: int = 7):
        monkeypatch.setattr(security.settings, "FIPS_MODE", False)
        monkeypatch.setattr(security, "_get_pbkdf2_iterations", lambda: iterations)
        monkeypatch.setattr(security, "_bcrypt_rounds", lambda: bcrypt_rounds)
        return security._create_password_context()

    def test_schemes_default_and_deprecation(self, monkeypatch):
        ctx = self._ctx(monkeypatch)

        assert ctx.schemes() == ("bcrypt_sha256", "bcrypt", "pbkdf2_sha256")
        assert ctx.default_scheme() == "bcrypt_sha256"

        bcrypt_hash = ctx.hash(PROBE_PASSWORD, scheme="bcrypt")
        bcrypt_sha256_hash = ctx.hash(PROBE_PASSWORD)  # the default scheme
        pbkdf2_hash = ctx.hash(PROBE_PASSWORD, scheme="pbkdf2_sha256")

        assert ctx.needs_update(bcrypt_hash) is True, "plain bcrypt must still be deprecated"
        assert ctx.needs_update(bcrypt_sha256_hash) is False, (
            "bcrypt_sha256 is the current default here, not deprecated"
        )
        assert ctx.needs_update(pbkdf2_hash) is False

    def test_every_rounds_kwarg_is_wired_to_its_own_source(self, monkeypatch):
        ctx = self._ctx(monkeypatch, iterations=210777, bcrypt_rounds=7)

        bcrypt_sha256_hash = ctx.hash(PROBE_PASSWORD)  # default scheme
        rounds = int(bcrypt_sha256_hash.split("r=")[1].split("$")[0])
        assert rounds == 7

        bcrypt_hash = ctx.hash(PROBE_PASSWORD, scheme="bcrypt")
        assert int(bcrypt_hash.split("$")[2]) == 7

        pbkdf2_hash = ctx.hash(PROBE_PASSWORD, scheme="pbkdf2_sha256")
        assert int(pbkdf2_hash.split("$")[2]) == 210777


# ── needs_rehash_for_fips_v3: isolate its OWN logic from pwd_context.needs_update ─


class _NeverNeedsUpdate:
    """A ``pwd_context`` stand-in whose ``needs_update`` never fires.

    ``needs_rehash_for_fips_v3``'s first line already delegates to
    ``pwd_context.needs_update()``, which -- verified against the real passlib
    context -- independently flags ANY pbkdf2_sha256 rounds mismatch (over or
    under its own configured value) as needing update, and raises outright on a
    structurally incomplete hash. That makes the function's OWN ``$``-split and
    threshold comparison unreachable through the real ``pwd_context`` for most
    synthetic inputs. Substituting this stand-in isolates the function's own
    parsing/threshold code, the same way the worked examples pull a mutant
    function out of ``backend/mutants/`` to test it standalone.
    """

    def needs_update(self, hashed_password: str) -> bool:  # noqa: ARG002
        return False


class TestNeedsRehashForFipsV3OwnLogic:
    def test_a_hash_matching_the_apps_own_policy_but_below_the_fips_threshold_needs_rehash(
        self, monkeypatch
    ):
        monkeypatch.setattr(security, "pwd_context", _NeverNeedsUpdate())
        monkeypatch.setattr(type(security.settings), "PBKDF2_ITERATIONS_V3", 600000)

        assert security.needs_rehash_for_fips_v3("$pbkdf2-sha256$210000$salt$digest") is True

    def test_a_hash_at_exactly_the_fips_threshold_does_not_need_rehash(self, monkeypatch):
        """``rounds < ...``, not ``<=``: equality already satisfies the requirement."""
        monkeypatch.setattr(security, "pwd_context", _NeverNeedsUpdate())
        monkeypatch.setattr(type(security.settings), "PBKDF2_ITERATIONS_V3", 600000)

        assert security.needs_rehash_for_fips_v3("$pbkdf2-sha256$600000$salt$digest") is False

    def test_a_three_field_hash_is_still_parsed_not_silently_skipped(self, monkeypatch):
        """``len(parts) >= 3``, not ``> 3``/``>= 4``: a hash with no salt/digest
        fields (3 elements after splitting on ``$``) sits exactly on that boundary.
        """
        monkeypatch.setattr(security, "pwd_context", _NeverNeedsUpdate())
        monkeypatch.setattr(type(security.settings), "PBKDF2_ITERATIONS_V3", 600000)

        assert security.needs_rehash_for_fips_v3("$pbkdf2-sha256$100000") is True

    def test_an_unparseable_rounds_field_fails_safe_to_needs_rehash(self, monkeypatch):
        """The ``except (ValueError, IndexError): return True`` fallback -- flipped
        to ``False`` by a mutant, which would treat a corrupted hash as compliant."""
        monkeypatch.setattr(security, "pwd_context", _NeverNeedsUpdate())

        assert security.needs_rehash_for_fips_v3("$pbkdf2-sha256$not-a-number$salt$digest") is True

    def test_a_hash_the_app_does_not_flag_and_that_is_not_pbkdf2_reports_no_fips_gap(
        self, monkeypatch
    ):
        """The trailing ``return False`` -- the only remaining exit -- flipped to
        ``True`` would make every non-pbkdf2, non-deprecated hash falsely need
        rehash."""
        monkeypatch.setattr(security, "pwd_context", _NeverNeedsUpdate())

        assert (
            security.needs_rehash_for_fips_v3("$bcrypt-sha256$v=2,t=2b,r=12$salt$digest") is False
        )


# ── authenticate_user: the allow_local_fallback flag actually reaching the check ─


class TestAuthenticateUserAllowLocalFallbackReachesTheCheck:
    """``bool(getattr(user, "allow_local_fallback", False))`` -- the per-user opt-in
    for PKI/OIDC accounts -- must be the REAL, current value, not silently forced to
    ``False`` (a dropped argument, ``bool(None)``, a wrong attribute name, or reading
    the attribute off ``None`` instead of ``user``).
    """

    def _user(self, db_session, *, allow_local_fallback: bool):
        from app.models.user import User

        user = User(
            uuid=uuid4(),
            email=f"pki-fallback-{uuid4().hex[:8]}@example.com",
            hashed_password=security.get_password_hash(PROBE_PASSWORD),
            role="user",
            auth_type=AUTH_TYPE_PKI,
            allow_local_fallback=allow_local_fallback,
            is_active=True,
        )
        db_session.add(user)
        db_session.flush()
        return user

    def test_the_real_per_user_flag_being_true_allows_local_auth(self, db_session):
        user = self._user(db_session, allow_local_fallback=True)

        result = security.authenticate_user(db_session, user.email, PROBE_PASSWORD)

        assert result is not None
        assert result.id == user.id

    def test_the_control_false_still_refuses(self, db_session):
        """Without this, a mutant forcing the flag to ``True`` unconditionally would
        pass the test above too."""
        user = self._user(db_session, allow_local_fallback=False)

        result = security.authenticate_user(db_session, user.email, PROBE_PASSWORD)

        assert result is None


# ── create_access_token: jti uniqueness and expires_delta ────────────────────────


def _decode_unverified_claims(token: str) -> dict:
    key = OctKey.import_key(settings.JWT_SECRET_KEY)
    algorithm = security.signing_algorithm(TOKEN_TYPE_ACCESS)
    return dict(_jwt.decode(token, key, algorithms=[algorithm]).claims)


class TestCreateAccessTokenClaims:
    def test_jti_is_unique_per_token_not_the_literal_string_none(self):
        """``str(uuid.uuid4())`` -> ``str(None)``: a fixed JTI on every token would
        make per-token revocation (or a JTI blacklist) unable to distinguish
        sessions -- revoking one revokes all of them at once."""
        claims_a = _decode_unverified_claims(security.create_access_token("subject-a"))
        claims_b = _decode_unverified_claims(security.create_access_token("subject-a"))

        assert claims_a["jti"] != "None"
        assert claims_a["jti"] != claims_b["jti"]

    def test_a_custom_expires_delta_actually_moves_the_expiry_forward(self):
        """``expire = now + expires_delta`` -- not dropped to ``None`` (which would
        crash ``jwt.encode`` on a non-numeric ``exp``) and not ``now - expires_delta``
        (which would mint an already-expired token)."""
        token = security.create_access_token("subject-b", expires_delta=timedelta(minutes=42))

        payload = security.verify_token(token)

        expected = time.time() + 42 * 60
        assert abs(payload["exp"] - expected) < 10  # generous slack for test wall-clock

    def test_a_positive_delta_is_not_immediately_expired(self):
        """The direct control for the ``now - expires_delta`` mutant: a token minted
        with a POSITIVE delta must not already be in the past."""
        token = security.create_access_token("subject-c", expires_delta=timedelta(minutes=5))

        payload = security.verify_token(token)  # must not raise

        assert payload["exp"] > time.time()


# ── verify_token: the two HTTPException sites' untested half, and the audit record ─


def _mint(claims: dict, *, algorithm: str | None = None) -> str:
    key = OctKey.import_key(settings.JWT_SECRET_KEY)
    header = {"alg": algorithm or settings.JWT_ALGORITHM}
    return _jwt.encode(header, claims, key)


class TestVerifyTokenWrongTypeResponseBody:
    """``test_verify_token_claims.py`` already pins this site's ``headers`` exactly;
    this pins the ``detail`` string the same raise site also carries, which nothing
    checked."""

    def test_the_detail_is_exactly_this_string(self):
        refresh_like = _mint(
            {"sub": "1", "type": TOKEN_TYPE_REFRESH, "exp": int(time.time()) + 600}
        )

        with pytest.raises(HTTPException) as exc:
            security.verify_token(refresh_like, expected_type=TOKEN_TYPE_ACCESS)

        assert exc.value.detail == "Invalid authentication credentials"


class TestVerifyTokenDecodeFailureResponseBody:
    """``test_jwt_algorithm_downgrade.py`` already pins this site's ``detail``
    exactly (via a strict-mode-rejected legacy token); this pins the ``headers``
    dict the same ``except JoseError`` raise site also carries, which nothing
    checked."""

    def test_the_headers_are_exactly_this_dict(self):
        with pytest.raises(HTTPException) as exc:
            security.verify_token("not-a-jwt")

        assert exc.value.status_code == 401
        assert exc.value.headers == {"WWW-Authenticate": "Bearer"}


class TestVerifyTokenLegacyAlgorithmAuditRecord:
    """The FIPS 140-3 legacy-algorithm-fallback audit call -- a structured,
    queryable compliance record (``event_type``/``outcome``/``details`` keys), not
    free-text log output. ``test_jwt_algorithm_downgrade.py`` asserts only
    ``details["warning"]``; this pins the rest of the same call.
    """

    #: HS512 needs a 64-byte key; config.py warns otherwise for a real deployment.
    FIPS_SECRET = "unit-test-audit-record-secret-padded-to-sixty-four-bytes-0123"
    SUBJECT = "019ec90a-1b2c-7def-8000-0000000000aa"

    @pytest.fixture
    def audited(self, monkeypatch) -> list[dict]:
        from app.auth import audit as audit_module

        events: list[dict] = []
        monkeypatch.setattr(audit_module.audit_logger, "log", lambda **kw: events.append(kw))
        return events

    @pytest.fixture
    def fips_legacy_fallback(self, monkeypatch) -> str:
        """A FIPS 140-3, HS512-migrated deployment verifying an HS256-signed
        (legacy) token -- the one path that reaches the audit call."""
        monkeypatch.setattr(settings, "JWT_SECRET_KEY", self.FIPS_SECRET)
        monkeypatch.setattr(settings, "FIPS_MODE", True)
        monkeypatch.setattr(settings, "FIPS_VERSION", "140-3")
        monkeypatch.setattr(settings, "FIPS_MIGRATION_MODE", "compatible")
        monkeypatch.setattr(settings, "JWT_ALGORITHM", "HS512")
        monkeypatch.setattr(settings, "JWT_ALGORITHM_V3", "HS512")
        return _jwt.encode(
            {"alg": "HS256"},
            {
                "sub": self.SUBJECT,
                "type": TOKEN_TYPE_ACCESS,
                "exp": int(time.time()) + 600,
            },
            OctKey.import_key(self.FIPS_SECRET),
            algorithms=["HS256"],
        )

    def test_the_call_carries_the_exact_event_type_and_outcome(self, fips_legacy_fallback, audited):
        security.verify_token(fips_legacy_fallback)

        assert len(audited) == 1
        assert audited[0]["event_type"] == AuditEventType.AUTH_TOKEN_VERIFY
        assert audited[0]["outcome"] == AuditOutcome.SUCCESS

    def test_the_details_dict_carries_both_algorithm_fields_under_their_exact_keys(
        self, fips_legacy_fallback, audited
    ):
        security.verify_token(fips_legacy_fallback)

        assert len(audited) == 1
        details = audited[0]["details"]
        assert details["used_algorithm"] == "HS256"
        assert details["required_algorithm"] == "HS512"
