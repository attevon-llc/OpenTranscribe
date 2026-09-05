"""release-manifest.txt must stay truthful.

The manifest is the single list of files a self-hosted deployment downloads. It
replaced two hardcoded lists (one in setup-opentranscribe.sh, one in
opentranscribe.sh's update-full) that had silently drifted apart in both
directions — see the manifest's own header for the two production bugs that
caused.

Consolidating only helps if the manifest itself stays correct, so:

* every listed path must exist in the repo (a typo means a 404 at install time,
  for every user, on the pinned tag)
* every compose overlay opentranscribe.sh can select must be listed (that is the
  exact bug that shipped: get_compose_files() picks docker-compose.blackwell.yml
  on SM_12x hardware, the installer never downloaded it, and the `[ -f ]` guard
  turned it into a silent fallback to the wrong image)
* both consumers must actually read the manifest rather than growing a new
  hardcoded list beside it
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
MANIFEST = REPO_ROOT / "release-manifest.txt"
MANAGER = REPO_ROOT / "opentranscribe.sh"
INSTALLER = REPO_ROOT / "setup-opentranscribe.sh"
ENV_EXAMPLE = REPO_ROOT / ".env.example"

pytestmark = pytest.mark.skipif(
    not MANIFEST.exists(), reason="release-manifest.txt not present in this checkout"
)


def _entries() -> list[tuple[str, set[str]]]:
    """Parse the manifest the same way the shell consumers do."""
    entries: list[tuple[str, set[str]]] = []
    for raw in MANIFEST.read_text(encoding="utf-8").splitlines():
        line = raw.rstrip("\n")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = line.split("\t")
        path = parts[0].strip()
        flags = set()
        if len(parts) > 1 and parts[1].strip():
            flags = {f.strip() for f in parts[1].split(",") if f.strip()}
        entries.append((path, flags))
    return entries


def test_manifest_is_not_empty():
    assert _entries(), "release-manifest.txt parsed to zero entries"


def test_every_listed_path_exists():
    """A path that does not exist 404s for every user installing that tag."""
    missing = [path for path, _ in _entries() if not (REPO_ROOT / path).exists()]
    assert not missing, f"release-manifest.txt lists paths that do not exist: {missing}"


def test_flags_are_known():
    known = {"optional", "exec", "preserve"}
    bad = {path: sorted(flags - known) for path, flags in _entries() if not flags <= known}
    assert not bad, f"unknown manifest flags (consumers ignore these silently): {bad}"


def test_base_compose_is_listed_and_required():
    """docker-compose.yml carries the service definitions every overlay merges onto.

    Omitting it from the upgrade path is what made celery-redaction start with the
    image's default CMD instead of the redaction worker.
    """
    entries = dict(_entries())
    assert "docker-compose.yml" in entries, "base compose file missing from the manifest"
    assert "optional" not in entries["docker-compose.yml"], (
        "docker-compose.yml must never be optional — overlays merge onto it"
    )


def test_every_selectable_compose_overlay_is_listed():
    """Any overlay get_compose_files() can choose must be downloadable.

    The `[ -f overlay ]` guards mean a missing overlay degrades silently rather
    than erroring, so this test is the only thing that catches the omission.
    """
    listed = {path for path, _ in _entries()}

    # Comments are prose, not selection logic. opentranscribe.sh explains in a
    # comment that docker-compose.offline.yml sets HF_HUB_OFFLINE=1 — that file is
    # shipped inside the offline package and is never downloaded, so matching it
    # made this test fail on a documentation change.
    code = "\n".join(
        line for line in MANAGER.read_text().splitlines() if not line.lstrip().startswith("#")
    )
    referenced = set(re.findall(r"docker-compose[a-z0-9.-]*\.yml", code))

    unlisted = sorted(referenced - listed)
    assert not unlisted, (
        f"opentranscribe.sh can select {unlisted} but the manifest does not list "
        "them, so a deployment may never download them"
    )


def test_update_full_reads_the_manifest():
    """Guard against someone reintroducing a hardcoded download list."""
    text = MANAGER.read_text()
    assert "release-manifest.txt" in text, (
        "opentranscribe.sh no longer references release-manifest.txt — "
        "did update-full grow its own artifact list again?"
    )


def test_installer_reads_the_manifest():
    """The FRESH-INSTALL half of the same guard (issue #640).

    The manifest header has always claimed setup-opentranscribe.sh as a consumer, but
    the installer kept its own hardcoded list of compose files instead, and nothing
    enforced the claim — so `docker-compose.blackwell.yml` and `docker-compose.backup.yml`
    were listed here, downloaded by `update-full`, and never by a fresh install.
    """
    text = INSTALLER.read_text()
    assert "release-manifest.txt" in text, (
        "setup-opentranscribe.sh no longer references release-manifest.txt — "
        "did the installer grow its own artifact list again?"
    )


def _extract_function(script: Path, name: str) -> str:
    """Pull one shell function out of a script so it can be run in isolation."""
    out = subprocess.run(
        ["sed", "-n", f"/^{name}()/,/^}}/p", str(script)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert out.strip(), f"{name}() not found in {script.name}"
    return out


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_a_fresh_install_downloads_every_compose_overlay_the_manifest_lists(tmp_path):
    """Run the installer's REAL download loop against a stub curl and check what it asks for.

    This is the behavioural half of the guard, and the one that would actually have
    caught #640: a string-presence check passes the moment the manifest is mentioned
    anywhere, even with the hardcoded list still doing the real work.

    Why it matters that blackwell is in here specifically: get_compose_files() selects
    the overlay with `if is_blackwell_gpu && [ -f docker-compose.blackwell.yml ]`, so a
    file that was never downloaded does not error — it silently falls through to the
    generic GPU overlay, whose image crashes in NVRTC on SM_121 the first time anyone
    actually transcribes something (docs/BLACKWELL_SETUP.md).
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    requested = tmp_path / "requested.txt"

    # Stub curl: record the URL, write a non-empty file to the -o target.
    stub = bin_dir / "curl"
    stub.write_text(
        "#!/bin/bash\n"
        "url=''; out=''\n"
        "while [ $# -gt 0 ]; do\n"
        '  case "$1" in\n'
        '    -o) out="$2"; shift 2 ;;\n'
        '    http*) url="$1"; shift ;;\n'
        "    *) shift ;;\n"
        "  esac\n"
        "done\n"
        f'echo "$url" >> {requested}\n'
        'if [ -n "$out" ]; then\n'
        # The manifest fetch must return the real manifest; anything else is a stub body.
        '  case "$url" in\n'
        f'    *release-manifest.txt) cp {MANIFEST} "$out" ;;\n'
        '    *) printf "stub-content\\n" > "$out" ;;\n'
        "  esac\n"
        "fi\n"
        "exit 0\n"
    )
    stub.chmod(0o755)

    workdir = tmp_path / "install"
    workdir.mkdir()

    snippet = (
        "set -uo pipefail\nset -e\n"
        "RED=''; GREEN=''; YELLOW=''; BLUE=''; NC=''\n"
        + _extract_function(INSTALLER, "download_release_manifest_artifacts")
        + "\ndownload_release_manifest_artifacts\n"
    )
    proc = subprocess.run(
        ["bash", "-c", snippet],
        capture_output=True,
        text=True,
        cwd=str(workdir),
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "OPENTRANSCRIBE_BRANCH": "master"},
    )
    assert proc.returncode == 0, f"replay loop failed:\n{proc.stdout}\n{proc.stderr}"

    asked_for = requested.read_text().splitlines()
    listed = [path for path, _ in _entries()]

    missing = [p for p in listed if not any(u.endswith("/" + p) for u in asked_for)]
    assert not missing, (
        f"a fresh install never downloads {missing} — the manifest lists them, so "
        "get_compose_files() can select a file that is not on disk"
    )

    # The two the hardcoded list forgot, named explicitly so a regression is unambiguous.
    for overlay in ("docker-compose.blackwell.yml", "docker-compose.backup.yml"):
        assert (workdir / overlay).is_file(), f"{overlay} was not written to the install dir"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_a_pinned_install_survives_a_required_entry_the_tag_predates(tmp_path):
    """issue #723 — a REQUIRED manifest entry added TODAY must not retroactively break the
    install of an ALREADY PUBLISHED release.

    `download_release_manifest_artifacts()` fetches the manifest from the pinned ref first,
    but falls back to the default branch when that ref predates release-manifest.txt
    entirely (issue #683). That fallback list is then NEWER than the tag it is applied to, so
    it can list a required entry the tag genuinely never shipped — exactly what happened when
    `NOTICE` was added required (91128ecb) while v0.4.1 (published before release-manifest.txt
    existed) was still the latest release: the pinned manifest fetch 404ed, the fallback to
    master borrowed a manifest listing NOTICE as required, and the download of NOTICE from the
    v0.4.1 tag 404ed too, which install-failed EVERY fresh install.

    This drives that exact shape with a SYNTHETIC required entry rather than pinning on NOTICE
    itself, per the issue's acceptance criteria: the regression is the class (a borrowed,
    newer-than-the-tag manifest listing something the tag lacks), not this one instance.
    A pinned install must still succeed — skipping the entry, not failing closed.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    old_tag = "v0.0.0-predates-manifest"
    synthetic_required_entry = "SYNTHETIC_REQUIRED_FILE_723"

    borrowed_manifest = tmp_path / "borrowed-release-manifest.txt"
    borrowed_manifest.write_text(f"docker-compose.yml\n{synthetic_required_entry}\n")

    # Stub curl:
    #   * the OLD TAG's own manifest fetch 404s (it predates release-manifest.txt)
    #   * the DEFAULT BRANCH's manifest fetch succeeds, returning the borrowed manifest
    #     above — which lists a REQUIRED entry the old tag never shipped
    #   * the GitHub API call inside resolve_default_branch() reports "master"
    #   * every artifact download from the old tag succeeds EXCEPT the synthetic entry,
    #     which 404s — simulating a file that simply does not exist at that old ref
    stub = bin_dir / "curl"
    stub.write_text(
        "#!/bin/bash\n"
        "url=''; out=''\n"
        "while [ $# -gt 0 ]; do\n"
        '  case "$1" in\n'
        '    -o) out="$2"; shift 2 ;;\n'
        '    http*) url="$1"; shift ;;\n'
        "    *) shift ;;\n"
        "  esac\n"
        "done\n"
        '  case "$url" in\n'
        "    *api.github.com*)\n"
        '      echo \'{"default_branch": "master"}\'\n'
        "      exit 0\n"
        "      ;;\n"
        f"    */master/release-manifest.txt)\n"
        f'      cp {borrowed_manifest} "$out"\n'
        "      exit 0\n"
        "      ;;\n"
        f"    */{old_tag}/release-manifest.txt)\n"
        # This tag predates the manifest — the pinned-ref fetch must fail.
        "      exit 1\n"
        "      ;;\n"
        f"    *{synthetic_required_entry})\n"
        # This tag never shipped the synthetic file the borrowed manifest requires.
        "      exit 1\n"
        "      ;;\n"
        "    *)\n"
        '      [ -n "$out" ] && printf "stub-content\\n" > "$out"\n'
        "      exit 0\n"
        "      ;;\n"
        "  esac\n"
    )
    stub.chmod(0o755)

    workdir = tmp_path / "install"
    workdir.mkdir()

    snippet = (
        "set -uo pipefail\nset -e\n"
        "RED=''; GREEN=''; YELLOW=''; BLUE=''; NC=''\n"
        + _extract_function(INSTALLER, "resolve_default_branch")
        + "\n"
        + _extract_function(INSTALLER, "download_release_manifest_artifacts")
        + "\ndownload_release_manifest_artifacts\n"
    )
    proc = subprocess.run(
        ["bash", "-c", snippet],
        capture_output=True,
        text=True,
        cwd=str(workdir),
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "OPENTRANSCRIBE_BRANCH": old_tag},
    )
    assert proc.returncode == 0, (
        f"a pinned install at {old_tag} failed because today's manifest requires "
        f"{synthetic_required_entry}, which {old_tag} never shipped — a REQUIRED addition to "
        f"the manifest retroactively broke an already-published release's install "
        f"(issue #723):\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )
    assert (workdir / "docker-compose.yml").is_file()
    assert not (workdir / synthetic_required_entry).exists()


def test_opentr_sh_is_not_shipped_and_the_shipped_script_covers_it():
    """opentr.sh is deliberately absent (issue #613): its backup/restore path uses bare
    `docker compose`, and the base compose file ALONE is an invalid project — measured
    (`docker compose -f docker-compose.yml exec -T postgres echo hi` fails with "service
    ... has neither an image nor a build context specified"). That exclusion is only
    defensible while opentranscribe.sh carries the commands itself, so assert BOTH halves.
    If someone ships opentr.sh, this test should make them say why.
    """
    entries = {path for path, _ in _entries()}
    assert "opentr.sh" not in entries, (
        "opentr.sh is now in release-manifest.txt, but its backup/restore path uses bare "
        "`docker compose` with no -f chain — shipping it as-is gives production operators a "
        "restore command that dies on its first `docker compose exec` (issue #613 §2.1). If "
        "this is intentional, opentr.sh's compose calls must first be threaded through a "
        "real -f chain the way scripts/common.sh's backup_database/restore_database are."
    )

    manager_source = MANAGER.read_text()
    assert re.search(r"^\s*backup\|restore\)", manager_source, re.MULTILINE), (
        "opentranscribe.sh has no backup|restore dispatch arm — with opentr.sh deliberately "
        "unshipped, this is the ONLY production backup/restore path (issue #613)"
    )


def test_env_example_is_listed_for_new_key_reporting():
    """update-full diffs .env.example against the user's .env to report new keys.

    Settings uses extra="ignore", so a newly required var is silently defaulted
    rather than erroring — reporting it at upgrade time is the only signal.
    """
    assert ".env.example" in {path for path, _ in _entries()}


def test_backup_overlay_is_opt_in_not_default():
    """The backup overlay must not be selected by a value .env.example ships SET.

    .env.example ships BACKUP_HOST_PATH=./backups, so keying selection off that being
    non-empty would enable the overlay for every install — and it sets path.repo on the
    opensearch service, force-recreating that container on every existing deployment's
    next update. Keyed on a dedicated BACKUP_OVERLAY_ENABLED that .env.example leaves
    COMMENTED OUT (issue #616).
    """
    manager_source = MANAGER.read_text(encoding="utf-8")
    assert "BACKUP_OVERLAY_ENABLED" in manager_source, (
        "opentranscribe.sh no longer references BACKUP_OVERLAY_ENABLED — did the backup "
        "overlay selection get keyed back onto BACKUP_HOST_PATH?"
    )
    # The selection guard must not test BACKUP_HOST_PATH's presence/truthiness -- only
    # its own dedicated toggle.
    assert not re.search(r'\[\s*-n\s*"\$backup_host_path"\s*\]', manager_source), (
        "opentranscribe.sh's backup overlay selection appears keyed on BACKUP_HOST_PATH "
        "being non-empty -- .env.example ships that SET, so this would enable the "
        "overlay (and its OpenSearch path.repo recreate) for every install by default"
    )

    assert ENV_EXAMPLE.exists(), ".env.example not present in this checkout"
    env_example_source = ENV_EXAMPLE.read_text(encoding="utf-8")
    assert "#BACKUP_OVERLAY_ENABLED=" in env_example_source, (
        ".env.example does not ship BACKUP_OVERLAY_ENABLED commented out -- the backup "
        "overlay must be opt-in, not enabled by a fresh `cp .env.example .env`"
    )
    assert not re.search(r"^BACKUP_OVERLAY_ENABLED=", env_example_source, re.MULTILINE), (
        ".env.example ships an UNCOMMENTED BACKUP_OVERLAY_ENABLED -- this would enable "
        "the backup overlay (and its OpenSearch path.repo recreate) for every fresh install"
    )


def test_no_shipped_script_references_scripts_lib():
    """No script listed in release-manifest.txt may reference scripts/lib/.

    scripts/lib/ (env_reader.py and friends) is dev/CI-only tooling -- it is
    deliberately NOT in this manifest, so it never reaches a standalone
    setup-opentranscribe.sh install. issue #590 added scripts/lib/env_reader.py and a
    caller in two SHIPPED scripts (download-models.sh, fix-model-permissions.sh)
    without adding env_reader.py itself to the manifest -- every real end-user install
    called a file that does not exist on disk (issue #590/#581), silently degrading
    (download-models.sh fell back to :latest instead of the pinned image tag) or
    crashing outright (fix-model-permissions.sh, which runs under `set -e`).

    This is the invariant that would have caught that mistake, and the guard against
    it recurring for any future script added to the manifest: a shipped script must
    read its .env values via scripts/common.sh's read_env_value() (also shipped),
    never scripts/lib/env_reader.py.
    """
    offenders = []
    for path, _flags in _entries():
        full = REPO_ROOT / path
        if full.suffix != ".sh":
            continue
        # A real invocation, not an explanatory comment naming the file (both
        # download-models.sh and fix-model-permissions.sh now carry comments
        # documenting why they DON'T call it -- those must not trip this).
        if re.search(r"python3[^\n]*lib/env_reader\.py", full.read_text(encoding="utf-8")):
            offenders.append(path)
    assert not offenders, (
        f"shipped script(s) reference scripts/lib/, which is dev/CI-only and never "
        f"reaches a standalone install: {offenders}. Use scripts/common.sh's "
        f"read_env_value() instead (issue #590/#581)."
    )
