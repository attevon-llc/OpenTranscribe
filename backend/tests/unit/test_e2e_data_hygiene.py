"""Every E2E test that creates persistent state must remove it, and must not name it.

``backend/tests/CLAUDE.md`` states the rule this module enforces: **E2E must never
persist changes to dev data.** Backend pytest is safe by construction — ``db_session``
wraps each test in a savepoint — but E2E has no such harness. It drives a real browser
and a real HTTP client against the live dev stack, so anything it creates is committed,
indexed, and stored for good.

Until now that rule was enforced by nothing, and it had already been broken: a
registration test with a hard-coded ``test@example.com`` created a real account on the
live stack that had to be deleted by hand. ``scripts/cleanup-test-users.py`` exists
because of that class of leak, and its own keep-list still carries ``test@example.com``
as though it were a legitimate dev account.

Three hazards, one per check
----------------------------
1. **Create without teardown.** A test (or fixture) that POSTs to a creating endpoint,
   or drives a creating form, and has no ``finally`` / cleaning fixture / finalizer.
2. **Hard-coded identity.** An email, a created object's name, or a value typed into a
   field that names a persistent object, with no ``uuid4``/random/pid suffix. This is
   the exact defect that leaked the account: a fixed identity turns "my test made a
   row" into "my test made *the* row", which then collides with, or outlives, every
   later run. ``admin@example.com`` / ``password`` are the legitimate shared dev
   credentials and live in the allow-list, not in the findings.
3. **A negative login that uses a WRONG PASSWORD for an account that exists.**
   The inverse hazard, also documented in ``backend/tests/CLAUDE.md``: lockout is
   per-account and progressive (``app/auth/lockout.py``, keyed on the resolved
   account's email by ``canonical_identifier``), so one such test poisons every later
   test that logs in as that account. The correct form is a **nonexistent** account,
   which takes the identical 401 path.

Why AST and not "run it and see"
-------------------------------
A leak is only observable *after* it has happened, on a stack you then have to clean by
hand. The whole point is to fail in CI, on the fast unit suite, before the E2E run
touches anything. Nothing here imports ``app.*``, opens a socket, or needs a database.

Deliberate scope limits
-----------------------
- **Creation is detected through this app's HTTP API and its own UI.** State created
  inside a throwaway IdP container (``test_ldap_oidc.py``'s LLDAP users and Keycloak
  realm) is out of scope: that container is destroyed with the stack, and the local
  ``User`` rows those logins provision are the deliberate ``ldap-``/``kc-`` dev
  fixtures ``cleanup-test-users.py`` keeps on purpose.
- **A registration form submitted with a syntactically valid email counts as creating,
  whether or not the server accepts it.** Whether the password policy or a validator
  rejects a given payload is not something the test file can guarantee — the policy is
  DB-backed and admin-editable (``core/auth_settings.py`` defaults: min length 12, all
  four character classes) — and the leak happened precisely because a test assumed
  rejection. Cleanup is a no-op when nothing was created, so the safe direction is to
  require it.
- **A bare delete on the happy path is not cleanup.** It is skipped by the very
  assertion failure that leaves the object behind, which is exactly how
  ``test_upload.py`` could leave an uploaded file in the dev library.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

_TESTS_ROOT = Path(__file__).resolve().parents[1]
_E2E_ROOT = _TESTS_ROOT / "e2e"

# ---------------------------------------------------------------------------
# What counts as creating persistent state
# ---------------------------------------------------------------------------

#: A dynamic path segment, or an expression this scanner deliberately does not resolve.
_WILDCARD = "*"

#: Collection endpoints where POST *creates a row*. Matched against the full normalized
#: path, not as a prefix, so the many sub-action POSTs under the same prefixes
#: (``/api/tags/promote``, ``/api/files/management/bulk-action``,
#: ``/api/files/{uuid}/prepare-download``) are correctly not treated as creation.
_CREATING_PATHS = frozenset(
    {
        "/api/auth/register",
        "/api/admin/users",
        "/api/invitations",
        "/api/tags",
        "/api/collections",
        "/api/chat/conversations",
        "/api/llm-settings",
        "/api/watch-sources",
        "/api/watch-sources/email-configs",
        "/api/speaker-profiles/profiles",
        "/api/prompts",
        "/api/files",  # bare POST = upload
        "/api/files/upload",
    }
)

#: Endpoints that change **shared, persistent configuration** rather than creating a
#: row. A test that flips one of these and does not restore it in a ``finally`` leaves
#: the dev stack reconfigured — ``force_pii`` left ON changes what every other user of
#: the stack sees. Prefix-matched, because the config surface is a tree.
_MUTATING_CONFIG_PREFIXES = (
    "/api/admin/redaction-policy",
    "/api/admin/settings",
    "/api/admin/auth-config",
    "/api/admin/engine-settings",
    "/api/user-settings",
)

_CONFIG_MUTATING_METHODS = frozenset({"post", "put", "patch"})

#: UI flows that commit a new persistent object. Each entry is
#: ``(form_marker, commit_marker, label)``: the form marker must appear as a string
#: constant in the function AND the commit marker must appear inside a ``.click()``
#: call, because a test that merely asserts the Save button is *visible* creates
#: nothing (``test_watch_sources_e2e.py::test_stepper_walks_all_steps` walks the
#: stepper to Save and stops there deliberately). Keyed on the app's own selectors and
#: button labels, so a rename breaks the guard loudly rather than silently disarming it
#: — ``test_scanner_finds_a_known_ui_creation`` is the backstop.
_UI_CREATION_MARKERS: tuple[tuple[str, str, str], ...] = (
    ("Create Account", "Create Account", "submits the registration form"),
    ("#ws-name", "Save", "saves a watch source through the stepper"),
    ("New tag", "Add", "creates a tag through the tag manager"),
    ("Tag name", "Add tag", "creates a tag through the bulk-apply flow"),
)

#: The registration marker above only counts when a syntactically valid email is typed
#: in — see "Deliberate scope limits" in the module docstring. It is also the marker
#: that suppresses the wrong-password check: the register form has an ``#email`` and a
#: ``#password`` field too, but submitting it is not a login attempt and cannot move a
#: lockout counter.
_REGISTRATION_MARKER = "Create Account"

#: Fields whose value NAMES a persistent object. A literal here is a fixed identity.
_IDENTITY_FIELD_SELECTORS = frozenset({"#ws-name", "#collection-name"})

#: JSON keys in a creating POST body whose value names the created object.
_IDENTITY_PAYLOAD_KEYS = frozenset({"name", "title", "filename"})

# ---------------------------------------------------------------------------
# What counts as cleanup
# ---------------------------------------------------------------------------

#: Names that mean "this call removes something on the server". Used to decide whether
#: a *fixture's* teardown is about server state or merely about the browser context —
#: ``page.close()`` / ``context.close()`` must not be mistaken for data cleanup, or
#: every test that takes an authenticated-page fixture would pass this gate for free.
_DELETING_CALL_NAMES = frozenset({"delete", "unlock_account"})

_ADD_FINALIZER = "addfinalizer"

# ---------------------------------------------------------------------------
# Identities
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.[A-Za-z]{2,}")

#: Markers that make a string run-unique. Presence of any of these in the expression
#: that produced a literal means the identity is not fixed.
_UNIQUIFIERS = (
    "uuid4",
    "uuid1",
    "uuid7",
    "token_hex",
    "randint",
    "randrange",
    "choice",
    "getpid",
    "time()",
    "monotonic",
    "urandom",
    "uuid_pkg",
)

#: Emails that are deliberately fixed, each with the reason it is legitimate.
_ALLOWED_EMAILS: dict[str, str] = {
    "admin@example.com": (
        "The shared dev super-admin, documented in backend/tests/CLAUDE.md as THE e2e "
        "credential. Pre-existing dev data, never created or deleted by a test."
    ),
    "nonexistent@example.com": (
        "Deliberately nonexistent — this is the CORRECT form of a negative-login test "
        "(backend/tests/CLAUDE.md). Nothing to clean up because nothing can be created."
    ),
    "nosuchuser-e2e@example.com": (
        "Same as nonexistent@example.com: the suite's second never-registered address, "
        "used where a test needs a failing login that is not the admin account."
    ),
    "ldap-admin@example.com": (
        "LLDAP fixture account provisioned by test_ldap_oidc.py's throwaway container. "
        "cleanup-test-users.py keeps the `ldap-` prefix on purpose."
    ),
    "ldap-user@example.com": "As ldap-admin@example.com — LLDAP fixture account.",
    "ldap-negative@example.com": (
        "LLDAP fixture account reserved for the wrong-password test and never logged in "
        "successfully, so the app provisions no local User for it and its lockout bucket "
        "belongs to no account (test_ldap_oidc.py::test_ldap_wrong_password_rejected)."
    ),
    "kc-admin@example.com": (
        "Keycloak realm fixture account in the throwaway IdP container. "
        "cleanup-test-users.py keeps the `kc-` prefix on purpose."
    ),
    "kc-user@example.com": "As kc-admin@example.com — Keycloak realm fixture account.",
    "admin@example.com / password": (
        "Docstring prose naming the shared dev credentials, not a value any test sends."
    ),
}

#: Accounts known to EXIST on a stack the e2e suite runs against. A failing login
#: against one of these increments its lockout counter.
_EXISTING_ACCOUNTS = frozenset(
    {
        "admin@example.com",
        "admin",
        "ldap-admin",
        "ldap-user",
        "ldap-admin@example.com",
        "ldap-user@example.com",
        "kc-admin",
        "kc-user",
        "kc-admin@example.com",
        "kc-user@example.com",
    }
)

#: Passwords that are CORRECT for one of the accounts above. A login that submits one
#: of these is a positive test and cannot trip lockout.
_CORRECT_PASSWORDS = frozenset(
    {"password", "admin_password", "LdapAdmin123", "LdapUser123", "KcAdmin123", "KcUser123"}
)

#: Helper functions that submit the app's login form / token endpoint, and the positions
#: of their (identifier, password) arguments.
_LOGIN_HELPERS: dict[str, tuple[int, int]] = {
    "_login_local": (1, 2),
    "_login_browser": (2, 3),
    "login": (0, 1),
}

# ---------------------------------------------------------------------------
# Allow-lists. An entry is a decision with a written reason, never a parking space.
# ---------------------------------------------------------------------------

#: Tests that create-or-appear-to-create without teardown, and why that is accepted.
_CREATES_WITHOUT_CLEANUP_ALLOWLIST: dict[str, str] = {
    "test_registration.py::test_duplicate_email_rejected": (
        "Submits admin@example.com — an email that ALREADY exists — so the unique "
        "constraint on User.email makes creation impossible by construction; the test "
        "asserts the 'Email already registered' rejection. Cleanup would have to target "
        "the shared dev admin account, which must never be deleted."
    ),
    "test_auth_flow.py::test_registration_duplicate_email_fails": (
        "Same shape as test_registration.py::test_duplicate_email_rejected: registers "
        "the existing admin@example.com to assert the duplicate rejection."
    ),
}

#: Hard-coded identities that are deliberately fixed, and why.
_FIXED_IDENTITY_ALLOWLIST: dict[str, str] = {}

#: Negative logins against an existing account, and why the lockout risk is accepted.
_WRONG_PASSWORD_ALLOWLIST: dict[str, str] = {}


# ---------------------------------------------------------------------------
# AST plumbing
# ---------------------------------------------------------------------------


def _e2e_modules() -> list[Path]:
    """Every e2e module, including ``conftest.py`` (its fixtures create state too)."""
    return sorted(p for p in _E2E_ROOT.glob("*.py"))


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """``id()`` of every Constant that is a module/class/function docstring.

    Prose that mentions ``admin@example.com / password`` is documentation, not a value
    the test sends; flagging it would put pure narration in the findings.
    """
    ids: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        body = getattr(node, "body", [])
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            ids.add(id(body[0].value))
    return ids


def _module_literals(tree: ast.Module) -> dict[str, str]:
    """Module-level ``NAME = "literal"`` bindings, including the ``os.environ.get`` form.

    ``TEST_ADMIN_EMAIL = os.environ.get("E2E_ADMIN_EMAIL", "admin@example.com")`` is the
    dominant shape in this suite; without resolving its default, half the identity
    checks would see an unresolvable expression and pass by accident.
    """
    literals: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        value: str | None = None
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            value = node.value.value
        elif isinstance(node.value, ast.Call) and len(node.value.args) == 2:
            fallback = node.value.args[1]
            if isinstance(fallback, ast.Constant) and isinstance(fallback.value, str):
                value = fallback.value
        if value is None:
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                literals[target.id] = value
    return literals


def _conftest_literals() -> dict[str, str]:
    """``e2e/conftest.py``'s constants, which most modules import by name."""
    conftest = _E2E_ROOT / "conftest.py"
    if not conftest.exists():  # pragma: no cover - the suite cannot run without it
        return {}
    return _module_literals(ast.parse(conftest.read_text()))


