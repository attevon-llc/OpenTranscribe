"""`./opentr.sh bench` must address the bench stack, not the dev stack.

`docker-compose.bench.yml` overrides ``container_name`` to ``otbench-*`` on every
service, precisely so a bench stack can run beside the dev stack without a name
collision (Docker container names are global — the compose project name alone
does not separate them). Every ``docker ps`` / ``docker inspect`` / ``docker
exec`` in the bench flow therefore has to match ``otbench-*``.

Three of them matched ``opentranscribe-*`` instead (issue #399), and one of those
was a safety gate rather than a cosmetic display:

    if ! docker ps --format '{{.Names}}' | grep -q "^${WORKER}$"; then

With ``WORKER="opentranscribe-celery-worker"`` that gate validated **the one
stack the benchmark must not touch**. It aborted when only the bench stack was
up, and it *passed* when the dev stack was up — green-lighting an engine
benchmark whose bench worker might not exist at all. Auditing the same
neighbourhood found a fourth the issue had not: ``wait_for_bench_backend_health``
polled ``opentranscribe-backend``, so the bench stack's readiness wait watched
the dev backend's health.

These tests are string-level on purpose. A shell `case` block is not worth a
parser, the failure mode is literally "the wrong string", and exercising the real
thing means standing up two GPU stacks. Two rules keep them from ossifying around
one particular fix:

* the expected prefix is read out of **docker-compose.bench.yml**, which is the
  authority on what the containers are called — not out of opentr.sh, which would
  make the test agree with the script by construction;
* ``$VAR`` references in the strings under test are expanded against opentr.sh's
  own literal assignments, so a fix that inlines names and a fix that introduces
  a constant both satisfy the same assertion.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
OPENTR = REPO_ROOT / "opentr.sh"
BENCH_COMPOSE = REPO_ROOT / "docker-compose.bench.yml"

# Subcommands whose `case` pattern is a catch-all or is the usage text itself —
# there is nothing for show_help() to document separately.
NON_DOCUMENTED_ARMS = frozenset({"help", "*"})

pytestmark = pytest.mark.skipif(
    not OPENTR.exists() or not BENCH_COMPOSE.exists(),
    reason="opentr.sh / docker-compose.bench.yml not present in this checkout",
)


def _script() -> str:
    return OPENTR.read_text(encoding="utf-8")


def _block(text: str, start_pattern: str, end_pattern: str) -> str:
    """Slice from the first line matching `start_pattern` to the next `end_pattern`."""
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if re.search(start_pattern, line)), None)
    assert start is not None, f"{start_pattern!r} not found in opentr.sh"
    end = next((i for i in range(start + 1, len(lines)) if re.match(end_pattern, lines[i])), None)
    assert end is not None, f"no {end_pattern!r} after line {start + 1} in opentr.sh"
    return "\n".join(lines[start : end + 1])


def _bench_case_block() -> str:
    return _block(_script(), r'case "\$BENCH_SUBCOMMAND" in', r"^\s{4}esac\s*$")


def _show_help_body() -> str:
    return _block(_script(), r"^show_help\(\)\s*\{", r"^\}\s*$")


def _bench_health_helper() -> str:
    return _block(_script(), r"^wait_for_bench_backend_health\(\)\s*\{", r"^\}\s*$")


def _compose_container_names() -> list[str]:
    return re.findall(r"^\s*container_name:\s*(\S+)", BENCH_COMPOSE.read_text(), re.M)


def _bench_container_prefix() -> str:
    """The prefix docker-compose.bench.yml actually gives every bench container.

    Read from the compose file rather than opentr.sh on purpose: the compose file
    is what Docker obeys, so it is the only side of this contract that can be
    "right" independently of the script under test.
    """
    names = _compose_container_names()
    assert names, "docker-compose.bench.yml declares no container_name overrides"
    prefixes = {name.split("-", 1)[0] for name in names}
    assert len(prefixes) == 1, f"bench containers use mixed prefixes: {sorted(prefixes)}"
    return prefixes.pop()


def _shell_assignments(text: str) -> dict[str, str]:
    """`NAME="value"` assignments in `text` (including `local`/`export` ones)."""
    return dict(
        re.findall(
            r'^\s*(?:local\s+|export\s+|readonly\s+)?([A-Za-z_][A-Za-z0-9_]*)="([^"`]*)"',
            text,
            re.M,
        )
    )


def _expand(value: str, scope: str | None = None) -> str:
    """Substitute `${NAME}` / `$NAME` using opentr.sh's own assignments.

    `scope` (the function or case block the value came from) takes precedence
    over the file, because a shell `local` shadows — opentr.sh has two unrelated
    `local container=` declarations, and resolving against the wrong one made
    this test report a container that appears nowhere near the bench flow.

    Iterated to a fixed point so an indirection resolves too (`"$container"` ->
    `"${BENCH_CONTAINER_PREFIX}-backend"` -> `"otbench-backend"`). That is what
    lets one assertion accept every shape of a correct fix: an inlined
    `"otbench-celery-worker"`, a shared constant, or a local holding either.
    """
    assignments = _shell_assignments(_script())
    if scope is not None:
        assignments.update(_shell_assignments(scope))
    for _ in range(5):
        expanded = value
        for name, literal in assignments.items():
            expanded = re.sub(
                r"\$\{" + name + r"\}|\$" + name + r"\b", literal.replace("\\", "\\\\"), expanded
            )
        if expanded == value:
            break
        value = expanded
    return value


def _bench_arms() -> set[str]:
    """Subcommands the bench `case` block actually implements.

    Derived from the source so a new arm is covered the day it lands, instead of
    the day someone remembers to update a list in this file.
    """
    arms: set[str] = set()
    for line in _bench_case_block().splitlines():
        match = re.match(r"^\s{6}([a-z|*]+)\)\s*$", line)
        if match:
            arms.update(match.group(1).split("|"))
    return arms


def test_the_bench_overlay_renames_containers_away_from_the_dev_stack():
    """The premise of every assertion below. If this changes, they are all wrong."""
    prefix = _bench_container_prefix()
    assert prefix != "opentranscribe", (
        "docker-compose.bench.yml no longer renames bench containers; the whole "
        "dev/bench name separation this file tests has been removed"
    )
    assert len(_compose_container_names()) >= 10, (
        "docker-compose.bench.yml renames suspiciously few services — a service "
        "without a container_name override keeps the dev stack's global name"
    )


def test_bench_flow_never_names_a_dev_stack_container():
    """The core of #399: matching `opentranscribe-*` addresses the wrong stack.

    Covers the bench `case` block *and* `wait_for_bench_backend_health`, which
    lives outside it and had the same defect (`docker inspect
    opentranscribe-backend`) despite existing only to serve the bench flow.
    """
    offenders: list[str] = []
    for label, block in (
        ("bench case block", _bench_case_block()),
        ("wait_for_bench_backend_health", _bench_health_helper()),
    ):
        for lineno, line in enumerate(block.splitlines(), start=1):
            if line.lstrip().startswith("#"):
                continue
            if re.search(r"opentranscribe[-\"'\s]", line):
                offenders.append(f"{label} (+{lineno}): {line.strip()}")

    assert not offenders, (
        "bench commands referencing dev-stack container names. "
        "docker-compose.bench.yml renames every service to "
        f"{_bench_container_prefix()}-*, so these address the wrong stack "
        "(issue #399):\n" + "\n".join(f"  {o}" for o in offenders)
    )


def test_engine_gate_checks_the_bench_worker():
    """The dangerous one: a safety gate that validated the stack to stay off.

    Asserts both halves — that WORKER resolves to a container the bench overlay
    actually creates, and that the `docker ps` gate is still keyed on WORKER
    rather than an inlined name.
    """
    block = _bench_case_block()

    worker = re.search(r'^\s*WORKER="([^"]+)"', block, re.M)
    assert worker, "the engine arm no longer assigns WORKER"
    resolved = _expand(worker.group(1), scope=block)
    expected = f"{_bench_container_prefix()}-celery-worker"
    assert resolved == expected, (
        f"engine benchmark gates on container {resolved!r}, which is not a "
        f"container docker-compose.bench.yml creates; expected {expected!r}"
    )
    assert resolved in _compose_container_names(), (
        f"{resolved!r} is not among the bench overlay's container_name values"
    )

    assert 'grep -q "^${WORKER}$"' in block, (
        "the engine arm's worker-presence gate no longer matches on ${WORKER} — "
        "an inlined container name is how #399 happened"
    )


def test_bench_status_and_start_list_bench_containers():
    """`docker ps | grep <prefix>` — grepping `opentranscribe` showed the dev stack.

    `bench status` reported "(none running)" for a live bench stack, and `bench
    start`'s post-start listing printed the dev containers instead of the bench
    ones it had just started.
    """
    block = _bench_case_block()
    # (?:-\S+\s+)* skips grep flags, so `grep -q <pattern>` yields the pattern.
    patterns = re.findall(r"docker ps [^\n|]*\|\s*grep\s+(?:-\S+\s+)*(\S+)", block)
    assert patterns, "no `docker ps | grep` found in the bench case block"
    # The single-container gate has its own test; these are the listings.
    listings = {_expand(p, scope=block).strip("\"'") for p in patterns if "WORKER" not in p}
    assert listings == {_bench_container_prefix()}, (
        f"bench `docker ps` listings grep for {sorted(listings)}; they must match "
        f"{_bench_container_prefix()!r} so they show the bench stack"
    )


def test_bench_health_wait_polls_the_bench_backend():
    """A readiness wait on the wrong container returns as soon as *dev* is healthy."""
    helper = _bench_health_helper()
    # The container is the last token before the `2>` redirect on each line.
    inspected = re.findall(r"docker (?:inspect|logs)[^\n]*?(\S+)\s+2>", helper)
    assert inspected, "wait_for_bench_backend_health no longer inspects a container"
    resolved = {_expand(target, scope=helper).strip("\"'") for target in inspected}
    expected = f"{_bench_container_prefix()}-backend"
    assert resolved == {expected}, (
        f"the bench readiness wait polls {sorted(resolved)}; it must poll "
        f"{expected!r} or it is reporting on the dev stack's health"
    )


def test_show_help_documents_every_bench_subcommand():
    """A help menu that omits real subcommands hides them.

    `bench all`, `bench phase` and `bench collate` — the whole end-to-end
    orchestrator — were implemented and undocumented at the top level.
    """
    documented = _show_help_body()
    missing = sorted(
        arm for arm in _bench_arms() - NON_DOCUMENTED_ARMS if f"bench {arm}" not in documented
    )
    assert not missing, (
        f"opentr.sh implements `bench {{{','.join(missing)}}}` but show_help() "
        "does not list them. Add a line per arm to the Benchmark Commands section."
    )


def test_bench_usage_text_documents_every_bench_subcommand():
    """The `bench help` arm is the second help surface and drifts independently."""
    usage = _bench_case_block()
    usage = usage[usage.index("help|*)") :]
    missing = sorted(
        arm for arm in _bench_arms() - NON_DOCUMENTED_ARMS if f"bench {arm}" not in usage
    )
    assert not missing, f"`./opentr.sh bench help` omits: {missing}"


def test_the_arm_list_is_derived_and_non_trivial():
    """Guard the guard: an empty derived list would pass both help tests silently."""
    arms = _bench_arms()
    assert len(arms - NON_DOCUMENTED_ARMS) >= 8, (
        f"only parsed {sorted(arms)} out of the bench case block — the pattern "
        "regex has drifted from the source and the help tests are now vacuous"
    )
