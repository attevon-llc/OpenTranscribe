"""Guards for the two backup round-trip suites' throwaway-container sharing.

Sibling of ``test_throwaway_pg_sharing.py``, which pins the same properties for the shared
**Postgres** container in ``tests/integration/conftest.py``. This file covers the two suites
that conftest does not reach:

* ``test_voiceprint_backup_roundtrip.py`` -- started a throwaway **OpenSearch** per test, and
  now shares one per module.
* ``test_scheduled_backup_restore_roundtrip.py`` -- started a throwaway Postgres per test for
  its Tier 2 (docker-exec-only) tests, and now takes ``conftest.py``'s ``isolated_pg``.

Everything checked here regresses **silently**. A slow test is not a failing test, and an
isolation leak between two tests that both happen to pass is invisible until something else
breaks. Both are cheap to guard and expensive to notice by hand -- the voiceprint suite spent
99.6-237.9 s per test on the 2026-09-07 gate doing work that profiles at well under a second.

These are unit tests on purpose: they run in the fast suite with **no docker daemon**, so the
guard fires long before anyone waits out an integration phase to find out.

⚠️ On the numbers quoted in this file and in the modules it guards. Two independent
measurements of the *same* container operations on this host differ by more than an order of
magnitude -- ``initdb`` at 62-92 s under a loaded docker daemon versus 3.7 s on an idle one.
The **container count** is therefore the property worth pinning, because it is the one that
does not depend on what else the machine was doing. Quote a count here; do not transcribe a
duration.
"""

from __future__ import annotations

import ast
import json
import subprocess
from pathlib import Path

import pytest

from tests.integration import test_scheduled_backup_restore_roundtrip as scheduled
from tests.integration import test_voiceprint_backup_roundtrip as voiceprint

_VOICEPRINT_SRC = Path(voiceprint.__file__).resolve()
_SCHEDULED_SRC = Path(scheduled.__file__).resolve()

# Fixture scopes that pay for a container ONCE for several tests. `function` is the value that
# puts the cost back, one container per test, with nothing else noticing.
_SHARED_SCOPES = frozenset({"module", "package", "session"})


# ---------------------------------------------------------------------------------------------
# The scanner: which fixture in a module issues a literal `docker run`, and at what scope.
# ---------------------------------------------------------------------------------------------


def _issues_docker_run(node: ast.AST) -> bool:
    """True if ``node`` contains a literal ``["docker", "run", ...]`` argv.

    Matched structurally -- adjacent string elements of a list literal -- rather than by
    substring, so the long docstrings in both modules (which discuss ``docker run`` at length)
    can neither satisfy nor trip it.
    """
    return bool(_docker_run_argvs(node))


def _docker_run_argvs(node: ast.AST) -> list[list[str]]:
    """Every literal ``docker run`` argv inside ``node``, as its list of STRING elements.

    ⚠️ Non-constant elements (``f"POSTGRES_PASSWORD={password}"``, a bare ``image`` name) are
    dropped, so this is a view of the argv's *literal* flags only. That is what the checks
    below want — and it is a trap worth naming, because a first draft of
    ``test_every_postgres_container_...`` identified the Postgres container by looking for the
    string ``POSTGRES_PASSWORD``, which is an ``ast.JoinedStr`` and therefore invisible here.
    The guard found nothing and reported it as "no container to check", which is the
    silent-skip shape this whole file exists to prevent. Select by WHERE the argv is (which
    fixture), never by an interpolated value inside it.
    """
    found: list[list[str]] = []
    for sub in ast.walk(node):
        if not isinstance(sub, ast.List):
            continue
        literals = [
            e.value for e in sub.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)
        ]
        if any(a == "docker" and b == "run" for a, b in zip(literals, literals[1:], strict=False)):
            found.append(literals)
    return found


def _fixture_scope(node: ast.FunctionDef) -> str | None:
    """The ``scope=`` of an ``@pytest.fixture`` decorator, or None if not a fixture.

    A bare ``@pytest.fixture`` means function scope, which is exactly the case this must
    report rather than skip -- so an undecorated ``scope`` returns ``"function"``, never None.
    """
    for decorator in node.decorator_list:
        call = decorator if isinstance(decorator, ast.Call) else None
        target = call.func if call is not None else decorator
        name = getattr(target, "attr", None) or getattr(target, "id", None)
        if name != "fixture":
            continue
        if call is None:
            return "function"
        for keyword in call.keywords:
            if keyword.arg == "scope" and isinstance(keyword.value, ast.Constant):
                return str(keyword.value.value)
        return "function"
    return None


def _container_fixture_scopes(source: str) -> dict[str, str]:
    """``{fixture name: scope}`` for every fixture in ``source`` that starts a container."""
    tree = ast.parse(source)
    found: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        scope = _fixture_scope(node)
        if scope is not None and _issues_docker_run(node):
            found[node.name] = scope
    return found


_MUST_FIRE_SOURCE = '''
import pytest

@pytest.fixture
def per_test_container():
    """A fixture that pays for a container on every single test."""
    started = _run(["docker", "run", "-d", "--name", "x", "some/image"])
    yield "x"
'''