def _flatten(node: ast.AST | None, literals: dict[str, str]) -> str:
    """Render an expression as a string, with ``*`` for anything dynamic."""
    if node is None:
        return _WILDCARD
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else _WILDCARD
    if isinstance(node, ast.Name):
        return literals.get(node.id, _WILDCARD)
    if isinstance(node, ast.JoinedStr):
        return "".join(_flatten(part, literals) for part in node.values)
    if isinstance(node, ast.FormattedValue):
        return _WILDCARD
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _flatten(node.left, literals) + _flatten(node.right, literals)
    if isinstance(node, ast.Attribute):
        return _WILDCARD
    return _WILDCARD


def _api_path(node: ast.AST | None, literals: dict[str, str]) -> str | None:
    """Normalize a URL expression to its ``/api/...`` path, or ``None``."""
    rendered = _flatten(node, literals)
    index = rendered.find("/api/")
    if index < 0:
        return None
    path = rendered[index:].split("?", 1)[0].rstrip("/")
    return path or None


def _has_uniquifier(node: ast.AST) -> bool:
    source = ast.dump(node)
    return any(marker.rstrip("()") in source for marker in _UNIQUIFIERS)


def _local_bindings(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> dict[str, ast.expr]:
    """``name = <expr>`` bindings inside a function body.

    Needed because the suite's own good pattern binds first and uses second
    (``name = _unique_tag_name(); ... .fill(name)``). Without resolving the binding, the
    uniquifying helper is invisible and every correct test reads as an offender.
    """
    bindings: dict[str, ast.expr] = {}
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    bindings[target.id] = node.value
    return bindings


def _is_uniquified(
    node: ast.expr,
    bindings: dict[str, ast.expr],
    uniquifying_helpers: set[str],
) -> bool:
    """Whether a value is run-unique, resolving one level of local binding / helper."""
    if _has_uniquifier(node):
        return True
    if isinstance(node, ast.Call) and _call_name(node) in uniquifying_helpers:
        return True
    if isinstance(node, ast.Name) and node.id in bindings:
        inner = bindings[node.id]
        if _has_uniquifier(inner):
            return True
        if isinstance(inner, ast.Call) and _call_name(inner) in uniquifying_helpers:
            return True
    return False


def _string_constants(fn: ast.AST, docstrings: set[int]) -> list[str]:
    return [
        n.value
        for n in ast.walk(fn)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings
    ]


def _marker_haystack(constants: list[str]) -> str:
    """All of a function's string constants, joined for substring marker matching.

    Substring rather than equality because the app's own selectors embed the label:
    the register submit is ``"button:has-text('Create Account')"``, not
    ``"Create Account"``. An equality check here silently matched nothing, which
    disarmed the whole UI-creation half of the gate — hence
    ``test_scanner_finds_a_known_ui_creation``.
    """
    return " | ".join(constants)


def _call_name(call: ast.Call) -> str:
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return getattr(call.func, "id", "")


def _region_has_call(nodes: list[ast.stmt]) -> bool:
    return any(isinstance(n, ast.Call) for stmt in nodes for n in ast.walk(stmt))


def _region_deletes(nodes: list[ast.stmt], deleting_helpers: set[str]) -> bool:
    """Whether a teardown region removes server state (directly or via a helper)."""
    for stmt in nodes:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Call):
                name = _call_name(node)
                if name in _DELETING_CALL_NAMES or name in deleting_helpers:
                    return True
    return False


