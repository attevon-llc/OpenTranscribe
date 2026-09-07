"""Guards for the shared throwaway-Postgres fixture (``tests/integration/conftest.py``).

Three integration suites used to start a private Postgres container **per test** -- 11
containers, ~124 s of setup each, ~1,360 s of the integration phase's 3,781 s wall clock.
They now share one session-scoped container and get a fresh database each. Two properties of
that arrangement can regress **silently**, which is why they are pinned here rather than left
to review:

* **Speed.** Dropping ``--tmpfs`` from ``PGDATA`` puts ``initdb`` back on this host's md RAID:
  measured 62-92 s to accept connections instead of 4.6 s, and 7.1 s per ``CREATE DATABASE``
  instead of 0.12 s. A slow test is not a failing test, so nothing else would notice.
* **Isolation.** Dropping ``--network none`` would give a throwaway container a route to the
  live dev stack. Also silent -- right up until something reaches it.

And one that regresses loudly but late: a suite quietly reintroducing its own ``docker run``
would restore the cost this change removed, and would only show up as the phase getting slow
again.

These are unit tests on purpose -- they run in the fast suite with **no docker daemon** -- so
the guard fires long before anyone waits an hour to find out.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

from tests.integration import throwaway_pg

_INTEGRATION_DIR = Path(throwaway_pg.__file__).resolve().parent

# The suites that were converted to share one container. Each talks to Postgres exclusively
# over `docker exec`, which is what makes `--network none` possible for them.
_EXEC_ONLY_SUITES = (
    "test_opentr_restore_roundtrip.py",
    "test_dbs_wait_for_speaker_attributes.py",
    "test_dbs_wait_for_table_settled.py",
)

# The must-fire control for the scanner below: this suite connects over TCP to a published
# loopback port (SQLAlchemy/psycopg), so it legitimately keeps its own container and MUST be
# detected as starting one. Without it, a scanner that silently matched nothing would report
# a clean tree -- the exact failure mode `scripts/audit-tests.py --selftest` exists for.
_SUITE_THAT_MUST_START_ITS_OWN = "test_cleanup_test_users_isolated_db.py"


def _fake_run(recorder: list[list[str]], returncode: int = 0):
    def _run(cmd: list[str], *, stdin_text: str | None = None) -> subprocess.CompletedProcess[str]:
        recorder.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode, stdout="", stderr="boom")

    return _run


def _docker_run_argv(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The exact argv ``start_container`` would hand to docker -- no daemon involved."""
    calls: list[list[str]] = []
    monkeypatch.setattr(throwaway_pg, "run", _fake_run(calls))
    throwaway_pg.start_container("ot-unit-probe", password="not-a-real-password")
    assert len(calls) == 1, f"expected exactly one docker invocation, got {calls}"
    return calls[0]


