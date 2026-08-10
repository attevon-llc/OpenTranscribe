"""The scripts a USER runs: install, update, and model download.

These three shell scripts are the entire product for someone who never clones the
repo, and until now nothing tested them below the level of the full release
scenarios — which take 45-120 minutes and need the live stack stopped. A typo in
an image tag or a lost unattended guard would only surface there, or in
production.

So: fast, hermetic checks of the *logic*, with no Docker, no network, and no
containers. They run in seconds as part of `release.sh verify` and CI, and the
long scenarios stay the proof that the whole thing works end to end.

Every case here corresponds to a defect that was actually found in these files:
the wrong GitHub org, a download list that had drifted from the code, a `:latest`
pin that defeated version pinning, an interactive prompt with no unattended
guard, and a downgrade with no guard rail.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
INSTALLER = REPO_ROOT / "setup-opentranscribe.sh"
MANAGER = REPO_ROOT / "opentranscribe.sh"
DOWNLOADER = REPO_ROOT / "scripts" / "download-models.sh"
DOWNLOADER_PY = REPO_ROOT / "scripts" / "download-models.py"

pytestmark = pytest.mark.skipif(
    not INSTALLER.exists(), reason="install scripts not present in this checkout"
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


def _run_shell(snippet: str, env: dict[str, str] | None = None, cwd: Path | None = None) -> str:
    proc = subprocess.run(
        ["bash", "-c", snippet],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", **(env or {})},
        cwd=str(cwd) if cwd else None,
    )
    return (proc.stdout + proc.stderr).strip()


# --------------------------------------------------------------------------- #
# All three scripts parse and lint
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("script", [INSTALLER, MANAGER, DOWNLOADER], ids=lambda p: p.name)
def test_script_parses(script: Path):
    """A syntax error here bricks installs for everyone on that release."""
    proc = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert proc.returncode == 0, f"{script.name} does not parse:\n{proc.stderr}"


@pytest.mark.parametrize("script", [INSTALLER, MANAGER, DOWNLOADER], ids=lambda p: p.name)
def test_no_wrong_github_org(script: Path):
    """The repo moved to attevon-llc; a stale org means every download 404s.

    opentranscribe.sh's update-full and version check both pointed at
    davidamacey/OpenTranscribe while the installer used attevon-llc, so existing
    users were fetching from a different path than fresh installs.
    """
    hits = [
        line for line in script.read_text().splitlines() if "davidamacey/OpenTranscribe" in line
    ]
    assert not hits, f"{script.name} references the old GitHub org:\n" + "\n".join(hits[:5])


# --------------------------------------------------------------------------- #
# Version pinning — the point of the whole release-pinning effort
# --------------------------------------------------------------------------- #


def test_installer_resolves_an_explicit_version():
    snippet = (
        _extract_function(INSTALLER, "resolve_install_ref")
        + """
print_info(){ :; }; print_success(){ :; }; print_warning(){ :; }; print_error(){ :; }
OPENTRANSCRIBE_VERSION="v0.4.0"; OPENTRANSCRIBE_BRANCH=""
resolve_install_ref
echo "$OPENTRANSCRIBE_BRANCH|$OT_IMAGE_TAG"
"""
    )
    assert _run_shell(snippet).endswith("v0.4.0|v0.4.0")


def test_installer_normalises_a_bare_semver():
    """`--version 0.4.0` must pin the same thing as `--version v0.4.0`."""
    snippet = (
        _extract_function(INSTALLER, "resolve_install_ref")
        + """
print_info(){ :; }; print_success(){ :; }; print_warning(){ :; }; print_error(){ :; }
OPENTRANSCRIBE_VERSION="0.4.0"; OPENTRANSCRIBE_BRANCH=""
resolve_install_ref
echo "$OPENTRANSCRIBE_BRANCH|$OT_IMAGE_TAG"
"""
    )
    assert _run_shell(snippet).endswith("v0.4.0|v0.4.0")


def test_installer_branch_mode_is_unpinned_and_says_so():
    """--branch is a testing escape hatch and must announce that it is not pinned."""
    snippet = (
        _extract_function(INSTALLER, "resolve_install_ref")
        + """
