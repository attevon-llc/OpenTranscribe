"""The installer's release download path and the update config-ref resolution."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.unit._shell_extract import INSTALLER
from tests.unit._shell_extract import MANAGER
from tests.unit._shell_extract import REPO_ROOT
from tests.unit._shell_extract import extract_function
from tests.unit._shell_extract import requires_install_scripts

pytestmark = requires_install_scripts


# --------------------------------------------------------------------------- #
# Install-path compatibility across releases (issue #683)
#
# A self-hosted install has TWO sources and only one of them is pinned: the
# installer is served from the default branch (the docs one-liner hardcodes it),
# while everything it downloads comes from the resolved release tag. The installer
# is therefore always NEWER than the release it installs, and it must stay able to
# install every release resolve_install_ref() can hand it.
#
# release-manifest.txt was added after v0.4.1 shipped. The new installer asked that
# tag for a file it had never heard of, 404ed, and fail-closed — killing 100% of
# `curl | bash` installs for 22 days. Nothing caught it, because every other check
# validates a tag against its OWN checkout and so cannot see a cross-ref break.
#
# scripts/verify-install-paths.sh is the live network gate. These are the hermetic
# half: no network, so they run in CI and in the fast suite.
# --------------------------------------------------------------------------- #

_PINNED_TAG = "v0.4.1"


def _curl_stub(bin_dir: Path, requested: Path, *, manifest_at: set[str]) -> None:
    """A curl that 404s like GitHub does.

    ``manifest_at`` is the set of refs that actually HAVE release-manifest.txt, so a
    test can reproduce the exact shape of #683: present on the default branch, absent
    on the pinned tag.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    serve = "\n".join(f"    */{ref}/release-manifest.txt) ok=1 ;;" for ref in sorted(manifest_at))
    stub = bin_dir / "curl"
    stub.write_text(
        "#!/bin/bash\n"
        "url=''; out=''\n"
        'while [ $# -gt 0 ]; do case "$1" in\n'
        '  -o) out="$2"; shift 2 ;;\n'
        '  http*) url="$1"; shift ;;\n'
        "  *) shift ;;\n"
        "esac; done\n"
        f'echo "$url" >> {requested}\n'
        # The repo API call feeds resolve_default_branch; it writes to stdout, not -o.
        'case "$url" in\n'
        '  *api.github.com/repos/*/OpenTranscribe) printf \'{"default_branch": "master"}\\n\'; exit 0 ;;\n'
        "esac\n"
        "ok=0\n"
        'case "$url" in\n'
        f"{serve}\n"
        "    *release-manifest.txt) ok=0 ;;\n"
        "    *) ok=1 ;;\n"
        "esac\n"
        '[ "$ok" = 1 ] || exit 22\n'
        'if [ -n "$out" ]; then\n'
        '  case "$url" in\n'
        f'    *release-manifest.txt) cp {REPO_ROOT / "release-manifest.txt"} "$out" ;;\n'
        '    *) printf "stub-content\\n" > "$out" ;;\n'
        "  esac\n"
        "fi\n"
        "exit 0\n"
    )
    stub.chmod(0o755)


def _run_installer_download(tmp_path: Path, *, manifest_at: set[str], ref: str = _PINNED_TAG):
    bin_dir = tmp_path / "bin"
    requested = tmp_path / "requested.txt"
    requested.write_text("")
    _curl_stub(bin_dir, requested, manifest_at=manifest_at)

    workdir = tmp_path / "install"
    workdir.mkdir()

    snippet = (
        "set -uo pipefail\n"
        "RED=''; GREEN=''; YELLOW=''; BLUE=''; NC=''\n"
        + extract_function(INSTALLER, "resolve_default_branch")
        + extract_function(INSTALLER, "download_release_manifest_artifacts")
        + "\ndownload_release_manifest_artifacts\n"
    )
    proc = subprocess.run(
        ["bash", "-c", snippet],
        capture_output=True,
        text=True,
        cwd=str(workdir),
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "OPENTRANSCRIBE_BRANCH": ref},
    )
    return proc, workdir, requested.read_text().splitlines()


def test_installer_installs_a_release_that_predates_the_manifest(tmp_path: Path):
    """THE #683 regression test: v0.4.1 has no release-manifest.txt, master does.

    Watched fail against the pre-fix installer, which exits 1 here with
    "Refusing to install from an unknown artifact list."
    """
    proc, workdir, _ = _run_installer_download(tmp_path, manifest_at={"master"})

    assert proc.returncode == 0, (
        "a release predating release-manifest.txt must still install:\n"
        f"{proc.stdout}\n{proc.stderr}"
    )
    assert (workdir / "docker-compose.yml").is_file(), "no compose file was written"
    assert "predates release-manifest.txt" in proc.stdout, (
        "the fallback must announce itself — a silent one hides which list was used"
    )


