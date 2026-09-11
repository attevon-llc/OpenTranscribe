"""``cp_pin_image_tag`` must not split a compose interpolation expression on its first colon.

Every backend service in ``docker-compose.prod.yml`` declares:

    image: ${BACKEND_IMAGE:-davidamacey/opentranscribe-backend:${OT_IMAGE_TAG:-latest}}

The first colon belongs to ``:-``, so ``image.split(":", 1)[0]`` returned ``${BACKEND_IMAGE``
and the helper wrote::

    image: ${BACKEND_IMAGE:v0.5.0

which is not a parseable expression. The whole stack then died at ``compose up``:

    invalid interpolation format for services.celery-worker.image

⚠️ **This was latent, not new.** ``lite-mode`` could not build an image at all (a global
``ARG TARGETARCH`` shadowed BuildKit's built-in), so the scenario never reached ``compose up``
to hit it. Fixing the build is what exposed it — a reminder that a scenario failing early
hides everything downstream of the failure, and that "it used to pass" can mean "it used to
stop sooner".

The fix resolves the repository out of the ``${VAR:-default}`` form and **refuses** when it
cannot, rather than emitting a malformed image string that fails several phases later with a
message pointing at compose instead of at this function.
"""

from __future__ import annotations

import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parents[3]
COMPOSE_PATCH = REPO_ROOT / "scripts" / "release-tests" / "lib" / "compose-patch.sh"
PROD_COMPOSE = REPO_ROOT / "docker-compose.prod.yml"
LITE_COMPOSE = REPO_ROOT / "docker-compose.lite.yml"

pytestmark = pytest.mark.skipif(
    not COMPOSE_PATCH.is_file() or shutil.which("bash") is None,
    reason="scripts/release-tests/lib/compose-patch.sh or bash is not present in this checkout",
)

INTERPOLATED = "${BACKEND_IMAGE:-davidamacey/opentranscribe-backend:${OT_IMAGE_TAG:-latest}}"


