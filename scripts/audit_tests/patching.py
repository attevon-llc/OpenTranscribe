"""Detectors for patch-heavy tests and stand-ins that contradict a test's claim to be real."""

from __future__ import annotations

import ast
import re

from audit_tests.common import MONKEYPATCH_METHODS, dotted

#: `patch`/`monkeypatch` CALLS above this in one test mean the test is mostly scaffolding.
#: Counted per call, not per AST node: the old implementation matched both `Name('patch')`
#: and `Attribute(attr='object')`, so every `patch.object(...)` scored 2 and the effective
#: threshold was 3 — while `monkeypatch.setattr` scored 0 and was invisible entirely.
MAX_PATCH_REFS = 6


def _is_patch_call(node: ast.Call) -> bool:
    """``patch()``, ``patch.object()``, ``mock.patch.dict()``, ``mocker.patch()``."""
    return 'patch' in dotted(node.func)


def _is_monkeypatch_call(node: ast.Call) -> bool:
    """``monkeypatch.setattr(...)`` and friends — a patch by another name."""
    parts = dotted(node.func)
    return bool(parts) and parts[0] == 'monkeypatch' and parts[-1] in MONKEYPATCH_METHODS


def patch_refs(fn: ast.AST) -> int:
    """Count patch CALLS (decorators included — ``ast.walk`` visits ``decorator_list``)."""
    count = 0
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and (_is_patch_call(node) or _is_monkeypatch_call(node)):
            count += 1
    return count


# -------------------------------------------------------------- external-service substitution

#: External services whose behaviour a test can only really prove against the running thing.
#: ``patch`` = substrings that identify the service in a patch/monkeypatch target;
#: ``name`` = words that identify it in an identifier (test name, fixture name, filename).
#: The two differ on purpose: ``boto3`` names S3 in a patch target and never in a test id,
#: while ``http`` names a transport in a fixture name (``fake_http``) and matches far too much
#: as a patch substring.
_EXTERNAL_SERVICES: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    'opensearch': (
        frozenset({'opensearch', 'elasticsearch'}),
        frozenset({'opensearch', 'elasticsearch'}),
    ),
    's3': (frozenset({'minio', 'boto3', 'botocore', 's3'}), frozenset({'minio', 's3'})),
    'redis': (frozenset({'redis'}), frozenset({'redis'})),
    'llm': (
        frozenset({'llm', 'openai', 'anthropic', 'ollama', 'bedrock'}),
        frozenset({'llm', 'openai', 'anthropic', 'ollama', 'bedrock'}),
    ),
    'smtp': (frozenset({'smtp', 'smtplib'}), frozenset({'smtp'})),
    'http': (
        frozenset({'requests', 'httpx', 'aiohttp', 'urllib', 'urlopen'}),
        frozenset({'http', 'httpx', 'requests'}),
    ),
}

#: Words that mark an identifier as naming a *substitute*. A fixture called ``fake_opensearch``
#: says what it is; the failure mode is a fixture like that wired into a test id that says the
#: opposite.
_DOUBLE_WORDS = frozenset(
    {
        'mock',
        'mocks',
        'mocked',
        'fake',
        'fakes',
        'faked',
        'stub',
        'stubs',
        'stubbed',
        'dummy',
        'patched',
        'inmemory',
        'standin',
        'spy',
    }
)

#: Words in a test id that claim the REAL service was exercised.
_REAL_CLAIM_WORDS = frozenset({'integration', 'integrations', 'real', 'live', 'e2e', 'actual'})

#: Multi-word claims that survive word-splitting badly (``end_to_end`` -> end, to, end).
_REAL_CLAIM_PHRASES = ('end_to_end', 'endtoend', 'against_real', 'realworld')

#: Words that make an id honest about running against a substitute. ``unit`` counts: a test
#: under ``tests/unit/`` has already declared what it is, and its id says so.
_HONEST_WORDS = _DOUBLE_WORDS | frozenset({'unit', 'offline', 'simulated', 'contract'})