def _teardown_region(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.stmt]:
    """Statements a generator fixture runs AFTER its ``yield`` — its teardown."""
    yields = [
        n for n in ast.walk(fn) if isinstance(n, ast.Yield | ast.YieldFrom) and n.lineno is not None
    ]
    if not yields:
        return []
    last_yield_line = max(n.lineno for n in yields)
    return [
        stmt
        for stmt in ast.walk(fn)
        if isinstance(stmt, ast.stmt) and stmt.lineno > last_yield_line
    ]


def _finally_regions(fn: ast.AST) -> list[list[ast.stmt]]:
    return [node.finalbody for node in ast.walk(fn) if isinstance(node, ast.Try) and node.finalbody]


def _is_fixture(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for dec in fn.decorator_list:
        for node in ast.walk(dec):
            if isinstance(node, ast.Attribute) and node.attr == "fixture":
                return True
    return False


def _params(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    return {a.arg for a in fn.args.args} | {a.arg for a in fn.args.kwonlyargs}


# ---------------------------------------------------------------------------
# Per-function analysis
# ---------------------------------------------------------------------------


def _click_markers(fn: ast.AST) -> str:
    """A dump of every ``.click()`` call, for "was the commit button actually clicked?"."""
    return " ".join(
        ast.dump(node)
        for node in ast.walk(fn)
        if isinstance(node, ast.Call) and _call_name(node) == "click"
    )


def _supplies_an_email(fn: ast.AST) -> bool:
    """Whether the function types something that could be a valid address into ``#email``.

    A LITERAL is checked against the email shape, so the deliberately malformed probes
    (``"notanemail"``, ``"test@"``, ``"testexample.com"``) do not count — they cannot
    create anything. Anything DYNAMIC counts: a variable or f-string is the correct,
    uniquified form, and the whole point is that those submissions succeed. Requiring a
    literal here made the guard blind to every properly written registration test.
    """
    for node in ast.walk(fn):
        if not (isinstance(node, ast.Call) and _call_name(node) == "fill"):
            continue
        if len(node.args) < 2:
            continue
        selector = node.args[0]
        if not (isinstance(selector, ast.Constant) and selector.value == "#email"):
            continue
        value = node.args[1]
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            if _EMAIL_RE.search(value.value):
                return True
            continue
        return True
    return False


def _creation_reasons(
    fn: ast.FunctionDef | ast.AsyncFunctionDef, literals: dict[str, str], docstrings: set[int]
) -> list[str]:
    """Why this function creates or reconfigures persistent state (empty = it does not)."""
    reasons: list[str] = []

    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        method = _call_name(node)
        first = node.args[0] if node.args else None

        if method == "post":
            path = _api_path(first, literals)
            if path in _CREATING_PATHS:
                reasons.append(f"POST {path}")
        if method in _CONFIG_MUTATING_METHODS:
            path = _api_path(first, literals)
            if path and path.startswith(_MUTATING_CONFIG_PREFIXES):
                reasons.append(f"{method.upper()} {path} (shared configuration)")
        if method == "register":
            reasons.append("auth_helper.register()")

    constants = _string_constants(fn, docstrings)
    haystack = _marker_haystack(constants)
    clicks = _click_markers(fn)
    for form_marker, commit_marker, label in _UI_CREATION_MARKERS:
        if form_marker not in haystack or commit_marker not in clicks:
            continue
        if form_marker == _REGISTRATION_MARKER and not _supplies_an_email(fn):
            # An empty or malformed-email registration submit cannot create anything.
            continue
        reasons.append(label)

    return sorted(set(reasons))


def _cleans_up(
    fn: ast.FunctionDef | ast.AsyncFunctionDef,
    cleaning_fixtures: set[str],
    deleting_helpers: set[str],
) -> bool:
    """Whether this function's own structure guarantees teardown.

    A ``finally`` written inside the function that creates is unambiguously about that
    creation, so any call in it counts. A *fixture's* post-``yield`` teardown has to
    name a deletion, because ``page.close()`` is a teardown of the browser and not of
    the data — accepting it would hand a free pass to every test that takes an
    authenticated-page fixture.
    """
    if any(_region_has_call(region) for region in _finally_regions(fn)):
        return True
    if any(_call_name(n) == _ADD_FINALIZER for n in ast.walk(fn) if isinstance(n, ast.Call)):
        return True
    if _is_fixture(fn) and _region_deletes(_teardown_region(fn), deleting_helpers):
        return True
    return bool(_params(fn) & cleaning_fixtures)


def _fixed_emails(fn: ast.AST, docstrings: set[int]) -> list[str]:
    """Every non-allow-listed email address written as a literal in this function."""
    findings: list[str] = []
    for node in ast.walk(fn):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if id(node) in docstrings:
            continue
        for email in _EMAIL_RE.findall(node.value):
            if email.strip().lower() not in _ALLOWED_EMAILS:
                findings.append(f"fixed email {email!r}")
    return findings


def _fixed_object_names(
    fn: ast.AST,
    literals: dict[str, str],
    bindings: dict[str, ast.expr],
    uniquifying_helpers: set[str],
) -> list[str]:
    """Fixed values that NAME a persistent object: a name field, or a create payload."""
    findings: list[str] = []

    def _render(node: ast.expr) -> str:
        rendered = _flatten(node, literals)
        if rendered == _WILDCARD and isinstance(node, ast.Name) and node.id in bindings:
            return _flatten(bindings[node.id], literals)
        return rendered

    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        method = _call_name(node)

        # A literal typed into a field that names a persistent object.
        if method == "fill" and len(node.args) >= 2:
            selector = _flatten(node.args[0], literals)
            if selector in _IDENTITY_FIELD_SELECTORS and not _is_uniquified(
                node.args[1], bindings, uniquifying_helpers
            ):
                findings.append(f"fixed {selector} value {_render(node.args[1])!r}")

        # A literal name/title in a creating POST body.
        if method != "post":
            continue
        if _api_path(node.args[0] if node.args else None, literals) not in _CREATING_PATHS:
            continue
        payloads = [kw.value for kw in node.keywords if kw.arg == "json"]
        payloads += [arg for arg in node.args[1:] if isinstance(arg, ast.Dict)]
        for payload in payloads:
            if not isinstance(payload, ast.Dict):
                continue
            findings += _fixed_payload_names(payload, _render, bindings, uniquifying_helpers)

    return findings


def _fixed_payload_names(
    payload: ast.Dict,
    render,  # noqa: ANN001 - a local closure over the enclosing literals/bindings
    bindings: dict[str, ast.expr],
    uniquifying_helpers: set[str],
) -> list[str]:
    findings: list[str] = []
    for key, value in zip(payload.keys, payload.values, strict=False):
        if not (isinstance(key, ast.Constant) and key.value in _IDENTITY_PAYLOAD_KEYS):
            continue
        if _is_uniquified(value, bindings, uniquifying_helpers):
            continue
        rendered = render(value)
        if rendered != _WILDCARD:
            findings.append(f"fixed {key.value} {rendered!r} in a created object")
    return findings


def _identity_findings(
    fn: ast.FunctionDef | ast.AsyncFunctionDef,
    literals: dict[str, str],
    docstrings: set[int],
    uniquifying_helpers: set[str],
) -> list[str]:
    """Fixed identities this function sends: emails, created-object names, name fields."""
    bindings = _local_bindings(fn)
    findings = _fixed_emails(fn, docstrings)
    findings += _fixed_object_names(fn, literals, bindings, uniquifying_helpers)
    return sorted(set(findings))


def _wrong_password_findings(
    fn: ast.FunctionDef | ast.AsyncFunctionDef, literals: dict[str, str], docstrings: set[int]
) -> list[str]:
    """Negative logins that submit a wrong password for an account that EXISTS.

    Two shapes, both present in the suite: paired ``fill("#email", ...)`` /
    ``fill("#password", ...)`` calls, and a login helper called with the pair as
    positional arguments.

    Two deliberate exclusions:

    * ``#email`` is the only identifier field matched, because it is *this app's* login
      field. The Keycloak page's own ``#username``/``#password`` form drives the IdP's
      lockout inside a throwaway container, not this app's per-account counter.
    * A function that submits the **registration** form is skipped entirely. That form
      also has ``#email`` and ``#password``, and the duplicate-email tests legitimately
      pair the admin address with a fresh password — but registering is not a login
      attempt and cannot move a lockout counter.
    """
    if _REGISTRATION_MARKER in _marker_haystack(_string_constants(fn, docstrings)):
        return []

    findings: list[str] = []
    identifier: str | None = None

    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)

        if name == "fill" and len(node.args) >= 2:
            selector = _flatten(node.args[0], literals)
            value = _flatten(node.args[1], literals).strip()
            if selector == "#email":
                identifier = value.lower()
            elif selector == "#password" and identifier in _EXISTING_ACCOUNTS:
                if value not in _CORRECT_PASSWORDS and value != _WILDCARD:
                    findings.append(f"{identifier!r} + wrong password {value!r}")
            continue

        if name in _LOGIN_HELPERS:
            id_pos, pw_pos = _LOGIN_HELPERS[name]
            if len(node.args) <= max(id_pos, pw_pos):
                continue
            who = _flatten(node.args[id_pos], literals).strip().lower()
            pw = _flatten(node.args[pw_pos], literals).strip()
            if who in _EXISTING_ACCOUNTS and pw not in _CORRECT_PASSWORDS and pw != _WILDCARD:
                findings.append(f"{who!r} + wrong password {pw!r}")

    return sorted(set(findings))


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------


def _collect() -> tuple[dict[str, str], dict[str, str], dict[str, str], int, int]:
    """Return (uncleaned, fixed_identities, wrong_passwords, modules, functions)."""
    uncleaned: dict[str, str] = {}
    identities: dict[str, str] = {}
    wrong_passwords: dict[str, str] = {}
    module_count = 0
    function_count = 0

    conftest = _conftest_literals()

    for path in _e2e_modules():
        source = path.read_text()
        try:
            tree = ast.parse(source)
        except SyntaxError:  # pragma: no cover - a syntax error fails collection anyway
            continue
        module_count += 1
        rel = path.name
        docstrings = _docstring_nodes(tree)
        literals = {**conftest, **_module_literals(tree)}

        functions = [
            n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
        ]

        # Module-level helpers that themselves delete, so a `finally: _delete_user(...)`
        # and a fixture teardown that calls one both resolve.
        deleting_helpers = {fn.name for fn in functions if _region_deletes(list(fn.body), set())}
        cleaning_fixtures = {
            fn.name
            for fn in functions
            if _is_fixture(fn) and _region_deletes(_teardown_region(fn), deleting_helpers)
        }
        # Helpers that mint a run-unique value (`_unique_tag_name`, `_unique_email`).
        uniquifying_helpers = {fn.name for fn in functions if _has_uniquifier(fn)}

        for fn in functions:
            interesting = fn.name.startswith("test_") or _is_fixture(fn)
            if not interesting:
                continue
            function_count += 1
            ident = f"{rel}::{fn.name}"

            reasons = _creation_reasons(fn, literals, docstrings)
            if reasons and not _cleans_up(fn, cleaning_fixtures, deleting_helpers):
                if ident not in _CREATES_WITHOUT_CLEANUP_ALLOWLIST:
                    uncleaned[ident] = "; ".join(reasons)

            found = _identity_findings(fn, literals, docstrings, uniquifying_helpers)
            if found and ident not in _FIXED_IDENTITY_ALLOWLIST:
                identities[ident] = "; ".join(found)

            bad_logins = _wrong_password_findings(fn, literals, docstrings)
            if bad_logins and ident not in _WRONG_PASSWORD_ALLOWLIST:
                wrong_passwords[ident] = "; ".join(bad_logins)

        # Module-level fixed identities. Only top-level statements that are NOT function
        # or class definitions: those are reported against the owning function above, and
        # walking them here would double-report every finding.
        module_emails = sorted(
            {
                email
                for node in tree.body
                if not isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
                for inner in ast.walk(node)
                if isinstance(inner, ast.Constant)
                and isinstance(inner.value, str)
                and id(inner) not in docstrings
                for email in _EMAIL_RE.findall(inner.value)
                if email.strip().lower() not in _ALLOWED_EMAILS
            }
        )
        module_ident = f"{rel}::<module>"
        if module_emails and module_ident not in _FIXED_IDENTITY_ALLOWLIST:
            identities[module_ident] = "; ".join(f"fixed email {e!r}" for e in module_emails)

    return uncleaned, identities, wrong_passwords, module_count, function_count


_UNCLEANED, _IDENTITIES, _WRONG_PASSWORDS, _MODULES, _FUNCTIONS = _collect()


def _report(findings: dict[str, str]) -> str:
    return "\n  ".join(f"{ident} -> {why}" for ident, why in sorted(findings.items()))


# ---------------------------------------------------------------------------
# The gates
# ---------------------------------------------------------------------------


def test_every_creating_e2e_test_tears_down() -> None:
    """E2E has no savepoint harness — what it creates on the dev stack stays there."""
    assert not _UNCLEANED, (
        "These e2e tests/fixtures create or reconfigure persistent state on the LIVE "
        "dev stack with no guaranteed teardown (no try/finally, no addfinalizer, no "
        "requested fixture whose teardown deletes). Add cleanup that runs whether the "
        "test passes or fails — a delete on the happy path is skipped by the very "
        "failure that leaves the object behind:\n  " + _report(_UNCLEANED)
    )


def test_no_e2e_test_uses_a_fixed_identity() -> None:
    """A fixed identity turns "my test made a row" into "my test made THE row"."""
    assert not _IDENTITIES, (
        "These e2e tests name persistent objects with a fixed identity instead of a "
        "uuid4/random-suffixed one. This is the defect that created a real "
        "test@example.com account on the live stack: the identity outlives the run, so "
        "it collides with the next one and cannot be swept by name. Suffix it, or "
        "allow-list it with a reason:\n  " + _report(_IDENTITIES)
    )


def test_no_negative_login_targets_an_existing_account() -> None:
    """Lockout is per-account and progressive — one such test poisons the whole suite."""
    assert not _WRONG_PASSWORDS, (
        "These e2e tests submit a WRONG PASSWORD for an account that exists. Lockout is "
        "keyed on the resolved account (app/auth/lockout.py: canonical_identifier) and "
        "escalates, so every later test that logs in as that account inherits the "
        "failure. Use a NONEXISTENT account — it takes the identical 401 path "
        "(backend/tests/CLAUDE.md):\n  " + _report(_WRONG_PASSWORDS)
    )


# ---------------------------------------------------------------------------
# Guard the guard: a scanner that silently matches nothing passes everything.
# ---------------------------------------------------------------------------


def test_scanner_parsed_the_e2e_suite() -> None:
    assert _MODULES >= 20, f"suspiciously few e2e modules parsed: {_MODULES}"
    assert _FUNCTIONS >= 200, f"suspiciously few e2e tests/fixtures parsed: {_FUNCTIONS}"


def test_scanner_finds_a_known_api_creation() -> None:
    """``test_tag_management.py`` POSTs ``/api/tags`` — the reference creating call."""
    path = _E2E_ROOT / "test_tag_management.py"
    tree = ast.parse(path.read_text())
    literals = {**_conftest_literals(), **_module_literals(tree)}
    docstrings = _docstring_nodes(tree)
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "test_modal_opens_with_the_tag_list"
    )
    assert _creation_reasons(fn, literals, docstrings) == ["POST /api/tags"]


