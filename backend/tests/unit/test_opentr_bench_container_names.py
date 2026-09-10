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


#: `docker ps ... | grep [flags] <pattern>` — the pattern is what addresses a stack.
#: `(?:-\S+\s+)*` skips grep's flags, so the capture is the pattern whether the call is
#: `grep <p>`, `grep -q <p>`, `grep -c <p>` or `grep -c -- <p>`. It must stay flag-agnostic:
#: commit 58871c8b rewrote every `grep -q` in opentr.sh to capture-then-`grep -c` (a `grep -q`
#: under `pipefail` can SIGPIPE its producer and read a MATCH as a non-match), and a regex that
#: only knew `-q` would have silently stopped extracting anything.
_DOCKER_PS_GREP = re.compile(r"docker ps\b[^\n|]*\|\s*grep\s+(?:-\S+\s+)*(\S+)")


def _docker_ps_grep_patterns(block: str) -> list[str]:
    return _DOCKER_PS_GREP.findall(block)


def _docker_ps_probes(block: str) -> list[str]:
    """Non-comment lines that ask docker which containers are running."""
    return [
        line.strip()
        for line in block.splitlines()
        if "docker ps" in line and not line.lstrip().startswith("#")
    ]


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

    Asserts two independent halves:

    1. ``WORKER`` resolves to a container the bench overlay actually creates;
    2. the ``docker ps`` presence gate is **derived from the ``WORKER`` variable**
       rather than from a container name written out at the probe.

    (2) is the property, not any particular spelling of it. This assertion used to be
    ``'grep -q "^${WORKER}$"' in block`` — a literal — and commit 58871c8b broke it by
    making a **correct** change: every ``grep -q`` in opentr.sh became capture-then-test
    (``[ "$(... | grep -c ...)" -eq 0 ]``), because a ``grep -q`` under ``pipefail``
    SIGPIPEs its producer and can read a MATCH as a non-match. A guard that fails on the
    fix for a real bug teaches people to delete guards.

    What #399 actually was: the gate said ``opentranscribe-celery-worker`` while the bench
    overlay names every container ``otbench-*``, so the safety check validated **the one
    stack the benchmark must not touch** — it aborted when only the bench stack was up and
    passed when the dev stack was. That is an *inlined name* defect, and it is invisible to
    (1) alone: ``WORKER`` can be perfectly correct while the probe ignores it. So the check
    is "the probe references ``${WORKER}``, and names no container itself", which survives
    ``grep -q`` -> ``grep -c`` -> ``docker ps --filter name=`` alike.
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

    probes = _docker_ps_probes(block)
    gate_lines = [line for line in probes if re.search(r"\$\{?WORKER\}?", line)]
    assert gate_lines, (
        "no `docker ps` probe in the bench engine arm is keyed on ${WORKER}. Either the "
        "worker-presence gate is gone, or it now spells a container name out at the "
        "probe — which is exactly issue #399: the gate said "
        "`opentranscribe-celery-worker` while WORKER said otherwise, so it validated the "
        "dev stack. The `docker ps` lines found were:\n" + "\n".join(f"  {p}" for p in probes)
    )

    for line in gate_lines:
        named = re.findall(r"\b(?:otbench|opentranscribe|otfresh)-[A-Za-z0-9_.-]+", line)
        assert not named, (
            f"the worker-presence gate writes a container name out: {named}. It must "
            "address the worker through ${WORKER} (checked above against "
            "docker-compose.bench.yml), so the name and the gate cannot drift apart:\n"
            f"  {line}"
        )