print_info(){ :; }; print_success(){ :; }; print_error(){ :; }
print_warning(){ echo "WARN:$*"; }
OPENTRANSCRIBE_VERSION=""; OPENTRANSCRIBE_BRANCH="master"
resolve_install_ref
echo "RESULT:$OPENTRANSCRIBE_BRANCH|$OT_IMAGE_TAG"
"""
    )
    out = _run_shell(snippet)
    assert "RESULT:master|latest" in out
    assert "WARN:" in out, "an unpinned install must warn that it is not reproducible"


def test_installer_never_silently_falls_back_to_master():
    """A failed release lookup must be a hard error, not a quiet unpinned install.

    Falling back to master would reintroduce exactly the un-reproducible install
    that pinning exists to remove — and it would do it invisibly.
    """
    source = INSTALLER.read_text()
    fn = _extract_function(INSTALLER, "resolve_install_ref")
    assert "exit 1" in fn, "resolve_install_ref must fail hard when it cannot resolve a release"
    assert "--version vX.Y.Z" in fn, "the failure must tell the user how to proceed"
    # And the flags it names must actually exist.
    assert "--version)" in source and "--branch)" in source


def test_downloader_uses_the_deployments_pinned_image():
    """Model downloads must use the version the deployment runs, not :latest.

    This was hardcoded to :latest. Model requirements change between releases, so
    a pinned v0.5.0 install would have fetched whatever :latest was that day —
    and on an air-gapped box, the wrong set means a hard failure at runtime.
    """
    fn = _extract_function(DOWNLOADER, "resolve_downloader_image")

    pinned = _run_shell(
        fn + "\nREPO_ROOT=/nonexistent\nresolve_downloader_image\n",
        env={"OT_IMAGE_TAG": "v0.5.0"},
    )
    assert pinned.endswith("opentranscribe-backend:v0.5.0")

    unset = _run_shell(fn + "\nREPO_ROOT=/nonexistent\nresolve_downloader_image\n")
    assert unset.endswith("opentranscribe-backend:latest"), "must stay backward-compatible"


def test_downloader_reads_the_tag_from_a_deployment_env(tmp_path: Path):
    """An installed deployment keeps .env beside the compose files, not one level up."""
    (tmp_path / ".env").write_text("OT_IMAGE_TAG=v0.4.1\n")
    out = _run_shell(
        _extract_function(DOWNLOADER, "resolve_downloader_image")
        + "\nREPO_ROOT=/nonexistent\nresolve_downloader_image\n",
        cwd=tmp_path,
    )
    assert out.endswith("opentranscribe-backend:v0.4.1")


def test_no_hardcoded_latest_backend_image_in_user_scripts():
    """Any remaining `:latest` pin silently defeats a pinned install."""
    offenders = {}
    for script in (INSTALLER, MANAGER, DOWNLOADER):
        hits = [
            line.strip()
            for line in script.read_text().splitlines()
            if re.search(r"opentranscribe-backend:latest", line)
            and not line.lstrip().startswith("#")
        ]
        if hits:
            offenders[script.name] = hits
    assert not offenders, f"hardcoded :latest backend image: {offenders}"


# --------------------------------------------------------------------------- #
# Unattended safety — a blocked prompt hangs an install with no explanation
# --------------------------------------------------------------------------- #


def test_installer_guards_every_interactive_section():
    """`read ... </dev/tty` bypasses redirection, so each needs an explicit guard."""
    source = INSTALLER.read_text()
    tty_reads = len(re.findall(r"read .*</dev/tty", source))
    guards = len(re.findall(r"is_unattended", source))
    assert tty_reads > 0
    assert guards >= 8, (
        f"{tty_reads} interactive reads but only {guards} is_unattended references — "
        "an unguarded prompt hangs an unattended install forever"
    )


def test_downloader_does_not_block_without_a_tty():
    """The model downloader is invoked by the installer and by the release harness."""
    source = DOWNLOADER.read_text()
    assert "OPENTRANSCRIBE_UNATTENDED" in source, "no unattended guard in download-models.sh"
    assert "-t 0" in source, "must also detect a missing TTY, not just the env var"


# --------------------------------------------------------------------------- #
# The downloader's documentation must match what it downloads
# --------------------------------------------------------------------------- #


def test_every_model_category_is_announced():
    """The user-facing list had drifted from the code by two whole categories.

    download-models.py grew the chat reranker and the redaction models; the shell
    wrapper still advertised six items and "~2.9GB", while its own failure path
    treated anything under 10GB as a partial download.
    """
    functions = set(re.findall(r"^def (download_\w+)", DOWNLOADER_PY.read_text(), re.MULTILINE))
    announced = DOWNLOADER.read_text().lower()

    # Map each downloader function to a word that must appear in the wrapper's list.
    keywords = {
        "download_whisperx_models": "whisperx",
        "download_pyannote_models": "pyannote",
        "download_nltk_data": "nltk",
        "download_sentence_transformers": "sentence-transformers",
        "download_chat_reranker": "reranker",
        "download_speaker_attribute_models": "wav2vec2",
        "download_opensearch_neural_models": "opensearch",
        "download_redaction_models": "redaction",
    }
    missing = [fn for fn in functions if fn in keywords and keywords[fn] not in announced]
    assert not missing, (
        f"download-models.py downloads {missing} but download-models.sh never mentions "
        "them — users cannot tell what they are waiting for, or what is missing"
    )


def test_redaction_toggle_reaches_the_container():
    """download-models.py honours DOWNLOAD_REDACTION_MODELS; the wrapper must pass it."""
    assert "DOWNLOAD_REDACTION_MODELS" in DOWNLOADER.read_text(), (
        "the wrapper drops DOWNLOAD_REDACTION_MODELS, so a user cannot turn it off"
    )


# --------------------------------------------------------------------------- #
# Upgrade path
# --------------------------------------------------------------------------- #


def test_update_full_is_manifest_driven():
    """A hardcoded list is what let update-full miss docker-compose.yml."""
    source = MANAGER.read_text()
    assert "release-manifest.txt" in source


def test_both_update_paths_share_the_phased_restart():
    """update-full previously did a bare `up -d`.

    That is the path compose can abandon mid-migration — and update-full is the
    command people run when crossing releases, so it is the MORE likely of the two
    to be running a long Alembic chain.
    """
    source = MANAGER.read_text()
    assert source.count("perform_phased_restart") >= 3, (
        "expected one definition plus a call from both update and update-full"
    )


def test_downgrade_is_guarded():
    """The migration chain is one-way; images can roll back, the database cannot."""
    source = MANAGER.read_text()
    assert "--force-downgrade" in source
    assert "--rollback" in source
    assert "sort -V" in source, "semver comparison must not be a string compare (v0.10 > v0.9)"


def test_update_full_mentions_the_model_cache():
    """A release that adds a model breaks air-gapped upgrades silently otherwise."""
    source = MANAGER.read_text()
    assert "download-models.sh" in source, (
        "update-full should tell the user how to pre-fetch models a new release needs; "
        "offline deployments set HF_HUB_OFFLINE=1 and cannot lazy-download"
    )


# --------------------------------------------------------------------------- #
# Release-harness image pinning
# --------------------------------------------------------------------------- #

PROD_COMPOSE = REPO_ROOT / "docker-compose.prod.yml"
ENV_TEMPLATE = REPO_ROOT / "scripts" / "release-tests" / "lib" / "env-template.sh"
UPGRADE_SCENARIO = REPO_ROOT / "scripts" / "release-tests" / "test-upgrade.sh"
FRESH_SCENARIO = REPO_ROOT / "scripts" / "release-tests" / "test-fresh-install.sh"


def test_every_prod_service_image_is_tag_pinnable():
    """One variable must pin every service, not a hand-maintained list.

    The scenarios also rewrite individual `image:` lines through
    cp_pin_image_tag, but that call takes an explicit service list which had
    already drifted — it named 11 services while docker-compose.prod.yml declares
    15, missing `docs` and the three GPU worker variants. Under
    `pull_policy: never` those would have run whatever `:latest` was in the local
    cache, i.e. the PREVIOUS release: a mixed-version stack, and a rehearsal
    proving the wrong thing.
    """
    import yaml

    services = yaml.safe_load(PROD_COMPOSE.read_text()).get("services") or {}
    unpinnable = {
        name: svc["image"]
        for name, svc in services.items()
        if isinstance(svc, dict)
        and "image" in svc
        and "opentranscribe" in svc["image"]
        and "${OT_IMAGE_TAG" not in svc["image"]
    }
    assert not unpinnable, f"these service images cannot be pinned by OT_IMAGE_TAG: {unpinnable}"


def test_generated_test_env_pins_the_image_tag():
    assert "OT_IMAGE_TAG=" in ENV_TEMPLATE.read_text(), (
        "the release-test .env must pin OT_IMAGE_TAG or the stack mixes versions"
    )


def test_both_scenarios_export_the_tag_under_test():
    for scenario in (FRESH_SCENARIO, UPGRADE_SCENARIO):
        assert "OT_TEST_IMAGE_TAG" in scenario.read_text(), (
            f"{scenario.name} does not export the tag the env template reads"
        )


def test_upgrade_repins_the_env_for_the_new_stack():
    """The swap copies the FROM .env; OT_IMAGE_TAG must be moved to TO.

    Copying it verbatim would leave the "upgraded" stack running the old images
    for every service outside the hardcoded pin list — an upgrade test that
    partly did not upgrade.
    """
    source = UPGRADE_SCENARIO.read_text()
    assert 'sed -i "s|^OT_IMAGE_TAG=' in source, (
        "the after-stack .env is not re-pinned to the version being upgraded to"
    )


def test_installer_reads_optional_env_keys_safely():
    """`set -e` + `set -o pipefail` made an ABSENT optional .env key fatal.

    display_summary loaded values with `VAR=$(grep '^KEY=' .env | cut -d= -f2-)`.
    When the key is absent -- the normal case for LLM_PROVIDER, VLLM_BASE_URL and
    NGINX_SERVER_NAME on a default install -- grep exits 1, pipefail propagates it
    to the assignment, and set -e killed the script at the very last step of the
    install, after every image had already been pulled.

    It surfaced in the release harness only as "one-liner failed", because the
    abort happened before the function printed anything.

    Two failure modes, both closed: reads use ${VAR:-} (set -u) and go through a
    helper ending in `|| true` (set -e + pipefail).
    """
    source = INSTALLER.read_text()
    summary = source.split("display_summary()", 1)[1].split("\nprompt_start", 1)[0]

    bare_greps = re.findall(r"=\$\(grep '\^[A-Z_]+=' \.env[^)]*\)", summary)
    assert not bare_greps, (
        f"display_summary still assigns from an unguarded grep pipeline: {bare_greps}. "
        "An absent optional key exits 1 under pipefail and set -e kills the install."
    )
    assert "_env_val" in summary, "expected the || true helper for optional .env reads"


def test_unattended_install_persists_the_huggingface_token():
    """An unattended install must WRITE the token, not just hold it in a variable.

    prompt_huggingface_token() returns early in unattended mode. That early return
    used to sit above the only line that writes the token to .env (in the
    interactive branch), so an unattended install produced `HUGGINGFACE_TOKEN=`
    empty. The containers never saw it, PyAnnote's gated models answered 401, and
    EVERY transcription failed on a stack that otherwise looked healthy —
    diarization is not optional, the pipeline cannot complete without it.

    Affects every scripted/CI install and the release harness itself.
    """
    source = INSTALLER.read_text()
    fn = source.split("prompt_huggingface_token()", 1)[1].split("\n_upsert_env", 1)[0]
    unattended = fn.split("is_unattended", 1)[1].split("return 0", 1)[0]
    assert "HUGGINGFACE_TOKEN=" in unattended and "sed" in unattended, (
        "the unattended branch returns without persisting HUGGINGFACE_TOKEN to .env"
    )