@pytest.mark.unit
def test_the_shared_container_keeps_pgdata_on_a_tmpfs(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ~20x win. Removing this flag costs ~87 s per container and breaks no test."""
    argv = _docker_run_argv(monkeypatch)
    assert "--tmpfs" in argv, (
        "throwaway Postgres must keep PGDATA on a tmpfs -- without it initdb fsyncs a fresh "
        f"cluster onto this host's md RAID (measured 62-92 s vs 4.6 s). argv={argv}"
    )
    assert argv[argv.index("--tmpfs") + 1] == "/var/lib/postgresql/data", (
        f"--tmpfs must cover PGDATA specifically, not some other path. argv={argv}"
    )


@pytest.mark.unit
def test_the_shared_container_cannot_reach_the_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """The safety posture the exec-only suites document: it cannot reach the live dev stack."""
    argv = _docker_run_argv(monkeypatch)
    assert "--network" in argv, f"throwaway Postgres must be network-isolated. argv={argv}"
    assert argv[argv.index("--network") + 1] == "none", (
        f"throwaway Postgres must run with `--network none`, not some other network. argv={argv}"
    )
    assert "-p" not in argv and "--publish" not in argv, (
        f"a `--network none` container must not publish ports. argv={argv}"
    )


@pytest.mark.unit
def test_the_shared_container_is_labelled_so_a_leak_is_findable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session-scoped container outlives every test, so a SIGKILL'd run leaks one. The
    label is the only thing that makes it distinguishable from a real deployment's container.
    """
    argv = _docker_run_argv(monkeypatch)
    assert "--label" in argv, f"throwaway containers must be labelled. argv={argv}"
    assert argv[argv.index("--label") + 1] == throwaway_pg.CONTAINER_LABEL


@pytest.mark.unit
def test_a_failed_container_start_raises_instead_of_yielding_a_dead_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Control for the three tests above: they all pass through ``start_container``, so it has
    to actually check the result rather than assume success.
    """
    monkeypatch.setattr(throwaway_pg, "run", _fake_run([], returncode=1))
    with pytest.raises(AssertionError, match="failed to start throwaway postgres"):
        throwaway_pg.start_container("ot-unit-probe", password="not-a-real-password")


def _sql_statements(
    monkeypatch: pytest.MonkeyPatch, existing: set[str], keep: set[str]
) -> list[str]:
    """Drive ``drop_databases_outside`` with a canned ``pg_database`` listing."""
    statements: list[str] = []

    def _psql(
        container: str, dbname: str, sql: str, *, tuples_only: bool = False
    ) -> subprocess.CompletedProcess[str]:
        statements.append(sql)
        stdout = "\n".join(sorted(existing)) if tuples_only else ""
        return subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")

    monkeypatch.setattr(throwaway_pg, "psql", _psql)
    throwaway_pg.drop_databases_outside("ot-unit-probe", frozenset(keep))
    return statements


@pytest.mark.unit
def test_per_test_cleanup_drops_exactly_the_new_databases_with_force(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole isolation argument for sharing a container rests on this: whatever a test
    created is gone before the next one runs, and a test that deliberately holds a connection
    open (``test_drop_database_with_force_terminates_an_open_connection``) cannot block it.
    """
    statements = _sql_statements(
        monkeypatch,
        existing={"postgres", "template0", "template1", "otrestore_bug", "opentranscribe_test"},
        keep={"postgres", "template0", "template1"},
    )
    drops = [s for s in statements if "DROP DATABASE" in s]
    assert len(drops) == 2, f"expected exactly the two new databases to be dropped, got {drops}"
    assert 'DROP DATABASE "opentranscribe_test" WITH (FORCE);' in drops
    assert 'DROP DATABASE "otrestore_bug" WITH (FORCE);' in drops
    assert not any("postgres" in s and "DROP" in s for s in statements), (
        f"the baseline databases must never be dropped: {statements}"
    )


@pytest.mark.unit
def test_per_test_cleanup_is_a_no_op_when_the_test_created_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Must-stay-clean control for the test above -- a detector that always finds something
    to drop would pass it for the wrong reason.
    """
    baseline = {"postgres", "template0", "template1"}
    statements = _sql_statements(monkeypatch, existing=baseline, keep=baseline)
    assert not [s for s in statements if "DROP DATABASE" in s], (
        f"nothing was created, so nothing may be dropped: {statements}"
    )


@pytest.mark.unit
def test_a_failed_drop_raises_rather_than_leaving_the_cluster_dirty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A silently-skipped drop surfaces later as an unrelated test's CREATE DATABASE failing
    with "already exists" -- a far worse place to start debugging.
    """

    def _psql(
        container: str, dbname: str, sql: str, *, tuples_only: bool = False
    ) -> subprocess.CompletedProcess[str]:
        if tuples_only:
            return subprocess.CompletedProcess([], 0, stdout="postgres\nleftover", stderr="")
        return subprocess.CompletedProcess([], 1, stdout="", stderr="still in use")

    monkeypatch.setattr(throwaway_pg, "psql", _psql)
    with pytest.raises(AssertionError, match="could not drop leftover database 'leftover'"):
        throwaway_pg.drop_databases_outside("ot-unit-probe", frozenset({"postgres"}))


def _starts_its_own_container(path: Path) -> bool:
    """True if the module contains a literal ``docker run`` argv.

    Matched structurally (adjacent string elements of a list literal) rather than by
    substring, so prose in a docstring describing ``docker run`` cannot satisfy or trip it --
    all four of these modules discuss it at length in their docstrings.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.List):
            continue
        literals = [
            e.value for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)
        ]
        for first, second in zip(literals, literals[1:], strict=False):
            if first == "docker" and second == "run":
                return True
    return False


@pytest.mark.unit
def test_the_scanner_fires_on_a_suite_that_really_does_start_its_own_container() -> None:
    """Must-fire control. A detector that matches nothing reports zero findings, which is
    indistinguishable from a clean tree (``scripts/audit-tests.py --selftest``'s lesson).
    """
    control = _INTEGRATION_DIR / _SUITE_THAT_MUST_START_ITS_OWN
    assert control.exists(), f"the must-fire control module is gone: {control}"
    assert _starts_its_own_container(control), (
        f"{_SUITE_THAT_MUST_START_ITS_OWN} connects over TCP and legitimately starts its own "
        "container -- if this no longer detects it, the scanner below proves nothing"
    )


@pytest.mark.unit
def test_the_exec_only_suites_share_one_container_instead_of_starting_their_own() -> None:
    """Each of these paid ~124 s per test for a private container. If one grows a ``docker
    run`` again, that cost comes back as nothing but a slower phase.
    """
    checked = 0
    offenders = []
    for name in _EXEC_ONLY_SUITES:
        path = _INTEGRATION_DIR / name
        assert path.exists(), f"expected converted suite is missing: {path}"
        checked += 1
        if _starts_its_own_container(path):
            offenders.append(name)
    assert checked == len(_EXEC_ONLY_SUITES), "not every converted suite was inspected"
    assert not offenders, (
        f"{offenders} start their own Postgres container again. Use the `isolated_pg` fixture "
        "from tests/integration/conftest.py, or -- if the suite genuinely needs a private "
        "cluster (a server restart, roles, ALTER SYSTEM) -- remove it from _EXEC_ONLY_SUITES "
        "here with a written reason."
    )