def test_bench_status_and_start_list_bench_containers():
    """`docker ps | grep <prefix>` — grepping `opentranscribe` showed the dev stack.

    `bench status` reported "(none running)" for a live bench stack, and `bench
    start`'s post-start listing printed the dev containers instead of the bench
    ones it had just started.
    """
    block = _bench_case_block()
    patterns = _docker_ps_grep_patterns(block)
    assert patterns, "no `docker ps | grep` found in the bench case block"
    # The single-container gate has its own test; these are the listings.
    listings = {_expand(p, scope=block).strip("\"'") for p in patterns if "WORKER" not in p}
    bench_prefix = _bench_container_prefix()

    # `bench rag` (#403 Stage 1) is a peer arm that measures retrieval over a
    # corpus injected into an ISOLATED --fresh deployment, so it legitimately
    # addresses `otfresh-<name>-*` rather than `otbench-*`. The rule #399 encodes
    # is not "always otbench" — it is "address the deployment you are measuring,
    # never the dev stack", so both prefixes pass and `opentranscribe-` does not.
    for pattern in sorted(listings):
        assert "opentranscribe" not in pattern, (
            f"bench `docker ps` listing greps for {pattern!r} — that is the DEV "
            "stack's container prefix, which is exactly issue #399"
        )
        assert bench_prefix in pattern or "otfresh-" in pattern, (
            f"bench `docker ps` listing greps for {pattern!r}; it must address "
            f"either the bench stack ({bench_prefix!r}) or an isolated "
            "`otfresh-<name>` deployment"
        )
    assert any(bench_prefix in pattern for pattern in listings), (
        f"no bench listing addresses {bench_prefix!r} any more"
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


def test_the_grep_pattern_extractor_survives_a_change_of_grep_flags():
    """Guard the guard: an extractor that matched nothing would pass every listing test.

    The flag set is not stable. `grep -q` was correct until 58871c8b proved it inverts under
    `pipefail`, and became `grep -c`; a `grep -m1` or a `grep -c --` is the same shape again.
    The extractor must key on the PATTERN's position, never on which flags precede it, or a
    routine change makes `_docker_ps_grep_patterns` return `[]` — and an empty list of
    patterns is indistinguishable from a bench flow that greps nothing wrong.
    """
    cases = {
        # (the spelling, the pattern it must yield)
        "docker ps --format '{{.Names}}' | grep \"^${W}$\"": '"^${W}$"',
        "docker ps --format '{{.Names}}' | grep -q \"^${W}$\"": '"^${W}$"',
        'if [ "$(docker ps --format \'{{.Names}}\' | grep -c "^${W}$")" -eq 0 ]; then': '"^${W}$")"',
        'docker ps -a | grep -c -- "^${W}$"': '"^${W}$"',
        "docker ps --format 'table {{.Names}}' | grep \"$PREFIX\"": '"$PREFIX"',
    }
    for source, expected in cases.items():
        got = _docker_ps_grep_patterns(source)
        assert got, f"extractor found no pattern in: {source}"
        assert expected in got[0], f"extractor returned {got!r} for: {source}"

    # ...and it must not invent one where there is no `docker ps` at all.
    assert _docker_ps_grep_patterns('docker inspect x | grep -q "^${W}$"') == []


def test_the_arm_list_is_derived_and_non_trivial():
    """Guard the guard: an empty derived list would pass both help tests silently."""
    arms = _bench_arms()
    assert len(arms - NON_DOCUMENTED_ARMS) >= 8, (
        f"only parsed {sorted(arms)} out of the bench case block — the pattern "
        "regex has drifted from the source and the help tests are now vacuous"
    )


def test_bench_rag_is_a_peer_arm_that_targets_an_isolated_deployment():
    """#403 Stage 1: retrieval quality, measured like every other bench arm.

    The RAG bench is a *peer* of the GPU arms, not a mode of them — it needs no
    GPU, no ASR and no LLM. What it does share is the #399 lesson: it must
    address the deployment it is measuring by that deployment's own container
    names, and it must refuse the shared dev stack.
    """
    block = _bench_case_block()
    assert "rag" in _bench_arms(), "`bench rag` is no longer a dispatched arm"

    rag = block[block.index("      rag)") :]
    rag = rag[: rag.index("\n      help|*)")]

    assert "otfresh-${RAG_FRESH_NAME}-opensearch" in rag, (
        "the rag arm no longer verifies the fresh deployment's OWN OpenSearch "
        "container before measuring it"
    )
    assert "scripts/benchmark_rag.py" in rag, "the rag arm no longer runs the harness"
    assert "exit $?" in rag, (
        "the rag arm must propagate the harness's exit status — opentr.sh ends "
        "in `exit 0`, so without this it reports success however it failed"
    )
    # The harness runs on the host against published ports; .env's hosts are
    # docker-network names and do not resolve there.
    assert re.search(r'export POSTGRES_HOST="\$RAG_HOST"', rag), (
        "the rag arm must ASSIGN the host, not default it — opentr.sh has "
        "already loaded .env, whose POSTGRES_HOST is `postgres`"
    )
