"""`stop_all_containers()`'s two `down`/`stop`/etc. chains must reach every profile-gated
GPU service, or `./opentr.sh stop` leaves them running (issue #782, finding N5).

Sibling of `test_opentr_fresh_destroy_profile_leak.py`, which closed the identical defect
shape for `fresh_destroy()`: `docker compose down` only tears down services whose profile
is ACTIVE for THIS invocation, not every service that merely exists in the project. Before
this fix, neither of `stop_all_containers()`'s two chains named
`docker-compose.gpu-split.yml` or `docker-compose.diar-native.yml` at all, and neither set
`COMPOSE_PROFILES`, so `celery-worker-gpu-transcribe`/`-gpu-diarize` (gpu-split),
`celery-worker-gpu-scaled` (gpu-scale) and `diar-native` were reachable ONLY by the
straggler-cleanup loop that runs after both `down` calls -- and that loop's job is to
catch what `down` MISSED, not to be the primary teardown path for three real GPU workers.

This file asserts the STATIC shape of the fix (both chains carry both new `-f` flags and
`COMPOSE_PROFILES="*"`), because actually starting a `--gpu-split`/diar-native deployment
and observing containers survive `./opentr.sh stop` needs the live stack and a GPU --
exactly the class of check `test_opentr_fresh_destroy_profile_leak.py` also limits itself
to for the same reason.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
OPENTR_SH = REPO_ROOT / "opentr.sh"


def _stop_all_containers_body() -> str:
    source = OPENTR_SH.read_text(encoding="utf-8")
    match = re.search(
        r"^stop_all_containers\(\)\s*\{(.*?)^\}\s*$",
        source,
        re.MULTILINE | re.DOTALL,
    )
    assert match, (
        "could not find a stop_all_containers() function in opentr.sh -- did it get renamed?"
    )
    return match.group(1)


def _dev_and_prod_down_lines(body: str) -> tuple[str, str]:
    """The two `docker compose ... "$@" ...` chain invocations, as single strings (each
    spans several physical lines via backslash continuation) -- sliced between their
    lead-in comments and the next blank line, rather than re-parsing bash continuation
    syntax generically."""
    dev_start = body.index("# Dev compose chain")
    dev_end = body.index("\n\n", dev_start)
    prod_start = body.index("# Prod compose chain", dev_end)
    prod_end = body.index("\n\n", prod_start)
    return body[dev_start:dev_end], body[prod_start:prod_end]


def test_both_chains_include_the_gpu_split_overlay() -> None:
    body = _stop_all_containers_body()
    dev_chain, prod_chain = _dev_and_prod_down_lines(body)
    for label, chain in (("dev", dev_chain), ("prod", prod_chain)):
        assert "-f docker-compose.gpu-split.yml" in chain, (
            f"the {label} chain in stop_all_containers() does not include "
            "docker-compose.gpu-split.yml -- celery-worker-gpu-transcribe/-gpu-diarize "
            "are reachable only by the straggler loop, not by this chain's own `down` "
            "(issue #782 finding N5)"
        )


def test_both_chains_include_the_diar_native_overlay() -> None:
    body = _stop_all_containers_body()
    dev_chain, prod_chain = _dev_and_prod_down_lines(body)
    for label, chain in (("dev", dev_chain), ("prod", prod_chain)):
        assert "-f docker-compose.diar-native.yml" in chain, (
            f"the {label} chain in stop_all_containers() does not include "
            "docker-compose.diar-native.yml -- the diar-native sidecar is reachable "
            "only by the straggler loop, not by this chain's own `down` (issue #782 "
            "finding N5)"
        )


def test_both_down_calls_carry_compose_profiles_wildcard() -> None:
    """COMPOSE_PROFILES="*" (supported since Compose v2.24) is load-bearing, not
    cosmetic: without it, `down` tears down only services whose profile is ACTIVE for
    THIS invocation, so gpu-split/gpu-scale-profiled services survive even with the
    right `-f` flags present -- the identical mechanism test_opentr_fresh_destroy_
    profile_leak.py pins for fresh_destroy()."""
    body = _stop_all_containers_body()
    dev_chain, prod_chain = _dev_and_prod_down_lines(body)
    for label, chain in (("dev", dev_chain), ("prod", prod_chain)):
        assert 'COMPOSE_PROFILES="*"' in chain, (
            f"the {label} chain's compose invocation in stop_all_containers() does not "
            'set COMPOSE_PROFILES="*" -- a profile-gated service (celery-worker-gpu-'
            "scaled, celery-worker-gpu-transcribe, celery-worker-gpu-diarize) started "
            "under this compose project survives `./opentr.sh stop` even with the "
            "matching -f overlay present, because `down` only acts on ACTIVE profiles"
        )


def test_the_gpu_scale_overlay_was_not_accidentally_dropped() -> None:
    """Regression guard for the edit itself: adding the two new -f flags must not have
    displaced the pre-existing gpu-scale overlay this chain already carried."""
    body = _stop_all_containers_body()
    dev_chain, prod_chain = _dev_and_prod_down_lines(body)
    for label, chain in (("dev", dev_chain), ("prod", prod_chain)):
        assert "-f docker-compose.gpu-scale.yml" in chain, (
            f"the {label} chain in stop_all_containers() lost its pre-existing "
            "docker-compose.gpu-scale.yml overlay while gpu-split/diar-native were added"
        )


def test_the_drain_call_precedes_both_down_chains() -> None:
    """The N5 fix closes the profile GAP; issue #782's actual point is that the workers
    these chains reach get a chance to shut down cleanly rather than being SIGKILLed by
    `down`'s own default timeout. ot_drain_gpu_workers_by_container() must run before
    either chain, not after."""
    body = _stop_all_containers_body()
    drain_idx = body.find("ot_drain_gpu_workers_by_container")
    dev_idx = body.find("# Dev compose chain")
    assert drain_idx != -1, (
        "stop_all_containers() no longer calls ot_drain_gpu_workers_by_container"
    )
    assert dev_idx != -1, "stop_all_containers()'s dev-chain comment marker is gone -- did it move?"
    assert drain_idx < dev_idx, (
        "ot_drain_gpu_workers_by_container() is called AFTER the dev chain's `down` -- "
        "it must run first, or the workers are already SIGKILLed by the time it drains them"
    )