#: Honest phrases, same reason as ``_REAL_CLAIM_PHRASES``.
_HONEST_PHRASES = ('in_memory', 'stand_in', 'no_stack', 'without_', 'no_service')


#: Word splitter that also breaks camelCase, so ``LLMService`` -> ``llm``, ``service``.
#: Matching service tokens as SUBSTRINGS instead cost a false positive immediately:
#: ``llm`` lives inside ``mfa_enro-llm-ent_module``, which made an MFA test read as a
#: mislabelled LLM integration test.
_WORD_RE = re.compile(r'[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+')


def words(text: str) -> set[str]:
    """Lowercase word set of an identifier or path (``api/test_s3_live.py`` -> api, test, …)."""
    return {m.group(0).lower() for m in _WORD_RE.finditer(text)}


def _service_of(text: str, *, use_name_tokens: bool) -> str | None:
    """The external service an identifier or patch target names, if any."""
    text_words = words(text)
    for service, (patch_tokens, name_tokens) in _EXTERNAL_SERVICES.items():
        tokens = name_tokens if use_name_tokens else patch_tokens
        if tokens & text_words:
            return service
    return None


def _params(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """Every parameter name — for a test or fixture, the fixtures it requests."""
    return [a.arg for a in [*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs]]


def _is_configuration_patch(target: str) -> bool:
    """Is this patch redirecting CONFIGURATION rather than substituting a service?

    ``monkeypatch.setattr(settings, "OPENSEARCH_CHUNKS_INDEX", scratch)`` changes which index
    the code under test writes to. The client is untouched: every assertion still executes on
    the real engine. Pointing a real client at a throwaway index is the *correct* shape for an
    integration test — it is how you get real engine semantics without touching the live index.

    This exists because the detector fired on all 8 tests in
    ``tests/integration/test_rename_propagation_chunks.py``, which do exactly that against a
    real OpenSearch 3.4. Reported by the #403 work, and their framing is the reason it was
    worth fixing rather than allowlisting: **a detector that fires on correct code teaches
    people to allowlist, and an allowlist habit is how the 41-finding backlog formed.**

    The discrimination is a service patch replaces the client or something that returns one
    (``get_opensearch_client``, ``OpenSearch``, ``.search``, ``.index``), whereas a
    configuration patch sets a scalar on a settings object.
    """
    first = target.split(',')[0].strip()
    # `settings` / `Settings` / `app.core.config.settings` as the patch target object.
    if re.search(r'(^|\.)settings$', first, re.IGNORECASE):
        return True
    # The dotted string form: patch("app.core.config.settings.OPENSEARCH_CHUNKS_INDEX").
    return bool(re.search(r'config\.settings\.[A-Z_]+', first))


def _direct_patch_services(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Services substituted by ``patch``/``monkeypatch`` calls written INSIDE this function."""
    services: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and (_is_patch_call(node) or _is_monkeypatch_call(node)):
            target = ', '.join(ast.unparse(a) for a in node.args[:2])
            if _is_configuration_patch(target):
                continue
            service = _service_of(target, use_name_tokens=False)
            if service is not None:
                services.add(service)
    return services


def services_by_fixture(tree: ast.Module) -> dict[str, set[str]]:
    """``fixture name -> services it substitutes``, resolved transitively within the module.

    **This is the part that makes the detector see the real defect.** The #400 suite installs
    its in-memory OpenSearch through a fixture called ``fake_index``::

        @pytest.fixture
        def fake_index(monkeypatch):
            client = _FakeIndex()
            monkeypatch.setattr(svc, 'opensearch_client', client)

    Every test then takes ``fake_index`` and contains no ``patch`` call at all. Reading only
    the test body sees nothing, and reading only the *parameter name* sees nothing either —
    ``fake_index`` says "fake" but never says which service, and naming the service in the
    fixture is a convention no test can be relied on to follow. Resolving the fixture's own
    body is what turns "a name that looks harmless" into "monkeypatches OpenSearch".

    Verified against the real file: before this, the genuine #400 suite stayed clean even when
    deliberately mislabelled into ``tests/integration/`` — the detector fired on its synthetic
    fixture and on nothing else, which is the failure mode this auditor exists to catch.
    """
    functions = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)]
    services = {fn.name: _direct_patch_services(fn) for fn in functions}
    requests_of = {fn.name: _params(fn) for fn in functions}

    # A fixture that requests another fixture inherits its substitutions. Iterate to a
    # fixpoint rather than recursing: fixture graphs in conftest-heavy trees contain cycles
    # through overridden names, and a recursive walk would not terminate.
    for _ in range(len(functions) + 1):
        changed = False
        for name, requested in requests_of.items():
            inherited = {s for r in requested for s in services.get(r, ())}
            if inherited - services[name]:
                services[name] |= inherited
                changed = True
        if not changed:
            break
    return {name: found for name, found in services.items() if found}


def _substitution_targets(
    fn: ast.FunctionDef | ast.AsyncFunctionDef, fixture_services: dict[str, set[str]]
) -> list[tuple[str, str]]:
    """``(service, evidence)`` for every external-service client this test substitutes.

    Three shapes, in descending order of how visible they are in the test itself:

    * a ``patch``/``monkeypatch`` call in the test body;
    * a requested **fixture** that patches the service — resolved through
      :func:`services_by_fixture`, and the shape that hid the #400 near-miss entirely;
    * a parameter whose name says "double" *and* names the service
      (``fake_opensearch``, ``mock_redis_unavailable``). Kept as the fallback for fixtures
      defined in a ``conftest.py`` this scan cannot see.
    """
    out: list[tuple[str, str]] = []
    for service in sorted(_direct_patch_services(fn)):
        out.append((service, f'patches {service}'))

    for param in _params(fn):
        resolved = fixture_services.get(param)
        if resolved:
            for service in sorted(resolved):
                out.append((service, f'fixture {param} patches {service}'))
            continue
        if words(param) & _DOUBLE_WORDS:
            service = _service_of(param, use_name_tokens=True)
            if service is not None:
                out.append((service, f'fixture {param}'))
    return out


#: Markers that DECLARE a test needs the real thing. A marker is part of test selection, so
#: `-m integration` picking up a fully mocked test is the mislabel in its purest form.
_REAL_CLAIM_MARKERS = frozenset({'integration', 'e2e'})

#: Env gates that declare the same thing. ``skipif(SKIP_OPENSEARCH, …)`` means "this test only
#: runs when the real OpenSearch is reachable" — the conftest sets it by TCP probe.
_SERVICE_SKIP_GATES = {
    'skip_opensearch': 'opensearch',
    'skip_s3': 's3',
    'skip_minio': 's3',
    'skip_redis': 'redis',
}


def marker_claims(decorators: list[ast.expr]) -> set[str]:
    """Service names (or ``*`` for any) claimed by a decorator set.

    Recognises ``@pytest.mark.integration``, ``@pytest.mark.needs_opensearch``,
    ``@pytest.mark.<service>``, and ``@pytest.mark.skipif(SKIP_S3, …)``.
    """
    claims: set[str] = set()
    for dec in decorators:
        target = dec.func if isinstance(dec, ast.Call) else dec
        parts = dotted(target)
        if 'mark' not in parts:
            continue
        marker = parts[-1]
        marker_words = words(marker)
        if marker in _REAL_CLAIM_MARKERS:
            claims.add('*')
        if marker in ('skipif', 'skipIf') and isinstance(dec, ast.Call):
            gate_text = ' '.join(ast.unparse(a) for a in dec.args).lower()
            for gate, service in _SERVICE_SKIP_GATES.items():
                if gate in gate_text:
                    claims.add(service)
        for service, (_, name_tokens) in _EXTERNAL_SERVICES.items():
            needs = marker.startswith(('needs_', 'requires_'))
            if (name_tokens & marker_words) and (needs or marker in name_tokens):
                claims.add(service)
    return claims


def pytestmark_values(scope: ast.Module | ast.ClassDef) -> list[ast.expr]:
    """The marker expressions of a ``pytestmark = …`` assignment, module- or class-level."""
    out: list[ast.expr] = []
    for statement in scope.body:
        if not isinstance(statement, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == 'pytestmark' for t in statement.targets):
            continue
        value = statement.value
        out.extend(value.elts if isinstance(value, ast.List | ast.Tuple) else [value])
    return out


def external_service_mock(
    fn: ast.FunctionDef | ast.AsyncFunctionDef,
    rel: str,
    module_claims: set[str],
    fixture_services: dict[str, set[str]],
) -> tuple[str, str] | None:
    """Return ``(services, evidence)`` when a test id claims the real service but mocks it.

    The decided convention (issue #431, from the #403 RAG work): **"ran against the real
    engine" and "ran against a stand-in" must be different test ids.** A suite of twelve tests
    for a ``delete_by_query`` body once ran entirely against an in-memory OpenSearch stand-in
    while carrying ids that read as real-engine coverage — a green suite resting on an
    assumption about the engine, invisible to JUnit history and to the timing analyser.

    A claim of realness is only counted where the test *declares* one, in descending order of
    how hard it is to argue with:

    * a **marker or env gate** — ``@pytest.mark.integration``, ``@pytest.mark.needs_redis``,
      ``skipif(SKIP_OPENSEARCH, …)``, or a module-level ``pytestmark``. These drive selection,
      so this is the strongest form and no naming excuses it;
    * a **realness word in the test's own name** (``test_real_delete_by_query``,
      ``…_against_live_opensearch``). No directory makes that id honest;
    * a **realness word in the module path** (``tests/integration/``, ``tests/e2e/``) — a
      weaker, positional claim, excused when the location or the name declares the substitute.

    The service's own name in the test id is deliberately NOT a claim. It names the *subject*,
    not the engine: on this tree that tier fired on 20 honestly-named unit tests
    (``test_blacklist_token_redis_unavailable_fail_secure``, whose fixture is literally called
    ``mock_redis_unavailable``), which is exactly the false-positive class that makes a gate
    get ignored.
    """
    substitutions = _substitution_targets(fn, fixture_services)
    if not substitutions:
        return None
    substituted = {service for service, _ in substitutions}

    name = fn.name.lower()
    path = rel.lower()
    name_words = words(name)
    path_words = words(path)

    claims = module_claims | marker_claims(fn.decorator_list)
    declared = substituted if '*' in claims else substituted & claims

    honest_name = bool(name_words & _HONEST_WORDS) or any(p in name for p in _HONEST_PHRASES)
    honest_path = bool(path_words & _HONEST_WORDS) or any(p in path for p in _HONEST_PHRASES)
    named = bool(name_words & _REAL_CLAIM_WORDS) or any(p in name for p in _REAL_CLAIM_PHRASES)
    positional = bool(path_words & _REAL_CLAIM_WORDS) or any(p in path for p in _REAL_CLAIM_PHRASES)

    # A realness word in a test name claims the real SERVICE only when the name says which
    # service. `test_a_real_session_still_authorizes_enrollment` uses "real" to qualify the
    # session object, not Redis, and was this detector's first false positive.
    named_services = {s for s in substituted if _EXTERNAL_SERVICES[s][1] & name_words}

    if declared:
        claimed, why = declared, 'marker/env gate'
    elif named and named_services and not honest_name:
        claimed, why = named_services, 'test name'
    elif positional and not (honest_name or honest_path):
        claimed, why = substituted, 'module path'
    else:
        return None

    evidence = '; '.join(sorted({e for _, e in substitutions}))[:100]
    return f'{", ".join(sorted(claimed))} ({why})', evidence
