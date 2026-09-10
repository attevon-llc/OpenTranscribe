"""An upgrade must be rehearsable on LOCAL images, before the tag is published.

``docker compose pull`` always contacts the registry — ``pull_policy: never`` does not apply
to an explicit pull — so ``./opentranscribe.sh update --version vX.Y.Z`` aborted whenever
that tag was not on Docker Hub, even with the images sitting on the host.

That made the release rehearsal's upgrade scenario **structurally unpassable**. It exists to
prove an upgrade works BEFORE publishing, so the tag it upgrades to is unpublished by
definition. Measured 2026-09-07, phase 08::

    diar-native Error manifest for davidamacey/opentranscribe-backend:v0.5.0 not found
    [guardrails] ✗ FATAL: opentranscribe.sh update --version failed

The only way to make it pass was to publish first — that is, to publish images before
anything had shown they work, which is precisely what the gate is for.

``compose_pull_for_upgrade`` continues on a pull failure **only** when every image the
compose chain names is present locally, and says loudly which ones it is proceeding with.

⚠️ The tests below therefore come in pairs. It would be easy to "fix" this by ignoring pull
failures, and that version passes the happy-path test while silently starting an upgrade with
missing images. The must-refuse cases are what distinguish the two.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
MANAGER = REPO_ROOT / "opentranscribe.sh"

pytestmark = pytest.mark.skipif(
    not MANAGER.is_file() or shutil.which("bash") is None,
    reason="opentranscribe.sh or bash is not present in this checkout",
)

IMAGES = "davidamacey/opentranscribe-backend:v0.5.0\ndavidamacey/opentranscribe-frontend:v0.5.0"


def _fake_docker(tmp_path: Path, *, pull_ok: bool, present: list[str], config_ok: bool = True):
    """A `docker` stub covering the three subcommands the function uses."""
    bindir = tmp_path / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    images_file = tmp_path / "images.txt"
    images_file.write_text(IMAGES + "\n", encoding="utf-8")
    present_file = tmp_path / "present.txt"
    present_file.write_text("\n".join(present) + "\n", encoding="utf-8")

    script = f"""#!/bin/bash
if [[ "$1" == "compose" ]]; then
    for a in "$@"; do
        if [[ "$a" == "pull" ]]; then
            {"exit 0" if pull_ok else 'echo "manifest unknown" >&2; exit 1'}
        fi
        if [[ "$a" == "--images" ]]; then
            {"cat " + str(images_file) if config_ok else "exit 1"}
            exit 0
        fi
    done
    exit 0
fi
if [[ "$1" == "image" && "$2" == "inspect" ]]; then
    grep -Fxq "$3" "{present_file}" && exit 0
    exit 1
fi
exit 0
"""
    (bindir / "docker").write_text(script, encoding="utf-8")
    (bindir / "docker").chmod(0o755)
    return bindir


def _run(tmp_path: Path, bindir: Path) -> subprocess.CompletedProcess[str]:
    harness = textwrap.dedent(f"""
        set -uo pipefail
        export PATH="{bindir}:$PATH"
        RED=''; YELLOW=''; NC=''
        # Source only the function under test: opentranscribe.sh executes on load.
        eval "$(awk '/^compose_pull_for_upgrade\\(\\) \\{{/,/^\\}}/' "{MANAGER}")"
        if compose_pull_for_upgrade "-f docker-compose.yml"; then
            echo "RESULT=CONTINUED"
        else
            echo "RESULT=REFUSED"
        fi
    """)
    return subprocess.run(
        ["bash", "-c", harness], capture_output=True, text=True, timeout=120, check=False
    )


def test_a_successful_pull_is_the_normal_path(tmp_path: Path):
    """The control: nothing about this change may alter the ordinary online upgrade."""
    result = _run(tmp_path, _fake_docker(tmp_path, pull_ok=True, present=[]))
    assert "RESULT=CONTINUED" in result.stdout, f"{result.stdout}\n{result.stderr}"
    assert "already" not in result.stdout, (
        "the local-image warning fired on a SUCCESSFUL pull; it must only appear when the "
        f"pull actually failed:\n{result.stdout}"
    )


def test_an_unpublished_tag_with_local_images_continues(tmp_path: Path):
    """The case the rehearsal needs, and the reason this function exists."""
    result = _run(
        tmp_path,
        _fake_docker(tmp_path, pull_ok=False, present=IMAGES.splitlines()),
    )
    assert "RESULT=CONTINUED" in result.stdout, (
        "a pull failure with every image present locally still aborted the upgrade, so the "
        f"rehearsal remains unable to test an unpublished release:\n{result.stdout}\n{result.stderr}"
    )


def test_it_names_the_local_images_it_proceeds_with(tmp_path: Path):
    """A silent fallback would make a STALE local image indistinguishable from a fresh pull."""
    result = _run(
        tmp_path,
        _fake_docker(tmp_path, pull_ok=False, present=IMAGES.splitlines()),
    )
    combined = result.stdout + result.stderr
    expected = IMAGES.splitlines()
    # Outside the loop: an empty list would run the body zero times and pass, which is
    # the loop-only shape scripts/audit-tests.py exists to reject.
    assert len(expected) == 2, f"the fixture no longer supplies two images: {expected}"
    for image in expected:
        assert image in combined, (
            f"{image} was used but never named. The operator cannot notice a stale image "
            f"they are not told about:\n{combined}"
        )


def test_a_missing_image_is_still_fatal(tmp_path: Path):
    """The must-refuse case: 'ignore pull failures' passes the happy path and breaks this."""
    result = _run(
        tmp_path,
        # backend present, frontend absent
        _fake_docker(tmp_path, pull_ok=False, present=[IMAGES.splitlines()[0]]),
    )
    assert "RESULT=CONTINUED" not in result.stdout, (
        "the upgrade continued with an image that is neither pullable nor present locally; "
        "it would start a stack with a missing image"
    )
    assert "opentranscribe-frontend" in result.stdout + result.stderr, (
        "refused without naming which image was missing"
    )


def test_an_unreadable_image_list_is_fatal(tmp_path: Path):
    """Fail closed: 'could not check' must not read as 'all present'."""
    result = _run(
        tmp_path,
        _fake_docker(tmp_path, pull_ok=False, present=IMAGES.splitlines(), config_ok=False),
    )
    assert "RESULT=CONTINUED" not in result.stdout, (
        "the image list could not be read, so nothing was verified — continuing there "
        "asserts a local-image guarantee that was never checked"
    )


def test_both_update_call_sites_go_through_the_helper():
    """A helper nothing calls is decoration; there were two bare `compose pull` sites."""
    text = MANAGER.read_text(encoding="utf-8")
    # Exclude the helper's own body: it necessarily contains the very `compose ... pull`
    # this forbids elsewhere. Without this the check flags its own implementation.
    helper = re.search(r"compose_pull_for_upgrade\(\) \{.*?\n\}", text, re.S)
    assert helper, "compose_pull_for_upgrade is gone"
    outside = text.replace(helper.group(0), "")
    bare = [
        line.strip()
        for line in outside.splitlines()
        if "compose $compose_files pull" in line and not line.lstrip().startswith("#")
    ]
    assert not bare, (
        "these upgrade paths still pull directly and will abort on an unpublished tag:\n  "
        + "\n  ".join(bare)
    )
    assert text.count('compose_pull_for_upgrade "$compose_files"') >= 2, (
        "expected both update call sites to route through compose_pull_for_upgrade"
    )