_MUST_STAY_CLEAN_SOURCE = '''
import pytest

@pytest.fixture
def talks_about_docker_run():
    """This fixture merely mentions `docker run` in prose and in a string."""
    note = "we used to docker run one of these per test"
    assert note
    yield "already-running-container"
'''


@pytest.mark.unit
def test_the_scanner_fires_on_a_function_scoped_fixture_that_starts_a_container() -> None:
    """Must-fire control. A scanner that matches nothing reports a clean tree, which is
    indistinguishable from a suite with no problem (``scripts/audit-tests.py --selftest``).
    """
    assert _container_fixture_scopes(_MUST_FIRE_SOURCE) == {"per_test_container": "function"}


@pytest.mark.unit
def test_the_scanner_ignores_a_fixture_that_only_talks_about_docker_run() -> None:
    """Must-stay-clean control. Both guarded modules discuss ``docker run`` in their
    docstrings at length; a substring scanner would report every one of them.
    """
    assert _container_fixture_scopes(_MUST_STAY_CLEAN_SOURCE) == {}


# ---------------------------------------------------------------------------------------------
# test_voiceprint_backup_roundtrip.py -- one OpenSearch per MODULE, not per test.
# ---------------------------------------------------------------------------------------------


@pytest.mark.unit
def test_the_voiceprint_suite_starts_exactly_one_opensearch_for_the_whole_module() -> None:
    """It started five (one per test). OpenSearch has no ``initdb``-equivalent to remove with
    ``--tmpfs`` -- the cost is JVM boot and cluster bootstrap, which no flag makes cheaper --
    so the container COUNT is the only waste available here, and it is the only thing keeping
    this suite off the top of the integration phase.
    """
    scopes = _container_fixture_scopes(_VOICEPRINT_SRC.read_text(encoding="utf-8"))
    assert len(scopes) == 1, (
        f"expected exactly one container-starting fixture in {_VOICEPRINT_SRC.name}, got {scopes}"
    )
    name, scope = next(iter(scopes.items()))
    assert scope in _SHARED_SCOPES, (
        f"{_VOICEPRINT_SRC.name}::{name} is `scope={scope!r}`, so every test pays for its own "
        "OpenSearch container again. Share it and give each test a clean cluster via the "
        "`os_container` fixture's index cleanup instead."
    )


@pytest.mark.unit
def test_the_shared_opensearch_publishes_no_ports() -> None:
    """The suite's documented safety posture: the throwaway cluster must be unreachable from
    the host, so it can never be mistaken for -- or reach -- the dev stack's cluster on 5180.
    Sharing one container for longer makes this matter more, not less.
    """
    docker_runs = _docker_run_argvs(ast.parse(_VOICEPRINT_SRC.read_text(encoding="utf-8")))
    assert docker_runs, "no `docker run` argv found at all — this guard would prove nothing"
    offenders = [argv for argv in docker_runs if "-p" in argv or "--publish" in argv]
    assert not offenders, f"the throwaway OpenSearch must publish no ports: {offenders}"


# ---------------------------------------------------------------------------------------------
# The per-test cleanup that makes sharing safe -- driven for real against a fake docker.
# ---------------------------------------------------------------------------------------------


def _fake_run(recorder: list[list[str]], listing: list[str], *, delete_rc: int = 0):
    """A stand-in for the module's ``_run`` that answers ``_cat/indices`` and records DELETEs."""

    def _run(cmd: list[str], *, stdin_text: str | None = None) -> subprocess.CompletedProcess[str]:
        recorder.append(cmd)
        joined = " ".join(cmd)
        if "_cat/indices" in joined:
            body = json.dumps([{"index": name} for name in listing])
            return subprocess.CompletedProcess(cmd, 0, stdout=body, stderr="")
        return subprocess.CompletedProcess(cmd, delete_rc, stdout="", stderr="boom")

    return _run


def _deleted_indices(
    monkeypatch: pytest.MonkeyPatch, listing: list[str], keep: set[str]
) -> list[str]:
    calls: list[list[str]] = []
    monkeypatch.setattr(voiceprint, "_run", _fake_run(calls, listing))
    voiceprint._delete_indices_outside("ot-unit-probe", frozenset(keep))
    return [cmd[-1].rsplit("/", 1)[-1] for cmd in calls if "DELETE" in cmd]


