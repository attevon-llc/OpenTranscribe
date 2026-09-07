"""A rehearsal in local-image mode must run the code under test, not a stale tag.

On 2026-09-07 the rehearsal ran `davidamacey/opentranscribe-backend:v0.5.0` built seven days
and **315 commits** earlier. Both fresh-install and upgrade then failed the same assertion —

    media_file.diarization_provider is NULL after completion

— and it was read as a product bug in the release being cut. It was not. The tree's
provenance plumbing is correct end to end; that *image* contains **zero** occurrences of
``diarization_provider``, because the feature postdates it. The rehearsal was a true
statement about an artifact nobody intended to test.

The scenarios set ``pull_policy: never`` and pin ``OT_IMAGE_TAG``, which means "run whatever
carries this tag on this host". They do not build it — the release pipeline's ``build`` stage
does, earlier in the same run — so running ``rehearse`` alone, or with ``--from`` (which the
ledger explicitly allows), silently rehearses whatever is lying around.

Same shape as ``run-backend-tests.sh --summary`` passing off a two-day-old junit artifact
from a different commit, and it takes the same answer: **missing provenance is a refusal, not
a pass.** An image with no ``org.opencontainers.image.revision`` label cannot be shown to be
the code under test, so it is rejected rather than assumed current.

⚠️ Hub mode (``USE_HUB_IMAGES=true``) is deliberately exempt: there the published artifact is
the subject of the test, so a differing revision is the point rather than a defect.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
GUARDRAILS = REPO_ROOT / "scripts" / "release-tests" / "lib" / "guardrails.sh"
FRESH_INSTALL = REPO_ROOT / "scripts" / "release-tests" / "test-fresh-install.sh"

pytestmark = pytest.mark.skipif(
    not GUARDRAILS.is_file() or shutil.which("bash") is None,
    reason="scripts/release-tests/lib/guardrails.sh or bash is not present in this checkout",
)

_FAKE_SHA_A = "a" * 40
_FAKE_SHA_B = "b" * 40


def _drive(body: str, env_extra: str = "") -> subprocess.CompletedProcess[str]:
    """Run the REAL guardrail function against a fake `docker` on PATH.

    A fake docker, not a real image: the check must be provable without building a
    multi-GB artifact, and the three cases below (match / mismatch / no label) are about
    what the label SAYS, which a stub can state exactly.
    """
    harness = textwrap.dedent(f"""
        set -uo pipefail
        export TEST_SCENARIO=probe TEST_PROJECT_NAME=ot-reltest-probe
        export TEST_ROOT=/tmp/ot-reltest-probe
        export TEST_LABEL=com.opentranscribe.release-test=probe
        export REPO_ROOT="{REPO_ROOT}"
        {env_extra}
        source "{GUARDRAILS}"
        {body}
    """)
    return subprocess.run(
        ["bash", "-c", harness], capture_output=True, text=True, timeout=120, check=False
    )


def _with_fake_docker(tmp_path: Path, label_value: str | None, inspect_ok: bool = True) -> str:
    """Emit shell that puts a fake `docker` first on PATH."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir(parents=True, exist_ok=True)
    if not inspect_ok:
        body = 'echo "no such image" >&2; exit 1\n'
    elif label_value is None:
        body = "echo '<no value>'\n"
    else:
        body = f"echo '{label_value}'\n"
    (bindir / "docker").write_text(f"#!/bin/bash\n{body}", encoding="utf-8")
    (bindir / "docker").chmod(0o755)
    return f'export PATH="{bindir}:$PATH"'


def _call(image: str = "some/image:vX") -> str:
    return (
        f'if gr_assert_image_is_the_code_under_test "{image}" "{_FAKE_SHA_A}"; '
        f'then echo "RESULT=ACCEPTED"; else echo "RESULT=REFUSED"; fi'
    )


def test_an_image_built_from_the_code_under_test_is_accepted(tmp_path: Path):
    """The control. Without it, "refuse everything" would satisfy every case below."""
    result = _drive(_call(), _with_fake_docker(tmp_path, _FAKE_SHA_A))
    assert "RESULT=ACCEPTED" in result.stdout, (
        f"a matching revision was refused — the check would block every release:\n"
        f"{result.stdout}\n{result.stderr}"
    )


