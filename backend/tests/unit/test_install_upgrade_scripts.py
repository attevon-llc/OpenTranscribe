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