def test_manifest_fallback_never_unpins_the_artifacts(tmp_path: Path):
    """Only the file LIST may fall back. Every artifact must come from the pinned ref.

    If the fallback pulled artifacts from the default branch too, this would still
    "install" — while silently shipping tip-of-development config against a pinned
    release's images. That is the exact failure pinning exists to prevent, and an
    existence-only assertion cannot see it.
    """
    _, _, urls = _run_installer_download(tmp_path, manifest_at={"master"})

    artifact_urls = [u for u in urls if "release-manifest.txt" not in u and "api.github" not in u]
    assert artifact_urls, "no artifacts were requested at all"

    unpinned = [u for u in artifact_urls if f"/{_PINNED_TAG}/" not in u]
    assert not unpinned, (
        f"artifacts fetched from somewhere other than {_PINNED_TAG} — install is unpinned:\n"
        + "\n".join(unpinned[:5])
    )


def test_installer_still_fails_closed_when_no_manifest_exists_anywhere(tmp_path: Path):
    """The fallback must not become a guess.

    Guessing the artifact list is the bug release-manifest.txt replaced (#640), so with
    no manifest reachable at all the installer must still refuse rather than assume.
    """
    proc, workdir, _ = _run_installer_download(tmp_path, manifest_at=set())

    assert proc.returncode != 0, "no manifest anywhere must be fatal, not a guessed list"
    assert "Refusing to install from an unknown artifact list" in proc.stdout
    assert not (workdir / "docker-compose.yml").exists(), "nothing may be written on refusal"


def _resolve_config_ref(tmp_path: Path, env_body: str, *, offline: bool = False):
    """Run opentranscribe.sh's real resolve_config_ref() against a fake .env."""
    (tmp_path / ".env").write_text(env_body)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # resolve_default_branch is the only curl caller here; `offline` makes it fail.
    (bin_dir / "curl").write_text(
        "#!/bin/bash\nexit 7\n"
        if offline
        else '#!/bin/bash\nprintf \'{"default_branch": "master"}\\n\'\nexit 0\n'
    )
    (bin_dir / "curl").chmod(0o755)

    snippet = (
        "set -uo pipefail\n"
        "RED=''; GREEN=''; YELLOW=''; BLUE=''; NC=''\n"
        + extract_function(MANAGER, "read_env_value")
        + extract_function(MANAGER, "resolve_default_branch")
        + extract_function(MANAGER, "deployment_ref")
        + extract_function(MANAGER, "resolve_config_ref")
        + "\nunset OPENTRANSCRIBE_BRANCH\nresolve_config_ref\n"
    )
    return subprocess.run(
        ["bash", "-c", snippet],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
    )


def test_update_full_takes_config_from_the_pinned_release(tmp_path: Path):
    """update-full defaulted to master, so a pinned install re-downloaded tip config.

    It did not 404 — it silently merged newer compose files onto older images. Service
    definitions live in docker-compose.yml, so the base file could reference services
    the pinned containers know nothing about. Silent is worse than broken.
    """
    proc = _resolve_config_ref(tmp_path, f"OT_IMAGE_TAG={_PINNED_TAG}\n")

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == _PINNED_TAG, (
        f"update-full on a {_PINNED_TAG} install took config from "
        f"{proc.stdout.strip()!r} — config/image mismatch"
    )


@pytest.mark.parametrize(
    "env_body,label",
    [("FOO=bar\n", "no OT_IMAGE_TAG at all"), ("OT_IMAGE_TAG=latest\n", "pinned to 'latest'")],
)
def test_update_full_still_works_on_a_pre_pinning_install(tmp_path: Path, env_body, label):
    """Backward compatibility: installs predating OT_IMAGE_TAG must keep updating.

    They may fall back to the default branch — but must SAY so. Falling back silently
    is how an unpinned install stops being visible as one.
    """
    proc = _resolve_config_ref(tmp_path, env_body)

    assert proc.returncode == 0, f"an install with {label} must still update:\n{proc.stderr}"
    assert proc.stdout.strip() == "master"
    assert "not pinned to a release" in proc.stderr, (
        f"an install with {label} fell back to the tip without warning"
    )


def test_update_full_refuses_rather_than_guessing_a_branch_name(tmp_path: Path):
    """No hardcoded branch name anywhere: unresolvable means stop, not assume 'master'.

    This is what lets the branch be renamed without breaking installs — the whole
    point of resolving it at runtime instead of writing it down.
    """
    proc = _resolve_config_ref(tmp_path, "FOO=bar\n", offline=True)

    assert proc.returncode != 0, "an unresolvable default branch must not be guessed"
    assert "not pinned to a release" in proc.stderr
    assert "--version vX.Y.Z" in proc.stderr, "the failure must tell the user how to proceed"


def test_no_hardcoded_default_branch_in_the_install_download_paths():
    """The literal 'master' must not reappear in a download URL.

    Every hardcoded branch in a fetch URL is a rename waiting to break installs. The
    fallback resolves the default branch from the API precisely so `master` -> `main`
    is a non-event; a literal would quietly defeat that.
    """
    offenders = []
    for script in (INSTALLER, MANAGER):
        for num, line in enumerate(script.read_text().splitlines(), 1):
            if "raw.githubusercontent.com" not in line or line.lstrip().startswith("#"):
                continue
            if "/master" in line or "/main" in line:
                offenders.append(f"{script.name}:{num}: {line.strip()}")

    assert not offenders, (
        "a branch name is hardcoded into a download URL — resolve it at runtime instead:\n"
        + "\n".join(offenders)
    )