def test_an_image_from_a_different_commit_is_refused(tmp_path: Path):
    result = _drive(_call(), _with_fake_docker(tmp_path, _FAKE_SHA_B))
    assert "RESULT=ACCEPTED" not in result.stdout, (
        "an image built from a different commit was accepted — this is exactly the "
        "315-commit-stale rehearsal whose missing features were filed as product bugs"
    )
    assert "not " in result.stderr or "was built from" in result.stderr, (
        f"refused without naming the mismatch, which is what made it undiagnosable:\n"
        f"{result.stderr}"
    )


def test_an_unlabelled_image_is_refused_rather_than_assumed_current(tmp_path: Path):
    """Fail closed: "cannot tell" must not read as "fine"."""
    result = _drive(_call(), _with_fake_docker(tmp_path, None))
    assert "RESULT=ACCEPTED" not in result.stdout, (
        "an image carrying no revision label was accepted. It cannot be shown to be the "
        "code under test, and an unattributable artifact is not evidence about a release."
    )


def test_an_uninspectable_image_is_refused(tmp_path: Path):
    result = _drive(
        _call(),
        _with_fake_docker(tmp_path, None, inspect_ok=False),
    )
    assert "RESULT=ACCEPTED" not in result.stdout, (
        "a missing/uninspectable image was accepted; the scenario would then start with "
        "pull_policy=never and fail later for an unrelated-looking reason"
    )


def test_an_empty_expected_commit_is_refused(tmp_path: Path):
    """`git rev-parse` failing must not silently disable the check.

    The call site passes `$(git rev-parse HEAD || true)`, so an empty string is reachable —
    and "no expected value" must never compare equal to everything.
    """
    result = _drive(
        'if gr_assert_image_is_the_code_under_test "some/image:vX" ""; '
        'then echo "RESULT=ACCEPTED"; else echo "RESULT=REFUSED"; fi',
        _with_fake_docker(tmp_path, _FAKE_SHA_A),
    )
    assert "RESULT=ACCEPTED" not in result.stdout, (
        "an empty expected commit was accepted — the guard would be silently off wherever "
        "git could not answer, which includes a tarball checkout"
    )


def test_the_explicit_override_exists_and_is_loud(tmp_path: Path):
    """A gate with no deliberate escape hatch gets bypassed by deleting the gate."""
    result = _drive(
        _call(),
        _with_fake_docker(tmp_path, _FAKE_SHA_B) + "\nexport OT_RELEASE_TEST_ALLOW_STALE_IMAGE=1",
    )
    assert "RESULT=ACCEPTED" in result.stdout, (
        "OT_RELEASE_TEST_ALLOW_STALE_IMAGE=1 did not permit the stale image, so there is no "
        "deliberate way to rehearse an older artifact on purpose"
    )
    assert "NOT" in result.stderr or "older artifact" in result.stderr, (
        f"the override passed silently; it must say every result describes the older "
        f"artifact:\n{result.stderr}"
    )


# ------------------------------------------------------------------ wiring, not just logic


def test_the_local_image_branch_actually_calls_the_check():
    """A guardrail nothing calls is decoration — that is how this got missed for 315 commits."""
    if not FRESH_INSTALL.is_file():
        pytest.skip("test-fresh-install.sh is not in this checkout")
    source = FRESH_INSTALL.read_text(encoding="utf-8")
    assert "gr_assert_image_is_the_code_under_test" in source, (
        "test-fresh-install.sh never calls gr_assert_image_is_the_code_under_test, so "
        "pull_policy=never still runs whatever image happens to carry the tag"
    )


def test_the_check_is_not_applied_to_hub_mode():
    """Hub mode's whole point is testing the PUBLISHED artifact.

    Asserting a revision match there would refuse every legitimate hub-mode run, so the
    call must sit in the local-image branch only.
    """
    if not FRESH_INSTALL.is_file():
        pytest.skip("test-fresh-install.sh is not in this checkout")
    source = FRESH_INSTALL.read_text(encoding="utf-8")
    hub_branch = re.search(
        r'if \[\[ "\$USE_HUB_IMAGES" == "true" \]\]; then(?P<hub>.*?)\n    else',
        source,
        re.S,
    )
    assert hub_branch, "the USE_HUB_IMAGES branch has moved; re-point this guard"
    assert "gr_assert_image_is_the_code_under_test" not in hub_branch.group("hub"), (
        "the revision check is inside the Docker Hub branch. There the published image IS "
        "the subject of the test and its revision legitimately differs, so this would "
        "refuse every hub-mode rehearsal."
    )