def test_scanner_finds_a_known_ui_creation() -> None:
    """``test_registration.py``'s success test drives the register form to completion."""
    path = _E2E_ROOT / "test_registration.py"
    tree = ast.parse(path.read_text())
    literals = {**_conftest_literals(), **_module_literals(tree)}
    docstrings = _docstring_nodes(tree)
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "test_successful_registration_redirects"
    )
    assert "submits the registration form" in _creation_reasons(fn, literals, docstrings)


def test_scanner_recognises_a_known_cleaning_fixture() -> None:
    """``tag_api``'s teardown sweeps this suite's tags — the reference cleanup shape."""
    path = _E2E_ROOT / "test_tag_management.py"
    tree = ast.parse(path.read_text())
    functions = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
    tag_api = next(fn for fn in functions if fn.name == "tag_api")
    assert _is_fixture(tag_api)
    assert _region_deletes(_teardown_region(tag_api), set())


def test_scanner_does_not_mistake_page_close_for_data_cleanup() -> None:
    """A browser-context teardown is not data cleanup, or every test passes for free."""
    path = _E2E_ROOT / "test_tag_management.py"
    tree = ast.parse(path.read_text())
    tags_page = next(
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "tags_page"
    )
    assert not _region_deletes(_teardown_region(tags_page), set())


