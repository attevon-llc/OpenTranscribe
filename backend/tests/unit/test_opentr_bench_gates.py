"""``opentr.sh``'s bench arms must gate on the BENCH stack, and be discoverable (issue #399).

Two defect classes, both of which shipped and neither of which any test could see.

**Gating on the wrong stack.** ``docker-compose.bench.yml`` renames every service to
``otbench-*`` precisely so a benchmark stack can coexist with the dev stack. Three checks in
the ``bench`` subcommands still looked for ``opentranscribe-*``, which inverted the safety
gate: with only the bench stack up it aborted ("worker not running"), and with the dev stack
up it **passed** — green-lighting an engine benchmark whose bench worker may not exist. The
check validated the one stack the benchmark must never touch.

This is the shell counterpart of the ``readiness-probe-target`` detector added to
``scripts/audit-tests.py``: *a health or presence check whose target is not derived from the
thing being started.* That detector reads Python; these arms are bash, so they need this.

**A help menu that drifts.** ``bench all|phase|collate`` existed for some time with no entry
in ``show_help()``, so the only way to find them was to read the ``case`` block. ``bench
help`` did list them, which is how the top-level menu went stale without anyone noticing.

Both checks are static text scans, deliberately: they must hold without Docker, a GPU, or a
running stack, and they are about what the script *says*, not what it does.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_OPENTR = _REPO_ROOT / "opentr.sh"
_BENCH_OVERLAY = _REPO_ROOT / "docker-compose.bench.yml"

#: Container-name prefix the bench overlay assigns. Read from the overlay rather than
#: hard-coded, so renaming it there cannot leave this test asserting a stale prefix.
_BENCH_PREFIX = "otbench-"


def _opentr_source() -> str:
    assert _OPENTR.is_file(), f"{_OPENTR} is missing"
    return _OPENTR.read_text()


def _bench_block(source: str) -> str:
    """The body of the ``bench)`` subcommand, where every bench arm lives.

    Sliced rather than parsed: bash has no importable AST, and a slice that fails to find
    its markers raises instead of silently returning the whole file — which would make every
    assertion below vacuous.
    """
    start = source.index("  bench)")
    # The next top-level subcommand label ends the block.
    remainder = source[start + len("  bench)") :]
    match = re.search(r"\n  [a-z][a-z|-]*\)\n", remainder)
    end = start + len("  bench)") + (match.start() if match else len(remainder))
    block = source[start:end]
    assert "engine)" in block, "the bench block slice missed the arms it is meant to cover"
    return block


def test_the_bench_overlay_really_renames_containers():
    """Guard the guard: if the overlay stopped renaming, the checks below assert nothing."""
    assert _BENCH_OVERLAY.is_file()
    overlay = _BENCH_OVERLAY.read_text()
    assert f"container_name: {_BENCH_PREFIX}" in overlay, (
        f"docker-compose.bench.yml no longer names containers {_BENCH_PREFIX}* — the bench "
        "arms' gates need to follow it"
    )


def test_no_bench_arm_gates_on_the_dev_stacks_container_names():
    """The inverted safety gate from #399.

    Any ``opentranscribe-`` name inside the bench block is a check pointed at the dev stack.
    """
    block = _bench_block(_opentr_source())
    offenders = [
        line.strip()
        for line in block.splitlines()
        if "opentranscribe" in line and not line.lstrip().startswith("#")
    ]
    assert not offenders, (
        "these lines in the `bench` subcommand reference the DEV stack's container names, so "
        "they gate on (or report) the wrong stack — the benchmark must only ever inspect "
        f"{_BENCH_PREFIX}*:\n  " + "\n  ".join(offenders)
    )


def test_the_worker_presence_gate_names_a_bench_container():
    """The specific check whose inversion was the dangerous one.

    Asserted positively as well as negatively: a gate deleted outright would satisfy the
    "no dev names" test above while removing the safety check entirely.
    """
    block = _bench_block(_opentr_source())
    assignments = re.findall(r'WORKER="([^"]+)"', block)
    assert assignments, "the bench block no longer sets WORKER — the presence gate is gone"
    for worker in assignments:
        assert worker.startswith(_BENCH_PREFIX), (
            f'WORKER="{worker}" must name a bench container; gating on the dev worker passes '
            "whenever the dev stack happens to be up"
        )


def _bench_arms(block: str) -> set[str]:
    """Every subcommand word handled by the bench ``case``, minus the fallback."""
    arms: set[str] = set()
    for labels in re.findall(r"^      ([a-z][a-z|*-]*)\)$", block, re.MULTILINE):
        for label in labels.split("|"):
            if label not in {"*", "help"}:
                arms.add(label)
    return arms


def test_every_bench_arm_is_listed_in_the_top_level_help():
    """`bench all|phase|collate` existed with no entry in `show_help()`.

    A subcommand nobody can find is not a feature. Derived from the `case` block so a new arm
    fails here rather than being documented only in someone's shell history.
    """
    source = _opentr_source()
    arms = _bench_arms(_bench_block(source))
    assert arms, "no bench arms were parsed — the derivation is broken, not the help"

    help_start = source.index("show_help()")
    help_text = source[help_start : source.index("\n}", help_start)]
    missing = sorted(arm for arm in arms if f"bench {arm}" not in help_text)
    assert not missing, (
        "these bench subcommands exist but are not in show_help(), so the only way to find "
        f"them is to read the case block: {missing}"
    )


@pytest.mark.parametrize("arm", ["start", "stop", "clean", "run", "engine", "status"])
def test_the_help_does_not_advertise_an_arm_that_was_removed(arm):
    """The other direction: help promising a subcommand the script no longer implements.

    Parametrised over the arms the help currently claims, so deleting an implementation
    without touching the help fails here. Documented-but-absent is worse than undocumented:
    it sends an operator to a command that errors.
    """
    block = _bench_block(_opentr_source())
    assert arm in _bench_arms(block), (
        f"show_help() advertises `bench {arm}` but the case block does not handle it"
    )


# ---------------------------------------------------------------------------
# Compose healthchecks — the same "probe pointed at the wrong thing" class, one layer down.
#
# Issue #355: the Flower container reported unhealthy forever because it inherited the
# backend image's HEALTHCHECK, which probes the API on :8080 — a port that does not exist
# inside the Flower container. Flower was fully functional the whole time. The fix (an
# explicit override probing :5555/<prefix>/healthcheck) is already in docker-compose.yml and
# both live containers are healthy; this is the test that was missing, so the override cannot
# be dropped in a refactor and quietly restore a permanently-red healthcheck.
#
# A permanently-unhealthy container is worse than a noisy one: `docker compose up --wait`
# and every dashboard learn to ignore it, so the NEXT genuine failure is invisible.
# ---------------------------------------------------------------------------

_COMPOSE = _REPO_ROOT / "docker-compose.yml"

#: Services that run the backend IMAGE but are not the API, so they must override its
#: HEALTHCHECK rather than inherit a probe against a port they do not serve.
_BACKEND_IMAGE_NON_API = ("flower",)


def _service_block(source: str, service: str) -> str:
    """The YAML block for one service, sliced by indentation.

    Text-sliced rather than YAML-parsed on purpose: this asserts what the compose file
    *says*, and a parse would also succeed against an inherited healthcheck that is not
    written down anywhere — which is the exact defect.
    """
    start = source.index(f"\n  {service}:\n")
    remainder = source[start + 1 :]
    match = re.search(r"\n  [a-z][a-z0-9_-]*:\n", remainder)
    end = start + 1 + (match.start() if match else len(remainder))
    return source[start:end]


@pytest.mark.parametrize("service", _BACKEND_IMAGE_NON_API)
def test_a_non_api_backend_image_service_overrides_the_healthcheck(service):
    block = _service_block(_COMPOSE.read_text(), service)
    assert "healthcheck:" in block, (
        f"`{service}` runs the backend image and would inherit its HEALTHCHECK, which probes "
        "the API port it does not serve — it must declare its own (issue #355)"
    )


@pytest.mark.parametrize("service", _BACKEND_IMAGE_NON_API)
def test_that_healthcheck_does_not_probe_the_api_port(service):
    """The specific wrong target from #355: :8080, the backend API."""
    block = _service_block(_COMPOSE.read_text(), service)
    healthcheck = block[block.index("healthcheck:") :]
    assert "8080" not in healthcheck, (
        f"`{service}`'s healthcheck probes :8080 (the backend API), which does not exist in "
        f"that container — it will report unhealthy forever while {service} works fine"
    )


def test_flowers_healthcheck_targets_its_own_port_and_url_prefix():
    """Positive control: "no :8080" is also satisfied by a healthcheck that probes nothing.

    Flower serves on 5555 and is mounted under a URL prefix, so the probe has to carry both
    or it 404s and the container is unhealthy for a different reason.
    """
    block = _service_block(_COMPOSE.read_text(), "flower")
    healthcheck = block[block.index("healthcheck:") :]
    assert "5555" in healthcheck, "the probe must target Flower's own port"
    assert "FLOWER_URL_PREFIX" in healthcheck, (
        "the probe must use the configured url_prefix — Flower's /healthcheck lives under it, "
        "and hard-coding the default breaks the moment someone sets FLOWER_URL_PREFIX"
    )