def _pin(compose_text: str, service: str, tag: str, tmp_path: Path):
    target = tmp_path / "docker-compose.yml"
    target.write_text(compose_text, encoding="utf-8")
    result = subprocess.run(
        [
            "bash",
            "-c",
            textwrap.dedent(f"""
                set -uo pipefail
                source "{COMPOSE_PATCH}"
                cp_pin_image_tag "{target}" "{service}" "{tag}"
            """),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    return result, target


def test_an_interpolated_image_is_pinned_to_the_right_repository(tmp_path: Path):
    text = f"services:\n  celery-worker:\n    image: '{INTERPOLATED}'\n"
    result, target = _pin(text, "celery-worker", "v0.5.0", tmp_path)
    assert result.returncode == 0, f"cp_pin_image_tag failed:\n{result.stderr}"

    pinned = yaml.safe_load(target.read_text(encoding="utf-8"))["services"]["celery-worker"][
        "image"
    ]
    assert pinned == "davidamacey/opentranscribe-backend:v0.5.0", (
        f"expected the repo from inside the ${{VAR:-default}} form; got {pinned!r}"
    )
    assert "${" not in pinned, (
        f"the pinned image still contains an interpolation fragment ({pinned!r}) — this is "
        "the `${BACKEND_IMAGE:v0.5.0` shape that fails compose up"
    )


def test_the_old_naive_split_really_did_produce_the_broken_value():
    """The must-fire control, stated as the arithmetic rather than trusting the story.

    Without this, the test above could pass for reasons unrelated to the bug it documents.
    """
    naive = INTERPOLATED.split(":", 1)[0] + ":v0.5.0"
    assert naive == "${BACKEND_IMAGE:v0.5.0", (
        "the naive split no longer reproduces the malformed value this module exists for; "
        f"got {naive!r} — re-check the premise before trusting the fix"
    )


def test_a_plain_image_is_still_pinned_normally(tmp_path: Path):
    """Regression guard: the non-interpolated form is the common case and must be untouched."""
    text = "services:\n  backend:\n    image: davidamacey/opentranscribe-backend:v0.3.3\n"
    result, target = _pin(text, "backend", "v0.5.0", tmp_path)
    assert result.returncode == 0, result.stderr
    pinned = yaml.safe_load(target.read_text(encoding="utf-8"))["services"]["backend"]["image"]
    assert pinned == "davidamacey/opentranscribe-backend:v0.5.0"


def test_an_unresolvable_expression_is_refused_not_mangled(tmp_path: Path):
    """Fail closed: a malformed write surfaces phases later, blamed on compose."""
    text = "services:\n  backend:\n    image: '${SOME_IMAGE}'\n"
    result, target = _pin(text, "backend", "v0.5.0", tmp_path)
    assert result.returncode != 0, (
        "an image expression with no resolvable repository was accepted; the helper would "
        "write a broken value that fails at `compose up`, far from its cause"
    )
    assert "${SOME_IMAGE}" in target.read_text(encoding="utf-8"), (
        "the file was rewritten despite the refusal — a refusal must not leave a partial edit"
    )


def test_the_real_prod_compose_still_uses_the_form_this_handles():
    """Prove the premise: if prod.yml stops interpolating, this guard is about nothing."""
    if not PROD_COMPOSE.is_file():
        pytest.skip("docker-compose.prod.yml is not in this checkout")
    text = PROD_COMPOSE.read_text(encoding="utf-8")
    assert "${BACKEND_IMAGE:-" in text, (
        "docker-compose.prod.yml no longer declares images as ${BACKEND_IMAGE:-...}; this "
        "module (and cp_pin_image_tag's interpolation handling) should be re-checked"
    )


def test_a_literal_repo_with_an_interpolated_tag_is_pinned_not_refused(tmp_path: Path):
    """The third shape, and the one the first fix got wrong.

    `davidamacey/opentranscribe-frontend:${OT_IMAGE_TAG:-latest}` has a LITERAL repository
    and an interpolated tag only. Keying the unwrap on "contains ${" rather than "starts
    with ${" made the helper refuse every frontend/docs service, which failed the lite-mode
    scenario at phase 03 — an over-strict guard, caught by the rehearsal on its next run.
    """
    text = "services:\n  frontend:\n    image: 'davidamacey/opentranscribe-frontend:${OT_IMAGE_TAG:-latest}'\n"
    result, target = _pin(text, "frontend", "v0.5.0", tmp_path)
    assert result.returncode == 0, (
        f"a literal repo with an interpolated tag was refused:\n{result.stderr}"
    )
    pinned = yaml.safe_load(target.read_text(encoding="utf-8"))["services"]["frontend"]["image"]
    assert pinned == "davidamacey/opentranscribe-frontend:v0.5.0", (
        f"expected the literal repository to survive; got {pinned!r}"
    )


def test_the_real_prod_compose_still_uses_the_literal_repo_form_too():
    """Prove the second premise as well, so neither branch of repo_of goes untested."""
    if not PROD_COMPOSE.is_file():
        pytest.skip("docker-compose.prod.yml is not in this checkout")
    text = PROD_COMPOSE.read_text(encoding="utf-8")
    assert "opentranscribe-frontend:${OT_IMAGE_TAG" in text, (
        "docker-compose.prod.yml no longer has a literal-repo/interpolated-tag image; the "
        "second branch of repo_of is now untested by this module"
    )


# --- Issue #895: docker-compose.lite.yml used to hardcode `:latest` and ignore ---
# --- OT_IMAGE_TAG entirely, so a lite install could not be pinned/upgraded/rolled ---
# --- back by release at all. Fixed to use the SAME `${VAR:-repo:${OT_IMAGE_TAG:-latest}}` ---
# --- shape prod already used, which is exactly the shape cp_pin_image_tag exists for. ---


def test_a_lite_interpolated_image_is_pinned_to_the_right_repository(tmp_path: Path):
    lite_interpolated = (
        "${BACKEND_LITE_IMAGE:-davidamacey/opentranscribe-backend-lite:${OT_IMAGE_TAG:-latest}}"
    )
    text = f"services:\n  backend:\n    image: '{lite_interpolated}'\n"
    result, target = _pin(text, "backend", "v0.5.0", tmp_path)
    assert result.returncode == 0, f"cp_pin_image_tag failed:\n{result.stderr}"

    pinned = yaml.safe_load(target.read_text(encoding="utf-8"))["services"]["backend"]["image"]
    assert pinned == "davidamacey/opentranscribe-backend-lite:v0.5.0", (
        f"expected the lite repo from inside the ${{VAR:-default}} form; got {pinned!r}"
    )
    assert "${" not in pinned, (
        f"the pinned lite image still contains an interpolation fragment ({pinned!r})"
    )


def test_the_real_lite_compose_now_uses_the_same_interpolated_form():
    """Prove the fix landed, not just that cp_pin_image_tag CAN parse the shape.

    Before issue #895's fix, every service in docker-compose.lite.yml hardcoded
    `${BACKEND_LITE_IMAGE:-davidamacey/opentranscribe-backend-lite:latest}` — no
    `${OT_IMAGE_TAG:-...}` anywhere, so no `OT_IMAGE_TAG` pin could ever reach a lite
    deployment. This asserts the shipped file, not a synthetic string, actually
    carries the fix — a regression here means the bug is back regardless of whether
    cp_pin_image_tag itself still parses the old OR new form correctly.
    """
    if not LITE_COMPOSE.is_file():
        pytest.skip("docker-compose.lite.yml is not in this checkout")
    text = LITE_COMPOSE.read_text(encoding="utf-8")
    image_lines = [line for line in text.splitlines() if "image:" in line]
    assert image_lines, "docker-compose.lite.yml has no image: lines to check"
    for line in image_lines:
        assert "${OT_IMAGE_TAG" in line, (
            f"docker-compose.lite.yml still hardcodes a tag and ignores OT_IMAGE_TAG "
            f"(issue #895 regression): {line!r}"
        )


def test_no_shipped_overlay_hardcodes_an_image_tag():
    """Repo-wide guard (the acceptance criterion issue #895 itself asks for): a
    hardcoded tag on ANY service in ANY shipped docker-compose*.yml is exactly how
    lite silently drifted out of sync with prod's OT_IMAGE_TAG pinning — prod was
    fixed in isolation and nothing compared the two. Every `image:` line that names
    a `davidamacey/opentranscribe-*` repository must resolve its tag through
    `${OT_IMAGE_TAG:-...}` (directly, or nested inside an outer `${VAR:-...}`
    override) rather than a bare literal tag.
    """
    # docker-compose.offline.yml is DELIBERATELY not pinned with ${OT_IMAGE_TAG} in
    # the repo template — its own header comment explains why: an air-gapped install
    # only ever has the `:latest`-tagged images `docker load` put there, so
    # interpolating OT_IMAGE_TAG here would make compose look for a tag the package
    # never loaded. build-offline-package.sh rewrites the tag in the PACKAGED COPY at
    # build time instead; this repo template is never the thing actually deployed.
    exempt_files = {"docker-compose.offline.yml"}

    offenders: dict[str, list[str]] = {}
    for compose_file in sorted(REPO_ROOT.glob("docker-compose*.yml")):
        if compose_file.name in exempt_files:
            continue
        for line in compose_file.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped.startswith("image:"):
                continue
            if "davidamacey/opentranscribe" not in stripped:
                continue
            if "${OT_IMAGE_TAG" in stripped:
                continue
            # `:blackwell` is a deliberate, permanently-floating tag (never part of
            # the vX.Y.Z release scheme — see opentranscribe.sh's
            # pin_diar_native_image_for_blackwell docstring), not a regression of
            # this kind.
            if ":blackwell" in stripped or ":${OT_BLACKWELL_IMAGE_TAG" in stripped:
                continue
            offenders.setdefault(compose_file.name, []).append(stripped)

    assert not offenders, (
        "these shipped overlay image lines hardcode a tag instead of resolving "
        f"OT_IMAGE_TAG: {offenders}"
    )