@pytest.mark.unit
def test_per_test_cleanup_deletes_exactly_the_indices_the_test_created(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole isolation argument for sharing rests on this. It is load-bearing in both
    directions: ``test_export_of_a_cluster_with_no_speaker_indices_...`` asserts the cluster
    has none, and the four seeding tests each ``PUT /speakers_v4``, which is REJECTED rather
    than reset if a previous test's index survived.
    """
    deleted = _deleted_indices(
        monkeypatch,
        listing=["speakers_v4", "leftover_probe", "kept_index"],
        keep={"kept_index"},
    )
    assert sorted(deleted) == ["leftover_probe", "speakers_v4"], (
        f"expected exactly the two new indices to be deleted, got {deleted}"
    )


@pytest.mark.unit
def test_per_test_cleanup_never_deletes_a_system_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OpenSearch creates its own ``.``-prefixed indices whenever it likes, and they are not
    in any test's baseline. Deleting one breaks the node rather than resetting it, and the
    next test's failure would point anywhere but here.
    """
    deleted = _deleted_indices(
        monkeypatch,
        listing=[".opensearch-observability", ".plugins-ml-config", "speakers_v4"],
        keep=set(),
    )
    assert deleted == ["speakers_v4"], (
        f"only the test's own index may be deleted; system indices must survive: {deleted}"
    )


@pytest.mark.unit
def test_per_test_cleanup_is_a_no_op_when_the_test_created_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Must-stay-clean control for the two tests above -- a cleanup that always found
    something to delete would pass them for the wrong reason.
    """
    deleted = _deleted_indices(monkeypatch, listing=["speakers_v4"], keep={"speakers_v4"})
    assert deleted == [], f"nothing was created, so nothing may be deleted: {deleted}"


@pytest.mark.unit
def test_a_failed_index_delete_raises_rather_than_leaving_the_cluster_dirty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A silently-skipped delete surfaces later as an unrelated test's ``PUT`` being rejected
    -- and because ``_os_request`` uses ``curl -sS`` (no ``-f``), that rejection is a 400 body
    with a **zero** exit status, i.e. invisible. Failing here is the only loud moment.
    """
    calls: list[list[str]] = []
    monkeypatch.setattr(voiceprint, "_run", _fake_run(calls, ["speakers_v4"], delete_rc=1))
    with pytest.raises(AssertionError, match="could not delete leftover index 'speakers_v4'"):
        voiceprint._delete_indices_outside("ot-unit-probe", frozenset())


# ---------------------------------------------------------------------------------------------
# test_scheduled_backup_restore_roundtrip.py -- Tier 2 shares, Tier 1 does not.
# ---------------------------------------------------------------------------------------------


@pytest.mark.unit
def test_the_scheduled_suites_tier2_fixture_takes_the_shared_postgres() -> None:
    """Its four Tier 2 tests are docker-exec-only and every operation they perform is
    DATABASE-scoped, so they have no reason to own a cluster. If ``pg_container`` grows its
    own ``docker run`` again, the only symptom is a slower phase.
    """
    tree = ast.parse(_SCHEDULED_SRC.read_text(encoding="utf-8"))
    fixtures = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and _fixture_scope(node) is not None
    }
    assert "pg_container" in fixtures, (
        f"pg_container is gone from {_SCHEDULED_SRC.name}; this guard now checks nothing"
    )
    pg_container = fixtures["pg_container"]
    assert not _issues_docker_run(pg_container), (
        "pg_container starts its own Postgres again. Tier 2 is docker-exec-only — take "
        "`isolated_pg` from tests/integration/conftest.py, or, if a test genuinely needs a "
        "private cluster (a server restart, roles, ALTER SYSTEM), say so in its docstring."
    )
    params = [arg.arg for arg in pg_container.args.args]
    assert "isolated_pg" in params, (
        f"pg_container must depend on the shared `isolated_pg` fixture, got params {params}"
    )


@pytest.mark.unit
def test_every_postgres_container_the_scheduled_suite_still_starts_uses_a_tmpfs() -> None:
    """Tier 1's ``networked_pg`` legitimately keeps a private container -- it needs a real
    bridge network, which is incompatible with the shared container's ``--network none``
    posture -- but there is no reason for it to fsync a fresh cluster onto this host's md
    RAID. Measured back to back on an idle daemon: 3.7 s to accept connections without
    ``--tmpfs``, 2.1 s with; under a loaded one the same gap has been measured at 62-92 s
    versus 4.6 s. Free either way, and it is one flag.
    """
    tree = ast.parse(_SCHEDULED_SRC.read_text(encoding="utf-8"))
    # Scoped to FIXTURES on purpose. The module's other `docker run` belongs to
    # `_run_pg_dump_in_backend_container`, which starts a *backend-image* container: a
    # PGDATA tmpfs there would be meaningless, and sweeping it in would make this guard
    # unsatisfiable. Fixtures are where containers-per-test live, which is what this checks.
    fixture_runs = {
        node.name: _docker_run_argvs(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and _fixture_scope(node) is not None
    }
    postgres_runs = [argv for argvs in fixture_runs.values() for argv in argvs]
    assert postgres_runs, (
        "no fixture in this module starts a container any more — if Tier 1 stopped needing "
        "its own Postgres that is fine, but delete this guard rather than leaving it "
        f"matching nothing. fixtures seen: {sorted(fixture_runs)}"
    )
    offenders = [
        argv
        for argv in postgres_runs
        if "--tmpfs" not in argv or argv[argv.index("--tmpfs") + 1] != "/var/lib/postgresql/data"
    ]
    assert not offenders, f"every throwaway Postgres must keep PGDATA on a tmpfs: {offenders}"
