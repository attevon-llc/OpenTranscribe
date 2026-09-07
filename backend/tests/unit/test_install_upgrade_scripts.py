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
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
INSTALLER = REPO_ROOT / "setup-opentranscribe.sh"
MANAGER = REPO_ROOT / "opentranscribe.sh"
DOWNLOADER = REPO_ROOT / "scripts" / "download-models.sh"
DOWNLOADER_PY = REPO_ROOT / "scripts" / "download-models.py"
COMMON = REPO_ROOT / "scripts" / "common.sh"

pytestmark = pytest.mark.skipif(
    not INSTALLER.exists(), reason="install scripts not present in this checkout"
)


def _extract_function(script: Path, name: str) -> str:
    """Pull one shell function out of a script so it can be run in isolation.

    Two definition shapes exist. opentranscribe.sh ships standalone to end users, so
    read_env_value/resolve_default_branch are defined inside an
    ``if ! declare -F <name>; then ... fi`` guard (common.sh's copy wins when present) —
    they are indented, and a ``^name()`` match cannot see them. Extracting the whole
    guard block yields valid bash either way.
    """
    out = subprocess.run(
        ["sed", "-n", f"/^{name}()/,/^}}/p", str(script)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    if not out.strip():
        out = subprocess.run(
            ["sed", "-n", rf"/^if ! declare -F {name}\b/,/^fi$/p", str(script)],
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


@pytest.mark.parametrize("script", [INSTALLER, MANAGER, DOWNLOADER, COMMON], ids=lambda p: p.name)
def test_script_parses(script: Path):
    """A syntax error here bricks installs for everyone on that release.

    scripts/common.sh joined this list in issue #613: it now carries the destructive
    backup/restore path both front ends share, and was not previously in this parametrize
    despite that.
    """
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
    # SCRIPT_DIR is normally set once at the top of download-models.sh (real invocations
    # always run the whole file); the isolated function here needs it injected the same
    # way. resolve_downloader_image() calls read_env_value() (scripts/common.sh), not
    # scripts/lib/env_reader.py directly (issue #590/#581) -- source common.sh so the
    # extracted function body has it, exactly as download-models.sh itself does and as
    # test_downloader_reads_the_tag_from_a_deployment_env already does below.
    script_dir_prelude = f'SCRIPT_DIR="{REPO_ROOT / "scripts"}"\nsource {COMMON}\n'
    fn = _extract_function(DOWNLOADER, "resolve_downloader_image")

    pinned = _run_shell(
        script_dir_prelude + fn + "\nREPO_ROOT=/nonexistent\nresolve_downloader_image\n",
        env={"OT_IMAGE_TAG": "v0.5.0"},
    )
    assert pinned.endswith("opentranscribe-backend:v0.5.0")

    unset = _run_shell(
        script_dir_prelude + fn + "\nREPO_ROOT=/nonexistent\nresolve_downloader_image\n"
    )
    assert unset.endswith("opentranscribe-backend:latest"), "must stay backward-compatible"


def test_downloader_reads_the_tag_from_a_deployment_env(tmp_path: Path):
    """An installed deployment keeps .env beside the compose files, not one level up."""
    (tmp_path / ".env").write_text("OT_IMAGE_TAG=v0.4.1\n")
    # resolve_downloader_image() now calls read_env_value() (scripts/common.sh), not
    # scripts/lib/env_reader.py directly (issue #590/#581) -- source common.sh so the
    # extracted function body has it, exactly as download-models.sh itself does.
    script_dir_prelude = f'SCRIPT_DIR="{REPO_ROOT / "scripts"}"\nsource {COMMON}\n'
    out = _run_shell(
        script_dir_prelude
        + _extract_function(DOWNLOADER, "resolve_downloader_image")
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
    """The .env's OT_IMAGE_TAG must move to TO before the scenario is done.

    Copying it verbatim (and leaving it there) would leave the "upgraded" stack
    running the old images for every service outside cp_pin_image_tag's
    hardcoded pin list — an upgrade test that partly did not upgrade.

    This used to be a hand-rolled `sed` in phase 07. Issue #598 found that the
    hand-rolled rewrite never recorded `# OT_PREVIOUS_IMAGE_TAG`, so
    `opentranscribe.sh update --rollback` invoked at the end of the scenario
    exited 1 with "no previous version recorded" — the rollback tail could not
    even be attempted. Phase 08 now runs the REAL
    `./opentranscribe.sh update --version` instead, which performs the same
    move AND records the rollback bookkeeping test_upgrade_scenario_records_a_
    rollback_target and phase 12 depend on.
    """
    source = UPGRADE_SCENARIO.read_text()
    assert 'sed -i "s|^OT_IMAGE_TAG=' not in source, (
        "a hand-rolled OT_IMAGE_TAG sed is back — it does not record "
        "# OT_PREVIOUS_IMAGE_TAG, so a rollback rehearsed against it has no target"
    )
    assert './opentranscribe.sh update --version "$LOCAL_IMAGE_TAG"' in source, (
        "phase 08 must invoke the real 'update --version' path, not a bare "
        "'update' (which does not move OT_IMAGE_TAG once phase 07 stopped seding it)"
    )


def test_upgrade_scenario_records_a_rollback_target():
    """`update --version` is what writes `# OT_PREVIOUS_IMAGE_TAG` (issue #598 §2.4).

    A bare `update` never writes it, so a `--rollback` invoked at the end of the
    scenario used to exit 1 with "no previous version recorded" — the rollback
    tail (phases 13-17) could not even be attempted. Phase 12 asserts the
    precondition is actually recorded in the staged .env, not merely that the
    scenario invokes the right subcommand — this test is the cheap static half.
    """
    source = UPGRADE_SCENARIO.read_text()
    assert "update --version" in source
    assert "phase_12_assert_rollback_precondition" in source
    assert "OT_PREVIOUS_IMAGE_TAG" in source


def _extract_case_block(script: Path, start_label: str, end_label: str) -> str:
    """Pull one `case` arm out of a script, from its label through the NEXT
    label's line (exclusive) — sed can't stop at a bare `;;`, since the block
    itself contains a nested `case ... esac` with its own `;;` terminators.
    """
    out = subprocess.run(
        ["sed", "-n", f"/^{start_label}/,/^{end_label}/p", str(script)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert out.strip(), f"{start_label} not found in {script.name}"
    body = out.split(start_label, 1)[1]
    body = body.rsplit(end_label, 1)[0]
    return body


def test_rollback_refuses_without_a_recorded_target(tmp_path: Path):
    """Behavioral: `update --rollback` against a .env with no recorded target.

    Runs the REAL `update)` case body (extracted, not re-typed) with
    `check_environment`/`fix_model_cache_permissions` stubbed — the real
    versions require a live `.env`/`docker-compose.yml` and can shell out to
    Docker to fix cache ownership, neither of which this fast unit test should
    do. It is still exercising the real do_rollback branch: with `set --
    update --rollback` and no `# OT_PREVIOUS_IMAGE_TAG` in .env, execution
    must reach `exit 1` before ever calling `compose_down_for_upgrade` (not
    stubbed here — an uncaught call to it would itself fail the snippet).
    """
    (tmp_path / ".env").write_text("OT_IMAGE_TAG=v0.5.0\n")
    body = _extract_case_block(MANAGER, r"    update)", r"    update-full)")
    snippet = f"""
YELLOW='\\033[1;33m'; GREEN='\\033[0;32m'; RED='\\033[0;31m'; BLUE='\\033[0;34m'; NC='\\033[0m'
cd {tmp_path}
check_environment() {{ :; }}
fix_model_cache_permissions() {{ :; }}
set -- update --rollback
{body}
"""
    out = _run_shell(snippet)
    assert "No previous version recorded" in out
    assert "update --version vX.Y.Z" in out


def _rollback_snippet(tmp_path: Path, body: str, docker_stub: str, extra_args: str = "") -> str:
    """Shared scaffolding for the issue #610 Part B rollback-preflight tests below.

    Stubs everything the real `update)` case body calls before/around the preflight so
    it needs neither a live stack nor a real backend image: `get_compose_files`
    (defined elsewhere in opentranscribe.sh, invisible to this extracted snippet),
    `preflight_upgrade_env`, and `compose_down_for_upgrade` — the last one echoes a
    marker and exits immediately, so reaching it (or not) is the observable signal for
    whether the preflight refused.
    """
    return f"""
YELLOW='\\033[1;33m'; GREEN='\\033[0;32m'; RED='\\033[0;31m'; BLUE='\\033[0;34m'; NC='\\033[0m'
cd {tmp_path}
check_environment() {{ :; }}
fix_model_cache_permissions() {{ :; }}
get_compose_files() {{ echo "-f docker-compose.yml"; }}
preflight_upgrade_env() {{ :; }}
compose_down_for_upgrade() {{ echo "REACHED_TEARDOWN"; exit 0; }}
{docker_stub}
set -- update --rollback {extra_args}
{body}
"""


def test_rollback_refuses_when_target_image_does_not_know_the_live_head(tmp_path: Path):
    """Issue #610 Part B — the mirror-image gap: `update --rollback` swaps in an OLDER

    image without checking whether its migration chain can even read the CURRENT
    schema. Without this preflight, Alembic aborts with a cryptic "Can't locate
    revision identified by '<head>'" only after the stack is already torn down.

    `docker` is stubbed as a bash function (functions shadow PATH binaries for
    unqualified calls) so this needs no live stack and no real backend image:
    `docker compose ... exec ... psql` returns a fake live head, `docker run ...`
    (the "does the target image know this revision" check) returns 1 — grep found
    nothing, i.e. the target image does NOT know it.
    """
    (tmp_path / ".env").write_text("OT_IMAGE_TAG=v0.5.0\n# OT_PREVIOUS_IMAGE_TAG=v0.4.1\n")
    body = _extract_case_block(MANAGER, r"    update)", r"    update-full)")
    docker_stub = """
docker() {
  if [ "$1" = "compose" ]; then
    echo "v393_add_overlap_timing_columns"
    return 0
  elif [ "$1" = "run" ]; then
    return 1
  fi
  return 0
}
"""
    out = _run_shell(_rollback_snippet(tmp_path, body, docker_stub))
    assert "Refusing to roll back" in out, out
    assert "does not know" in out, out
    assert "REACHED_TEARDOWN" not in out, (
        f"must refuse BEFORE compose_down_for_upgrade tears the stack down: {out}"
    )


def test_rollback_proceeds_when_target_image_knows_the_live_head(tmp_path: Path):
    """Positive control for the test above: same setup, but the target image DOES know

    the live head (`docker run`'s grep succeeds) — rollback must proceed to teardown,
    not just "the preflight never fires at all" (which would pass the negative test
    for the wrong reason too).
    """
    (tmp_path / ".env").write_text("OT_IMAGE_TAG=v0.5.0\n# OT_PREVIOUS_IMAGE_TAG=v0.4.1\n")
    body = _extract_case_block(MANAGER, r"    update)", r"    update-full)")
    docker_stub = """
docker() {
  if [ "$1" = "compose" ]; then
    echo "v393_add_overlap_timing_columns"
    return 0
  elif [ "$1" = "run" ]; then
    return 0
  fi
  return 0
}
"""
    out = _run_shell(_rollback_snippet(tmp_path, body, docker_stub))
    assert "Refusing to roll back" not in out, out
    assert "REACHED_TEARDOWN" in out, f"expected the rollback to proceed to teardown: {out}"


def test_rollback_force_downgrade_skips_the_preflight_check(tmp_path: Path):
    """`--force-downgrade` is the documented override — it must bypass this preflight

    entirely (not just the pre-existing semver-ordering guard it already overrides).
    `docker` is stubbed to fail loudly if called at all, so any call — the live-head
    read or the target-image check — fails this test.
    """
    (tmp_path / ".env").write_text("OT_IMAGE_TAG=v0.5.0\n# OT_PREVIOUS_IMAGE_TAG=v0.4.1\n")
    body = _extract_case_block(MANAGER, r"    update)", r"    update-full)")
    docker_stub = """
docker() { echo "DOCKER_SHOULD_NOT_BE_CALLED_UNDER_FORCE_DOWNGRADE"; return 1; }
"""
    out = _run_shell(_rollback_snippet(tmp_path, body, docker_stub, extra_args="--force-downgrade"))
    assert "DOCKER_SHOULD_NOT_BE_CALLED_UNDER_FORCE_DOWNGRADE" not in out, out
    assert "Refusing to roll back" not in out, out
    assert "REACHED_TEARDOWN" in out, f"expected the rollback to proceed to teardown: {out}"


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


def test_upgrade_tolerates_the_stale_network_race():
    """A routine `update` must not die half-way on a Docker daemon race.

    `docker compose down` removes containers then the network. The daemon
    sometimes keeps a stale endpoint record for an already-removed container, so
    network removal fails with "has active endpoints" while every container is
    gone. compose reports that as overall failure, which aborted the upgrade with
    the stack down and nothing brought back up -- and a user cannot restart the
    Docker daemon to recover.

    Hit during the v0.5.0 rehearsal at the real `opentranscribe.sh update` step.
    """
    source = MANAGER.read_text()
    assert "compose_down_for_upgrade" in source, (
        "the upgrade paths should use the race-tolerant teardown"
    )
    fn = source.split("compose_down_for_upgrade() {", 1)[1].split("\n}", 1)[0]
    assert "active endpoints" in source or "stale" in fn.lower()
    # Tolerating the race must NOT mean tolerating a real teardown failure.
    assert "docker ps -aq" in fn, (
        "must verify no containers remain before continuing past a failed down"
    )


def test_upgrade_refuses_before_teardown_when_secrets_are_missing():
    """v0.5.0 enforces production secrets v0.4.x skipped -- refuse BEFORE `down`.

    Enforcement moved from fail-open to fail-closed (#284 A0.3): ENVIRONMENT now
    defaults to production, so a v0.4.x deployment that never set it skipped every
    production secret check and now gets all of them. The first symptom was the
    new backend exiting with "REDIS_PASSWORD is required in production
    environment" AFTER the stack had been torn down -- old stack stopped, new one
    refusing to boot (#410).

    The gate must therefore run before the teardown, on BOTH upgrade paths, and
    must respect the same relaxed-environment opt-out the backend uses.
    """
    source = MANAGER.read_text()
    assert "preflight_upgrade_env" in source

    fn = source.split("preflight_upgrade_env() {", 1)[1].split("\n}", 1)[0]
    assert "REDIS_PASSWORD" in fn
    assert "development" in fn, "must honour the relaxed-environment opt-out"

    # Ordering is the whole point: the check must precede the teardown each time.
    for path in source.split("preflight_upgrade_env || exit 1")[1:]:
        head = path.lstrip().splitlines()[0]
        assert "compose_down_for_upgrade" in head, (
            f"teardown does not immediately follow the gate: {head!r}"
        )
    assert source.count("preflight_upgrade_env || exit 1") == 2, (
        "both `update` and `update-full` must be gated"
    )


def test_base_compose_does_not_blank_the_baked_version():
    """The bare `- APP_VERSION` form overrode the image's baked ENV with "".

    Only opentr.sh (a git checkout) exports APP_VERSION. A real deployment runs
    opentranscribe.sh or plain `docker compose`, so Compose forwarded an EMPTY
    value and every production container reported its version as "unknown" --
    defeating the --build-arg contract one layer further out, and silently
    disabling AboutModal's version-mismatch warning (#411).

    `${APP_VERSION:-}` is equally wrong: it injects an empty string just the same.
    """
    base = (REPO_ROOT / "docker-compose.yml").read_text()
    code = "\n".join(line for line in base.splitlines() if not line.lstrip().startswith("#"))
    assert not re.search(r"^\s+- APP_VERSION\s*$", code, re.MULTILINE), (
        "base compose forwards the host's APP_VERSION, blanking the image's baked value"
    )
    assert not re.search(r"APP_VERSION:\s*\$\{APP_VERSION:-\}", code), (
        "base compose injects an empty APP_VERSION, which is the same bug"
    )

    # Dev genuinely needs it: bind-mounted source over a possibly stale image.
    override = (REPO_ROOT / "docker-compose.override.yml").read_text()
    assert "APP_VERSION" in override, "dev override should still supply APP_VERSION"


def test_skip_is_not_counted_as_a_failure():
    """A deliberate "not applicable" must not read as a failing release gate.

    as_record's else-branch treated SKIP as FAIL, so a correctly hardened
    deployment that does not serve openapi.json made the route-diff check appear
    to fail.
    """
    assertions = (REPO_ROOT / "scripts" / "release-tests" / "lib" / "assertions.sh").read_text()
    assert "as_skip" in assertions, "SKIP is still counted as a failure"


# --------------------------------------------------------------------------- #
# Issue #613 — opentranscribe.sh backup/restore commands.
#
# A real production self-hosted install (curl-install / update-full) had NO shipped way to
# run backup or restore at all: opentr.sh is deliberately not on release-manifest.txt (its
# bare `docker compose` calls only work in a repo clone — see test_release_manifest.py's
# test_opentr_sh_is_not_shipped_and_the_shipped_script_covers_it), and opentranscribe.sh had
# no backup)/restore) dispatch arms. The fix promotes backup_database/restore_database out
# of opentr.sh into scripts/common.sh (parameterized by a leading compose-files chain and a
# front-end name) and wires them into both front ends.
# --------------------------------------------------------------------------- #


def test_manager_dispatches_backup_and_restore():
    """The dispatch case must actually have the arm — not just mention the word."""
    source = MANAGER.read_text()
    assert re.search(r"^\s*backup\|restore\)", source, re.MULTILINE), (
        "expected a `backup|restore)` case arm in opentranscribe.sh's dispatch"
    )


def test_manager_dispatch_control_a_commented_out_arm_does_not_count():
    """Must-fire control for the detector above."""
    synthetic = "    # backup|restore)\n    #     ...\n    #     ;;\n"
    assert not re.search(r"^\s*backup\|restore\)", synthetic, re.MULTILINE)


def test_manager_help_lists_backup_and_restore():
    """A command nobody can discover is not shipped.

    show_help is declared `function show_help { ... }` (the only function in this file using
    that style, everything else is `name() { ... }`), so it needs its own sed anchor rather
    than `_extract_function`'s `name()` pattern.
    """
    fn = subprocess.run(
        ["sed", "-n", "/^function show_help {/,/^}/p", str(MANAGER)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert fn.strip(), "show_help() not found in opentranscribe.sh"
    out = _run_shell(fn + '\nBLUE=""; NC=""\nshow_help\n')
    assert "backup" in out and "restore" in out, f"show_help does not mention backup/restore: {out}"


def test_manager_sources_common_sh_before_its_own_fix_model_cache_permissions():
    """Position matters: bash keeps the LAST definition, so sourcing common.sh must happen
    BEFORE opentranscribe.sh's own fix_model_cache_permissions is defined, or the shared
    (and slightly different) common.sh version would silently replace the local one.
    """
    lines = MANAGER.read_text().splitlines()
    source_idx = next(
        i for i, line in enumerate(lines) if line.strip() == "if [ -f ./scripts/common.sh ]; then"
    )
    fn_idx = next(
        i for i, line in enumerate(lines) if line.startswith("fix_model_cache_permissions() {")
    )
    assert source_idx < fn_idx, (
        f"scripts/common.sh is sourced at line {source_idx + 1}, but "
        f"fix_model_cache_permissions() is defined at line {fn_idx + 1} — sourcing must come "
        "FIRST so the local definition wins (bash: last definition wins)"
    )


def test_sourcing_order_detector_fires_when_reversed():
    """Must-fire control: a synthetic file with the order reversed."""
    reversed_source = (
        "fix_model_cache_permissions() {\n  echo local\n}\n"
        "if [ -f ./scripts/common.sh ]; then\n  . ./scripts/common.sh\nfi\n"
    ).splitlines()
    source_idx = next(
        i
        for i, line in enumerate(reversed_source)
        if line.strip() == "if [ -f ./scripts/common.sh ]; then"
    )
    fn_idx = next(
        i
        for i, line in enumerate(reversed_source)
        if line.startswith("fix_model_cache_permissions() {")
    )
    assert not (source_idx < fn_idx), "fixture is wrong: this must be the REVERSED order"


def test_local_fix_model_cache_permissions_still_wins(tmp_path: Path):
    """Behavioural companion to the ordering test above: source the REAL common.sh, then the
    REAL opentranscribe.sh, and assert the definition left standing is opentranscribe.sh's
    own — identifiable by `current_owner`, a variable name that appears only in ITS version
    (common.sh's sibling loops over every subdirectory and calls the equivalent variable
    `owner`, not `current_owner`).
    """
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "common.sh").write_text(COMMON.read_text())
    # Sourcing the whole manager also runs its trailing `case "${1:-help}" in ... esac` with
    # no args, which just prints help text — harmless, and proves nothing in the dispatch
    # itself interferes with the sourcing order under test.
    out = _run_shell(f"source {MANAGER}\ndeclare -f fix_model_cache_permissions\n", cwd=tmp_path)
    assert "current_owner" in out, (
        f"opentranscribe.sh's OWN fix_model_cache_permissions did not win after sourcing "
        f"common.sh (bash: last definition wins) — got:\n{out}"
    )


def test_backup_and_restore_fail_with_a_remedy_without_common_sh(tmp_path: Path):
    """A checkout whose scripts/common.sh predates issue #613 (or is missing) must fail
    closed with a remedy, not crash on "command not found: backup_database".
    """
    (tmp_path / ".env").write_text("")
    (tmp_path / "docker-compose.yml").write_text("")
    assert not (tmp_path / "scripts").exists()
    out = _run_shell(f"cd {tmp_path} && bash {MANAGER} backup 2>&1")
    assert "scripts/common.sh is missing or too old" in out, out
    assert "update-full" in out, "expected the remedy to name update-full"


def test_backup_and_restore_succeed_with_common_sh_present_no_remedy_message(tmp_path: Path):
    """Must-stay-clean companion: with common.sh present, the remedy must NOT appear."""
    (tmp_path / ".env").write_text("")
    (tmp_path / "docker-compose.yml").write_text("")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "common.sh").write_text(COMMON.read_text())
    out = _run_shell(f"cd {tmp_path} && bash {MANAGER} backup --bogus-flag 2>&1")
    assert "scripts/common.sh is missing or too old" not in out, out


def test_manager_has_no_dangling_opentr_sh_instructions():
    """The shipped script must never tell a production operator to run a file they don't
    have. `opentranscribe.sh:572`'s #610 rollback preflight used to say exactly that.
    """
    offenders = [
        (i + 1, line)
        for i, line in enumerate(MANAGER.read_text().splitlines())
        if "opentr.sh" in line and not line.lstrip().startswith("#")
    ]
    assert not offenders, (
        f"opentranscribe.sh names a file it does not ship: {offenders} (issue #613)"
    )


def test_dangling_reference_detector_ignores_a_commented_mention():
    """Must-stay-clean: a `#`-comment explaining the split (as this file now has) is fine."""
    source = "    # opentr.sh gets these from its prologue; this script does not.\n"
    offenders = [
        line
        for line in source.splitlines()
        if "opentr.sh" in line and not line.lstrip().startswith("#")
    ]
    assert offenders == []


_FAKE_DOCKER = r"""
docker() {
  # `docker` is a bash FUNCTION here (functions shadow PATH binaries for unquoted
  # command lookups), so every branch below must `return`, never `exit` — `exit`
  # inside a function terminates the WHOLE calling script, not just this call, which
  # would silently truncate restore_database mid-flight and look exactly like the
  # bug this file's set-e tests exist to catch. (Caught by running this fixture
  # itself under `bash -x` and watching it die after the first failing `start` call.)
  if [ "$1" = "info" ]; then return 0; fi
  if [ "$1" != "compose" ]; then return 0; fi
  shift
  while [ "$1" = "-f" ]; do shift 2; done
  verb="$1"; shift
  case "$verb" in
    # `stop` succeeds here: restore_database's own call to it is now CHECKED (a
    # dedicated follow-up finding fixed the fact that it used to be a bare, unchecked
    # statement — see test_restore_stop_failure_aborts_before_touching_the_database for
    # the fixture that models a failing stop and proves the abort). Modelling the common
    # case here keeps every other test in this family — which only cares about reaching
    # a completed restore — from having to know about that unrelated failure mode.
    stop) return 0 ;;
    # `start` (the restart_services array, invoked only after a successful replay)
    # deliberately FAILS instead: it is still a bare, unchecked statement (written for
    # opentr.sh's non-`set -e` context — a failed restart is not itself a reason to
    # treat the restore as failed), so THIS is what test_restore_arm_survives_set_e's
    # positive/negative pair now exercises: with the adapter, the failure is silently
    # absorbed and "Database restored successfully" still prints; without it, bare
    # `set -e` aborts the script right here, before that line.
    start) return 1 ;;
    ps) return 0 ;;
    exec)
      [ "$1" = "-T" ] && shift
      svc="$1"; shift
      if [ "$svc" != "postgres" ]; then return 0; fi
      cmd="$1"
      case "$cmd" in
        pg_dump) echo "-- fake dump output"; return 0 ;;
        psql)
          args="$*"
          case "$args" in
            *"count(*) FROM alembic_version"*) echo "1" ;;
            *"SELECT version_num FROM alembic_version"*) echo "abc123" ;;
            *"information_schema.tables"*) echo "1" ;;
            *"--single-transaction"*) cat >/dev/null ;;
            *) : ;;
          esac
          return 0
          ;;
        *) return 0 ;;
      esac
      ;;
    *) return 0 ;;
  esac
}
"""

_FAKE_DUMP = (
    "-- pg_dump fake\n"
    "CREATE TABLE public.alembic_version (version_num character varying);\n"
    "COPY public.alembic_version (version_num) FROM stdin;\n"
    "abc123\n"
    "\\.\n"
)


def _backup_restore_arm() -> str:
    # No backslash before `|` or `)`: GNU sed's BRE treats a bare `|` as literal (its
    # alternation is the `\|` EXTENSION) and a bare `)` as literal too (only `\(...\)` is
    # BRE grouping) — `_extract_case_block`'s other callers rely on the same bare-`)` rule
    # (e.g. `r"    update)"`). Escaping either here makes sed reject the pattern outright
    # ("Unmatched ) or \)"), which is a hard error, not a silent miss.
    return _extract_case_block(MANAGER, r"    backup|restore)", r"    config)")


def test_backup_arm_passes_the_resolved_compose_chain(tmp_path: Path):
    (tmp_path / ".env").write_text("")
    body = _backup_restore_arm()
    snippet = f"""
YELLOW=''; GREEN=''; RED=''; BLUE=''; NC=''
source {COMMON}
cd {tmp_path}
check_environment() {{ :; }}
require_db_helpers() {{ :; }}
get_compose_files() {{ echo "-f docker-compose.yml -f docker-compose.prod.yml"; }}
backup_database() {{ echo "BACKUP_CALL:$*"; }}
restore_database() {{ echo "RESTORE_CALL:$*"; }}
set -- backup
{body}
"""
    out = _run_shell(snippet)
    assert (
        "BACKUP_CALL:-f docker-compose.yml -f docker-compose.prod.yml ./opentranscribe.sh" in out
    ), out


def test_backup_arm_control_omitting_the_chain_does_not_match():
    """Must-fire control: an arm that forgets to pass the chain produces a different call."""
    called = 'backup_database "./opentranscribe.sh"'
    assert "BACKUP_CALL:-f docker-compose.yml" not in called


def test_restore_arm_forwards_every_flag_in_order(tmp_path: Path):
    (tmp_path / ".env").write_text("")
    body = _backup_restore_arm()
    snippet = f"""
YELLOW=''; GREEN=''; RED=''; BLUE=''; NC=''
source {COMMON}
cd {tmp_path}
check_environment() {{ :; }}
require_db_helpers() {{ :; }}
get_compose_files() {{ echo "-f docker-compose.yml"; }}
backup_database() {{ echo "BACKUP_CALL:$*"; }}
restore_database() {{ echo "RESTORE_CALL:$*"; }}
set -- restore --yes --no-safety-dump --migrate-forward /tmp/x.sql
{body}
"""
    out = _run_shell(snippet)
    assert (
        "RESTORE_CALL:-f docker-compose.yml ./opentranscribe.sh "
        "--yes --no-safety-dump --migrate-forward /tmp/x.sql" in out
    ), out


def test_restore_arm_control_a_dropped_shift_leaks_the_command_word(tmp_path: Path):
    """Must-fire control: an arm missing `shift` would forward "restore" itself as the
    first flag — prove the real arm does NOT do that.
    """
    body = _backup_restore_arm()
    assert 'cmd="$1"; shift' in body or 'cmd="$1"\n' not in body, (
        "expected the arm to shift off the command word before forwarding $@"
    )


def test_restore_arm_survives_set_e(tmp_path: Path):
    """The single most important control in this plan (issue #613).

    scripts/common.sh's restore_database is written for opentr.sh's execution semantics,
    which deliberately omit `set -e` — it has bare, unchecked `docker compose ...`
    statements (e.g. the post-restore `"${{restart_services[@]}}"` call below, whose exit
    status nothing reads) on the assumption that a failure there is non-fatal and the
    function should carry on. opentranscribe.sh runs `set -e` at the top of the file, so
    WITHOUT the `set +e` / `set -e` adapter around this call, any one of those unchecked
    commands failing would abort the WHOLE script mid-restore. (NOTE: a bare
    `[ -n "$x" ] && rm -f "$x"` — the shape ~10 of these statements take — turns out NOT to
    trip `set -e` even when $x is empty and the `&&` short-circuits: bash's errexit
    explicitly exempts a failing left-hand side of a `&&`/`||` list from triggering an
    abort, verified empirically (`bash -c 'set -e; [ -n "" ] && echo hi; echo REACHED'`
    prints REACHED). The REAL risk this test proves is the unchecked docker-compose calls,
    not that specific idiom.) This drives the REAL restore_database (sourced from the real
    common.sh), with only `docker` stubbed — and its `start` verb (used only by the
    post-restore restart, invoked once the matching-head success path is reached)
    deliberately failing — through a full --yes restore of a real plain-SQL dump.

    `stop` is deliberately NOT the failure this test injects: a follow-up finding made
    that call checked (see test_restore_stop_failure_aborts_before_touching_the_database),
    so a failing `stop` is now fatal with or without this adapter and can no longer
    demonstrate what this test exists to prove.
    """
    dump = tmp_path / "dump.sql"
    dump.write_text(_FAKE_DUMP)
    body = _backup_restore_arm()
    assert "set +e" in body and "set -e" in body, "expected the set +e/set -e adapter in the arm"

    snippet = f"""
set -e
{_FAKE_DOCKER}
source {COMMON}
cd {tmp_path}
check_environment() {{ :; }}
require_db_helpers() {{ :; }}
get_compose_files() {{ echo "-f docker-compose.yml"; }}
set -- restore --yes {dump}
{body}
"""
    out = _run_shell(snippet)
    assert "Database restored successfully" in out, out
    assert "Restarting services" in out, (
        f"expected the matching-head success path to restart services: {out}"
    )


def test_restore_arm_survives_set_e_control_aborts_without_the_adapter(tmp_path: Path):
    """Must-fire control for the test above: the SAME snippet, SAME real restore_database,
    same failing `docker compose ... start` (restart_services) call, with only the
    `set +e`/`set -e` adapter lines stripped out of the arm — under the
    opentranscribe.sh-realistic ambient `set -e`, this must die (on the unchecked `start`
    call) before completing.
    """
    dump = tmp_path / "dump.sql"
    dump.write_text(_FAKE_DUMP)
    body = _backup_restore_arm()
    stripped_body = "\n".join(
        line for line in body.splitlines() if line.strip() not in ("set +e", "set -e")
    )

    snippet = f"""
set -e
{_FAKE_DOCKER}
source {COMMON}
cd {tmp_path}
check_environment() {{ :; }}
require_db_helpers() {{ :; }}
get_compose_files() {{ echo "-f docker-compose.yml"; }}
set -- restore --yes {dump}
{stripped_body}
"""
    out = _run_shell(snippet)
    assert "Database restored successfully" not in out, (
        f"expected set -e to abort mid-restore without the adapter, but it completed anyway: {out}"
    )


def test_restore_arm_reads_a_custom_postgres_db_from_env(tmp_path: Path):
    """Catches issue #613 §3.1(5): opentranscribe.sh has no `set -a; source .env` prologue
    (unlike opentr.sh), so without an explicit read of POSTGRES_DB a restore would silently
    target the DEFAULT database name on any install that customised it.
    """
    (tmp_path / ".env").write_text("POSTGRES_DB=custom_db\nPOSTGRES_USER=custom_user\n")
    body = _backup_restore_arm()
    snippet = f"""
YELLOW=''; GREEN=''; RED=''; BLUE=''; NC=''
source {COMMON}
cd {tmp_path}
check_environment() {{ :; }}
require_db_helpers() {{ :; }}
get_compose_files() {{ echo "-f docker-compose.yml"; }}
restore_database() {{ echo "DB_SEEN:$POSTGRES_DB USER_SEEN:$POSTGRES_USER"; }}
set -- restore --yes /tmp/x.sql
{body}
"""
    out = _run_shell(snippet)
    assert "DB_SEEN:custom_db" in out, (
        f"expected the arm to export POSTGRES_DB from .env before calling restore_database: {out}"
    )
    assert "USER_SEEN:custom_user" in out, out


def test_restore_arm_control_without_the_env_read_resolves_the_wrong_database(tmp_path: Path):
    """Must-fire control: an arm that never reads .env would resolve restore_database's own
    internal `${POSTGRES_DB:-opentranscribe}` default, never `custom_db`.
    """
    (tmp_path / ".env").write_text("POSTGRES_DB=custom_db\n")
    snippet = f"""
cd {tmp_path}
restore_database() {{ echo "DB_SEEN:${{POSTGRES_DB:-opentranscribe}}"; }}
restore_database
"""
    out = _run_shell(snippet)
    assert "DB_SEEN:opentranscribe" in out, (
        f"fixture is wrong: this must resolve the DEFAULT, not custom_db: {out}"
    )


def test_arms_handle_a_long_compose_chain(tmp_path: Path):
    """A 6-pair `-f` chain (base + prod + nginx + gpu + blackwell + local) must survive
    intact through every layer: the arm's call into restore_database, restore_database's
    own `local compose_files="$1"`, and every `docker compose $compose_files ...` call site
    inside it (including the restart_services array build) — all the way to a completed
    restore. A chain mangled anywhere along that path (e.g. collapsed by an errant quote, or
    truncated) breaks the fake docker stub's own `-f`-pair stripping loop, which cascades
    into `svc` resolving to something other than "postgres", every exec becoming a silent
    no-op, and restore verification failing on an empty actual_head — i.e. exactly the
    failure this test would catch, via the same "Database restored successfully" oracle
    test_restore_arm_survives_set_e uses.
    """
    dump = tmp_path / "dump.sql"
    dump.write_text(_FAKE_DUMP)
    chain = (
        "-f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.nginx.yml "
        "-f docker-compose.gpu.yml -f docker-compose.blackwell.yml -f docker-compose.local.yml"
    )
    body = _backup_restore_arm()
    snippet = f"""
set -e
{_FAKE_DOCKER}
source {COMMON}
cd {tmp_path}
check_environment() {{ :; }}
require_db_helpers() {{ :; }}
get_compose_files() {{ echo "{chain}"; }}
set -- restore --yes {dump}
{body}
"""
    out = _run_shell(snippet)
    assert "Database restored successfully" in out, (
        f"a 6-pair compose chain did not survive intact through restore_database: {out}"
    )
    assert "Restarting services" in out, out


def test_long_chain_control_a_collapsed_chain_breaks_the_stub_oracle():
    """Must-fire control: proves the oracle above actually distinguishes a correctly
    word-split chain from a collapsed one, using the fake docker stub's own logic directly.
    """
    collapsed = "-f docker-compose.yml -f docker-compose.prod.yml"  # one shell word if quoted
    # Simulate what the stub does: strip "-f X" pairs one at a time. A correctly split
    # chain (the real scenario — this string arrives ALREADY split by bash, since it is
    # embedded unquoted in `docker compose $compose_files ...`) consumes cleanly:
    words = collapsed.split()
    assert words[0] == "-f" and words[2] == "-f", "fixture is not actually multi-word"
    # A collapsed chain (hypothetically passed as ONE quoted argv element) would instead
    # present as a single word starting with "-f docker-compose.yml -f ...", which does not
    # equal the literal string "-f" — the stub's `[ "$1" = "-f" ]` test fails immediately.
    single_word = [collapsed]
    assert single_word[0] != "-f", "a collapsed chain must NOT look like a bare -f flag"


def test_restore_hold_message_names_the_invoking_front_end(tmp_path: Path):
    """The schema-mismatch hold branch must tell the operator to run the front end they
    actually have. Hardcoding "./opentr.sh" here (as the pre-#613 code did at the sibling
    `--no-restart` hold-branch line) would repeat the exact defect this issue fixes.

    Drives a real schema MISMATCH: the fake docker returns a different alembic head for the
    pre-restore read (current_head) than for the post-replay read (actual_head, which must
    match the dump so verification still passes and the flow reaches the restart decision).
    """
    dump = tmp_path / "dump.sql"
    dump.write_text(_FAKE_DUMP)
    counter = tmp_path / ".head_calls"
    fake_docker = _FAKE_DOCKER.replace(
        '*"SELECT version_num FROM alembic_version"*) echo "abc123" ;;',
        f"""*"SELECT version_num FROM alembic_version"*)
              if [ ! -f {counter} ]; then
                echo x > {counter}
                echo "differenthead"
              else
                echo "abc123"
              fi
              ;;""",
    )
    body = _backup_restore_arm()
    snippet = f"""
set -e
{fake_docker}
source {COMMON}
cd {tmp_path}
check_environment() {{ :; }}
require_db_helpers() {{ :; }}
get_compose_files() {{ echo "-f docker-compose.yml"; }}
set -- restore --yes {dump}
{body}
"""
    out = _run_shell(snippet)
    assert "STOPPED on purpose" in out, f"expected to reach the hold branch: {out}"
    assert "./opentranscribe.sh start dev" in out, out
    assert "./opentr.sh start dev" not in out, (
        f"the hold message hardcoded the wrong front end: {out}"
    )


# --------------------------------------------------------------------------- #
# Follow-up findings from a second adversarial audit of the #613/#614/#615
# session: restore_database's own return code, its unchecked `stop`, and a
# lock against two concurrent restores.
# --------------------------------------------------------------------------- #


def test_restore_no_safety_dump_control_demonstrates_the_bug_mechanism():
    """Must-fire control: the exact bash trap the real fix addresses.

    A function whose LAST statement is `[ -n "$x" ] && echo ...` returns 1 when $x is
    empty, even though the function did everything it was asked to do — the truthiness
    of an unrelated conditional becomes the function's own return code. This proves the
    trap is real bash behaviour, not a hypothetical, before trusting the positive test
    below that proves restore_database no longer falls into it.
    """
    proc = subprocess.run(
        ["bash", "-c", 'f() { x=""; [ -n "$x" ] && echo "x is set"; }; f; echo "rc=$?"'],
        capture_output=True,
        text=True,
        check=False,
    )
    assert "rc=1" in proc.stdout, (
        f"fixture is wrong: expected the bare `[ -n ... ] && echo` idiom to return 1 "
        f"when unset: {proc.stdout!r}"
    )


def test_restore_no_safety_dump_still_exits_zero_on_success(tmp_path: Path):
    """The actual regression: restore_database's last statement used to be
    `[ -n "$safety_dump_file" ] && echo ...`, and with `--no-safety-dump`
    `$safety_dump_file` is empty — so that conditional's own exit status (1) silently
    became restore_database's return code, on a completely successful restore.
    opentranscribe.sh's `backup|restore)` arm does `rc=$?; set -e; exit $rc` right after
    calling in, so `./opentranscribe.sh restore --yes --no-safety-dump <file>` exited 1
    after a clean restore. This drives the REAL restore_database end to end (real
    common.sh, only `docker` stubbed) and asserts the actual PROCESS exit code, not just
    the printed output — `_run_shell` elsewhere in this file discards the return code
    entirely, which is exactly how this class of bug stays invisible to a test suite.
    """
    dump = tmp_path / "dump.sql"
    dump.write_text(_FAKE_DUMP)
    body = _backup_restore_arm()
    snippet = f"""
set -e
{_FAKE_DOCKER}
source {COMMON}
cd {tmp_path}
check_environment() {{ :; }}
require_db_helpers() {{ :; }}
get_compose_files() {{ echo "-f docker-compose.yml"; }}
set -- restore --yes --no-safety-dump {dump}
{body}
"""
    proc = subprocess.run(
        ["bash", "-c", snippet],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
        check=False,
    )
    out = (proc.stdout + proc.stderr).strip()
    assert "Database restored successfully" in out, out
    assert proc.returncode == 0, (
        f"restore --yes --no-safety-dump exited {proc.returncode} despite a successful "
        f"restore (expected 0): {out}"
    )


def test_restore_stop_failure_aborts_before_touching_the_database(tmp_path: Path):
    """A failing `docker compose ... stop` must abort the restore, not be silently
    absorbed. Every OTHER destructive step in restore_database (the safety dump, the
    drop/recreate, the replay, the verify) is already guarded with an explicit
    check-and-abort — this proves `stop` now is too: with the backend/celery services
    still possibly connected, proceeding to DROP DATABASE would be exactly the hazard
    the other guards exist to prevent.
    """
    dump = tmp_path / "dump.sql"
    dump.write_text(_FAKE_DUMP)
    fake_docker = _FAKE_DOCKER.replace("stop) return 0 ;;", "stop) return 1 ;;")
    body = _backup_restore_arm()
    snippet = f"""
set -e
{fake_docker}
source {COMMON}
cd {tmp_path}
check_environment() {{ :; }}
require_db_helpers() {{ :; }}
get_compose_files() {{ echo "-f docker-compose.yml"; }}
set -- restore --yes {dump}
{body}
"""
    out = _run_shell(snippet)
    assert "Could not stop application services" in out, out
    assert "Database restored successfully" not in out, (
        f"a failing stop must abort the restore, not complete it: {out}"
    )
    assert "Dropping and recreating" not in out, (
        f"restore_database must never reach DROP DATABASE when the stop it depends on failed: {out}"
    )


def test_restore_stop_failure_control_the_fixture_actually_fails_stop():
    """Must-fire control: the `.replace()` above targets the exact stub line — if that
    line's text ever changes, the replace silently no-ops and the test above would pass
    for the wrong reason (stop still succeeding).
    """
    assert "stop) return 0 ;;" in _FAKE_DOCKER, (
        "fixture text changed — update the .replace() in the test above to match"
    )


def test_restore_refuses_while_another_restore_holds_the_lock(tmp_path: Path):
    """Two concurrent restore_database invocations must not race on the destructive
    portion: both would read current_head, both would take a safety dump, and the
    second's DROP DATABASE ... WITH (FORCE) could kill the first's replay mid-transaction.
    Simulated deterministically (no real timing race) by pre-acquiring the exact lock
    file restore_database uses from a background holder, polling until it is actually
    held — the same `flock -n -w 0 <file> true` technique
    scripts/safe-precommit-selftest.sh uses for the identical class of hazard — before
    driving a real restore attempt against it.
    """
    dump = tmp_path / "dump.sql"
    dump.write_text(_FAKE_DUMP)
    (tmp_path / "backups").mkdir()
    lock_file = tmp_path / "backups" / ".restore.lock"

    holder = subprocess.Popen(
        ["bash", "-c", f'exec 9>"{lock_file}"; flock 9; sleep 10'],
    )
    try:
        for _ in range(50):
            probe = subprocess.run(["bash", "-c", f'flock -n -w 0 "{lock_file}" true'], check=False)
            if probe.returncode != 0:
                break
            time.sleep(0.1)
        else:
            holder.terminate()
            pytest.fail("background holder never actually acquired the lock")

        body = _backup_restore_arm()
        snippet = f"""
set -e
{_FAKE_DOCKER}
source {COMMON}
cd {tmp_path}
check_environment() {{ :; }}
require_db_helpers() {{ :; }}
get_compose_files() {{ echo "-f docker-compose.yml"; }}
set -- restore --yes {dump}
{body}
"""
        out = _run_shell(snippet)
    finally:
        holder.terminate()
        try:
            holder.wait(timeout=5)
        except subprocess.TimeoutExpired:
            # SIGTERM didn't land in time (sleep 10 ignoring it would be unusual, but a
            # leaked process holding the lock file open would make every OTHER test in
            # this module that touches a `.restore.lock` flaky) — escalate rather than
            # silently leaving it running.
            holder.kill()
            holder.wait(timeout=5)

    assert "already in progress" in out, out
    assert "Database restored successfully" not in out, (
        f"a restore must not proceed while another one holds the lock: {out}"
    )
    assert "Dropping and recreating" not in out, (
        f"restore_database must refuse before ever reaching DROP DATABASE: {out}"
    )


def test_restore_lock_is_released_after_a_successful_restore(tmp_path: Path):
    """Regression guard: the lock must not leak. Two SEQUENTIAL restores against the
    same directory (same lock file) must both succeed — if the first restore left the
    lock held, the second would report "already in progress" for no real reason.

    Two SEPARATE subprocess invocations, not two calls inside one snippet: the arm's
    own body ends in `exit $rc`, so a second `{{body}}` chained after the first inside
    one bash process would never even run — exactly matching how two real, sequential
    `./opentranscribe.sh restore ...` invocations actually happen (two processes).
    """
    dump = tmp_path / "dump.sql"
    dump.write_text(_FAKE_DUMP)
    body = _backup_restore_arm()
    snippet = f"""
set -e
{_FAKE_DOCKER}
source {COMMON}
cd {tmp_path}
check_environment() {{ :; }}
require_db_helpers() {{ :; }}
get_compose_files() {{ echo "-f docker-compose.yml"; }}
set -- restore --yes {dump}
{body}
"""
    first = _run_shell(snippet)
    second = _run_shell(snippet)
    assert "Database restored successfully" in first, first
    assert "already in progress" not in second, (
        f"the lock from the first restore leaked into the second: {second}"
    )
    assert "Database restored successfully" in second, second


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
        + _extract_function(INSTALLER, "resolve_default_branch")
        + _extract_function(INSTALLER, "download_release_manifest_artifacts")
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
        + _extract_function(MANAGER, "read_env_value")
        + _extract_function(MANAGER, "resolve_default_branch")
        + _extract_function(MANAGER, "deployment_ref")
        + _extract_function(MANAGER, "resolve_config_ref")
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
