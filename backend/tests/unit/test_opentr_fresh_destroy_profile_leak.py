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


def test_fresh_destroy_removes_project_scoped_image_tags() -> None:
    """The third thing a --fresh deployment owns, after containers and volumes.

    PR #759 made a ``--fresh`` build write ``opentranscribe-*:otfresh-<name>`` instead
    of clobbering the shared ``:latest`` -- which fixed cross-contamination but created
    an accumulation: nothing reclaimed those tags, so every fresh deployment that ever
    built left multi-GB images behind permanently. Observed after the xcard711 teardown:
    ``opentranscribe-backend:otfresh-xcard711`` still present with containers, volumes
    and generated files all correctly gone.
    """
    body = _fresh_destroy_body()
    assert "docker rmi" in body, (
        "fresh_destroy() never removes the project-scoped image tags a --fresh build "
        "creates (opentranscribe-{backend,frontend,docs}:otfresh-<name>, written because "
        'start_app() exports OT_DEV_IMAGE_TAG="$FRESH_PROJECT" -- PR #759). Without '
        "this, every fresh deployment that built anything leaks its images forever."
    )


def test_fresh_destroy_image_removal_is_scoped_to_this_projects_tag() -> None:
    """The safety property: this must be structurally unable to reach ``:latest``.

    ``fresh-destroy`` promises it touches ONLY the isolated project. An image filter
    that matched on the REPOSITORY (``opentranscribe-backend``) rather than the TAG
    would delete the shared ``:latest`` the main dev stack runs on -- a far worse bug
    than the leak being fixed. Assert the filter is anchored to ``:${proj}``.
    """
    body = _fresh_destroy_body()
    rmi_lines = [ln for ln in body.splitlines() if "docker rmi" in ln or "docker images" in ln]
    assert rmi_lines, "no image-removal lines found in fresh_destroy()"
    joined = "\n".join(rmi_lines)
    assert "reference=*:${proj}" in joined, (
        "fresh_destroy()'s image lookup is not anchored to this project's TAG "
        "(reference=*:${proj}). Matching on the repository instead would make "
        "fresh-destroy delete the shared :latest image the MAIN dev stack runs on."
    )
    assert "latest" not in joined, (
        "fresh_destroy()'s image-removal lines mention 'latest' -- the shared tag must "
        "be unreachable from here by construction, not by a filter that happens to exclude it"
    )


def test_fresh_destroy_lists_image_tags_before_the_confirmation_prompt() -> None:
    """Destroy is the one destructive fresh op; it must show what it will delete.

    The prompt already enumerates containers, volumes, generated files and the
    diar-native export directory. Images are multi-GB and must not be the one category
    removed without the operator seeing it first.
    """
    body = _fresh_destroy_body()
    prompt_idx = body.find("Proceed?")
    assert prompt_idx != -1, "fresh_destroy() no longer has a 'Proceed?' confirmation prompt"
    preamble = body[:prompt_idx]
    assert "image tags to remove" in preamble.lower(), (
        "fresh_destroy() removes project-scoped image tags but does not list them before "
        "the confirmation prompt -- every other destroyed category (containers, volumes, "
        "generated files, the diar-native export dir) is shown first"
    )
