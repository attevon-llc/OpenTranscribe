"""``fresh-destroy`` must tear down profile-gated services, or it leaks them.

Found while measuring issue #711 criterion 5 (cross-card GPU placement, PR #764's
follow-up): a `--fresh xcard711 --gpu-scale` deployment's `celery-worker-gpu-scaled`
container (defined with `profiles: [gpu-scale]` in ``docker-compose.gpu-scale.yml``)
and its `..._pipeline_scratch` named volume both survived
``./opentr.sh fresh-destroy xcard711`` -- which printed
"✅ Fresh deployment 'xcard711' destroyed" regardless. Reproduced live:

    $ docker ps -a --filter name=otfresh-xcard711
    otfresh-xcard711-celery-worker-gpu-scaled   Up 59 minutes (unhealthy)
    $ docker volume ls | grep xcard711
    local     otfresh-xcard711_pipeline_scratch

``docker compose down`` only tears down services whose profile is ACTIVE for that
invocation, not every service that exists in the project -- and ``fresh_destroy()``
called it with no ``COMPOSE_PROFILES`` set at all. This is the same class of leak
issue #347 closed for `--with-llm-test`'s vLLM container, reached from a different
angle: profile-gated services generically, not one named overlay.

Fix: ``COMPOSE_PROFILES="*"`` (supported since Compose v2.24) on the ``down`` call,
so every profile-gated service in the project is torn down regardless of which
profile started it. Verified live after the fix: same deployment, same
`--gpu-scale` container, `fresh-destroy` removed both the container and the volume.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
OPENTR_SH = REPO_ROOT / "opentr.sh"


def _fresh_destroy_body() -> str:
    source = OPENTR_SH.read_text(encoding="utf-8")
    match = re.search(
        r"^fresh_destroy\(\)\s*\{(.*?)^\}\s*$",
        source,
        re.MULTILINE | re.DOTALL,
    )
    assert match, "could not find a fresh_destroy() function in opentr.sh -- did it get renamed?"
    return match.group(1)


def test_fresh_destroy_down_call_enables_every_profile() -> None:
    body = _fresh_destroy_body()
    down_call_match = re.search(r"^.*docker compose \$chain down\b.*$", body, re.MULTILINE)
    assert down_call_match, (
        "fresh_destroy() no longer runs 'docker compose $chain down' in the shape "
        "this guard expects -- re-point it if the teardown call moved"
    )
    down_line = down_call_match.group(0)

    assert 'COMPOSE_PROFILES="*"' in down_line, (
        "fresh_destroy()'s 'docker compose $chain down' call does not set "
        "COMPOSE_PROFILES=\"*\" -- 'docker compose down' only tears down services "
        "whose profile is ACTIVE for the invocation, so any profiles:-gated service "
        "(celery-worker-gpu-scaled, celery-worker-gpu-diarize, celery-worker-gpu-"
        "transcribe, keycloak's 'pki' profile, llm-test's 'ollama' profile) started "
        "under a --fresh deployment survives 'fresh-destroy' with its named volumes "
        "intact -- reproduced live with celery-worker-gpu-scaled (issue #711 follow-up)"
    )


def test_fresh_destroy_still_removes_a_stray_volume_as_a_backstop() -> None:
    """The COMPOSE_PROFILES fix addresses containers; the pre-existing 'xargs docker
    volume rm' loop over $vols is the belt-and-suspenders for anything compose still
    doesn't own (e.g. a volume from a since-removed overlay) -- must not regress."""
    body = _fresh_destroy_body()
    assert "docker volume rm" in body, (
        "fresh_destroy() no longer has a fallback volume-removal step -- without both "
        "this fix AND that backstop, a stray named volume from a removed overlay would "
        "have no removal path left at all"
    )