def test_scanner_reads_the_shared_dev_credentials() -> None:
    """Identity checks depend on resolving conftest's constants by name."""
    assert _conftest_literals().get("TEST_ADMIN_EMAIL") == "admin@example.com"


# ---------------------------------------------------------------------------
# Allow-list honesty (mirrors test_ddl_marker_discipline.py)
# ---------------------------------------------------------------------------


def test_allowlists_are_honest() -> None:
    """A stale entry grants an exemption to nothing while reading as deliberate."""
    stale: list[str] = []
    unexplained: list[str] = []
    for allowlist in (
        _CREATES_WITHOUT_CLEANUP_ALLOWLIST,
        _FIXED_IDENTITY_ALLOWLIST,
        _WRONG_PASSWORD_ALLOWLIST,
    ):
        for ident, reason in sorted(allowlist.items()):
            module, _, name = ident.partition("::")
            path = _E2E_ROOT / module
            missing_module = not path.exists()
            missing_test = (
                not missing_module
                and name != "<module>"
                and (f"def {name}(" not in path.read_text())
            )
            if missing_module or missing_test:
                stale.append(ident)
            if not reason.strip():
                unexplained.append(ident)

    assert not stale, f"allow-list entries point at e2e tests that no longer exist: {stale}"
    assert not unexplained, f"allow-list entries need a written reason: {unexplained}"


def test_allowed_emails_each_carry_a_reason() -> None:
    """The email allow-list is the one most likely to grow by accident."""
    unexplained = [email for email, reason in _ALLOWED_EMAILS.items() if not reason.strip()]
    assert not unexplained, f"allow-listed emails need a written reason: {unexplained}"
