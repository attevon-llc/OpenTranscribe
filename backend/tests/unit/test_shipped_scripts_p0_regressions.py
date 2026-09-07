"""Pins for five defects found by an adversarial audit of the SHIPPED self-hosted scripts
(`opentranscribe.sh`, `scripts/common.sh`) — several regressions introduced by the diar-native
branch. Fast, hermetic: no Docker, no network, no live stack, matching
`test_install_upgrade_scripts.py`'s convention.

Each test corresponds to one audit finding:

1. `opentranscribe.sh`'s own `fix_model_cache_permissions()` always wins over
   `scripts/common.sh`'s (bash: last definition wins, and this IS the intended, pinned
   behaviour — see `test_install_upgrade_scripts.py::test_local_fix_model_cache_permissions_still_wins`).
   Because it always wins, it must stay behaviourally IDENTICAL to common.sh's: same
   subdirectory list, same "check every subdirectory, not just the parent" ownership scan.
   It had silently drifted (missing `diar-native`/`nltk_data`/`sentence-transformers`/
   `opensearch-ml` from the mkdir list, and a parent-only ownership check), which meant an
   install created before the diar-native branch never got a writable `diar-native` bind-mount
   source and silently fell back to PyAnnote.
2. `ENGINE_DIARIZER_BACKEND` is compared case-sensitively in `opentranscribe.sh`, while the
   backend (`backend/app/transcription/config.py`) resolves it with `.strip().lower()`. A
   value like `Native`/`NATIVE` therefore silently skipped both the native sidecar and the
   issue #670 upgrade preflight guard, even though the backend runs native for that value.
3. `scripts/common.sh`'s `read_env_value` anchored on `^KEY=` and missed two spellings
   `docker compose` itself honours in a `.env` file: leading whitespace before the key, and an
   `export KEY=` prefix. A value spelled either way reached the container correctly while every
   `read_env_value` caller here read back empty.
4. The Blackwell `DIAR_NATIVE_IMAGE` pin ignored an operator's own `.env` value (shell-env-only
   `${DIAR_NATIVE_IMAGE:-...}` expansion) and `download-models diar-native` never applied it at
   all, so `start` and `download-models diar-native` could resolve to two different images.
5. `fix_model_cache_permissions` returning 1 (a real, non-fatal "could not chown" outcome, with
   its own warning already printed) aborted `opentranscribe.sh` outright under `set -e`, before
   the more specific exit-7 NOT_WRITABLE remedy in `download_models_diar_native` ever had a
   chance to print.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
MANAGER = REPO_ROOT / "opentranscribe.sh"
COMMON = REPO_ROOT / "scripts" / "common.sh"

pytestmark = pytest.mark.skipif(
    not MANAGER.exists(), reason="opentranscribe.sh not present in this checkout"
)


def _run_shell(snippet: str, env: dict[str, str] | None = None, cwd: Path | None = None) -> str:
    proc = subprocess.run(
        ["bash", "-c", snippet],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", **(env or {})},
        cwd=str(cwd) if cwd else None,
    )
    return (proc.stdout + proc.stderr).strip()


def _extract_function(script: Path, name: str) -> str:
    fn = subprocess.run(
        ["sed", "-n", f"/^{re.escape(name)}() {{/,/^}}/p", str(script)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert fn.strip(), f"{name}() not found in {script}"
    return fn


def _mkdir_targets(fn_source: str, cache_var: str) -> set[str]:
    """Every `.../<subdir>` literal that appears in an `mkdir -p ...` invocation, relative to
    the model-cache variable, across possibly-multiple mkdir statements in the function.
    """
    targets: set[str] = set()
    for mkdir_stmt in re.findall(r"mkdir -p(.*?)(?:\n\S|\Z)", fn_source, flags=re.S):
        for m in re.finditer(rf'"\${{?{cache_var}\}}?/([a-zA-Z0-9_-]+)"', mkdir_stmt):
            targets.add(m.group(1))
    return targets


# --------------------------------------------------------------------------- #
# 1. fix_model_cache_permissions parity between opentranscribe.sh and common.sh
# --------------------------------------------------------------------------- #


def test_fix_model_cache_permissions_dir_lists_match():
    """opentranscribe.sh's local definition always wins over common.sh's (bash: last
    definition wins — intentional, see test_install_upgrade_scripts.py). Because it always
    wins, the two subdirectory lists must not diverge, or an install gets a different set of
    writable bind-mount sources depending on nothing but historical accident.
    """
    local_fn = _extract_function(MANAGER, "fix_model_cache_permissions")
    common_fn = _extract_function(COMMON, "fix_model_cache_permissions")

    local_dirs = _mkdir_targets(local_fn, "MODEL_CACHE_DIR")
    common_dirs = _mkdir_targets(common_fn, "MODEL_CACHE_DIR")

    assert local_dirs, "could not find any mkdir -p targets in opentranscribe.sh's version"
    assert common_dirs, "could not find any mkdir -p targets in common.sh's version"
    assert local_dirs == common_dirs, (
        f"fix_model_cache_permissions()'s subdirectory list has diverged between "
        f"opentranscribe.sh ({sorted(local_dirs)}) and scripts/common.sh "
        f"({sorted(common_dirs)}) — opentranscribe.sh's ALWAYS wins (last definition), so "
        "a directory missing here is missing for every real install."
    )
    assert "diar-native" in local_dirs, (
        "diar-native must be created unconditionally — dockerd creates it root-owned "
        "otherwise, and the backend (appuser) cannot write a root-owned bind-mount source"
    )


def test_dir_list_parity_control_fires_on_a_synthetic_divergence(tmp_path: Path):
    """Must-fire control: a copy of opentranscribe.sh missing diar-native from its mkdir list
    (the exact regression this audit found) must be caught by the comparison above.
    """
    text = MANAGER.read_text()
    fn = _extract_function(MANAGER, "fix_model_cache_permissions")
    assert '"$MODEL_CACHE_DIR/diar-native"' in fn or "diar-native" in fn
    regressed_fn = re.sub(r'"\$MODEL_CACHE_DIR/diar-native"\s*', "", fn)
    regressed_text = text.replace(fn, regressed_fn)
    broken = tmp_path / "opentranscribe.sh"
    broken.write_text(regressed_text)

    local_dirs = _mkdir_targets(
        _extract_function(broken, "fix_model_cache_permissions"), "MODEL_CACHE_DIR"
    )
    common_dirs = _mkdir_targets(
        _extract_function(COMMON, "fix_model_cache_permissions"), "MODEL_CACHE_DIR"
    )
    assert local_dirs != common_dirs, "fixture is wrong: this must reproduce the divergence"


def test_fix_model_cache_permissions_checks_every_subdirectory_not_just_the_parent():
    """A parent-only ownership check misses a root-owned SUBdirectory (e.g. diar-native,
    created root-owned by dockerd before this fix existed) even when the parent itself is
    correctly owned by UID 1000. Both scripts' versions must loop over subdirectories.
    """
    for script, label in ((MANAGER, "opentranscribe.sh"), (COMMON, "scripts/common.sh")):
        fn = _extract_function(script, "fix_model_cache_permissions")
        assert re.search(r'"\$MODEL_CACHE_DIR"/\*/', fn), (
            f"{label}'s fix_model_cache_permissions() does not appear to loop over "
            f'subdirectories (expected a glob like "$MODEL_CACHE_DIR"/*/ in the ownership '
            f"check) — a parent-only check misses a root-owned subdirectory"
        )


# --------------------------------------------------------------------------- #
# 2. ENGINE_DIARIZER_BACKEND must be compared case-insensitively, matching the backend
# --------------------------------------------------------------------------- #


def _get_compose_files_source() -> str:
    return _extract_function(MANAGER, "get_compose_files")


def _preflight_upgrade_env_source() -> str:
    return _extract_function(MANAGER, "preflight_upgrade_env")


@pytest.mark.parametrize("raw_value", ["native", "Native", "NATIVE", "  native  "])
def test_get_compose_files_sidecar_gate_is_case_insensitive(tmp_path: Path, raw_value: str):
    """The sidecar-loading gate in get_compose_files() must treat 'Native'/'NATIVE' the same
    as 'native' — backend/app/transcription/config.py:357-366 resolves the value with
    .strip().lower() and defaults anything unrecognised TO native, so a case-sensitive
    comparison here silently skips the sidecar for a backend that is about to run it anyway.
    """
    fn_source = _get_compose_files_source()
    read_line = next(
        line for line in fn_source.splitlines() if "diar_backend=$(read_env_value" in line
    )
    assert "tr '[:upper:]' '[:lower:]'" in read_line, (
        "get_compose_files' ENGINE_DIARIZER_BACKEND read must lowercase the value at read "
        f"time (raw_value={raw_value!r} would otherwise fail the 'native' compare below): "
        f"{read_line!r}"
    )


def test_preflight_upgrade_env_diar_gate_is_case_insensitive():
    """Same case-fold requirement for the #670 upgrade preflight guard: an operator who wrote
    ENGINE_DIARIZER_BACKEND=Native must still get the hard-refusal-when-unprovisioned check,
    not have it silently skipped because 'Native' != 'native'.
    """
    fn_source = _preflight_upgrade_env_source()
    read_line = next(
        line for line in fn_source.splitlines() if "diar_backend=$(read_env_value" in line
    )
    assert "tr '[:upper:]' '[:lower:]'" in read_line, (
        f"preflight_upgrade_env's ENGINE_DIARIZER_BACKEND read is not case-folded: {read_line!r}"
    )


def test_case_insensitive_gate_control_fires_on_a_case_sensitive_compare():
    """Must-fire control: a bare `[ "$v" = "native" ]` against 'Native' is false — proving the
    case-fold above is load-bearing, not a no-op.
    """
    out = _run_shell('v="Native"; [ "$v" = "native" ] && echo MATCH || echo NOMATCH')
    assert out == "NOMATCH", "fixture is wrong: case-sensitive compare must reject 'Native'"

    out_folded = _run_shell(
        'v=$(echo "Native" | tr \'[:upper:]\' \'[:lower:]\'); [ "$v" = "native" ] && echo MATCH || echo NOMATCH'
    )
    assert out_folded == "MATCH"


# --------------------------------------------------------------------------- #
# 3. read_env_value must honour leading whitespace and `export KEY=`, like docker compose does
# --------------------------------------------------------------------------- #


def _read_env_value_snippet(env_contents: str, key: str, tmp_path: Path) -> str:
    env_file = tmp_path / "dotenv"
    env_file.write_text(env_contents)
    snippet = f"""
source {COMMON}
read_env_value {key} {env_file}
"""
    return _run_shell(snippet)


@pytest.mark.parametrize(
    "line,key,expected",
    [
        ("  ENGINE_DIARIZER_BACKEND=pyannote\n", "ENGINE_DIARIZER_BACKEND", "pyannote"),
        ("\tENGINE_DIARIZER_BACKEND=pyannote\n", "ENGINE_DIARIZER_BACKEND", "pyannote"),
        ("export OT_BLACKWELL_IMAGE_TAG=v0.5.0\n", "OT_BLACKWELL_IMAGE_TAG", "v0.5.0"),
        ("  export DEPLOYMENT_MODE=lite\n", "DEPLOYMENT_MODE", "lite"),
        ("NORMAL=value\n", "NORMAL", "value"),  # must-stay-clean: unaffected by the fix
    ],
)
def test_read_env_value_handles_spellings_docker_compose_honours(
    tmp_path: Path, line: str, key: str, expected: str
):
    assert _read_env_value_snippet(line, key, tmp_path) == expected


def test_read_env_value_does_not_match_a_key_that_is_only_a_suffix(tmp_path: Path):
    """Must-stay-clean control: stripping leading whitespace/`export ` must not turn the
    anchor into a substring match — `NOT_ENGINE_DIARIZER_BACKEND=x` must not satisfy a read
    of `ENGINE_DIARIZER_BACKEND`.
    """
    out = _read_env_value_snippet(
        "NOT_ENGINE_DIARIZER_BACKEND=x\n", "ENGINE_DIARIZER_BACKEND", tmp_path
    )
    assert out == ""


def test_read_env_value_control_the_old_anchor_misses_these_spellings(tmp_path: Path):
    """Must-fire control, reproducing the pre-fix regex: a bare `^KEY=` grep (no whitespace/
    export stripping) misses both spellings that docker compose itself honours.
    """
    env_file = tmp_path / "dotenv"
    env_file.write_text(
        "  ENGINE_DIARIZER_BACKEND=pyannote\nexport OT_BLACKWELL_IMAGE_TAG=v0.5.0\n"
    )
    old_regex_snippet = f"""
grep -E "^ENGINE_DIARIZER_BACKEND=" {env_file} | head -1 | cut -d= -f2-
"""
    assert _run_shell(old_regex_snippet) == "", "fixture is wrong: old anchor must miss this"


# --------------------------------------------------------------------------- #
# 4. Blackwell DIAR_NATIVE_IMAGE pin: honour an operator's .env value; download-models agrees
# --------------------------------------------------------------------------- #


def test_pin_honours_an_operators_own_env_value():
    fn = _extract_function(MANAGER, "pin_diar_native_image_for_blackwell")
    assert "read_env_value DIAR_NATIVE_IMAGE" in fn, (
        "pin_diar_native_image_for_blackwell must read an operator's own DIAR_NATIVE_IMAGE "
        "pin via read_env_value (a bare ${DIAR_NATIVE_IMAGE:-...} shell-env expansion cannot "
        "see a value that only lives in .env, silently discarding a private-registry pin)"
    )


def test_download_models_diar_native_applies_the_same_blackwell_pin():
    fn = _extract_function(MANAGER, "download_models_diar_native")
    assert "pin_diar_native_image_for_blackwell" in fn, (
        "download_models_diar_native must call pin_diar_native_image_for_blackwell before "
        "resolving its image, or `start` (sidecar at :blackwell) and `download-models "
        "diar-native` (export at the plain release tag) can silently disagree on a Blackwell host"
    )


def test_resolve_diar_native_downloader_image_prefers_the_pin():
    fn = _extract_function(MANAGER, "resolve_diar_native_downloader_image")
    assert "read_env_value DIAR_NATIVE_IMAGE" in fn, (
        "resolve_diar_native_downloader_image must honour a DIAR_NATIVE_IMAGE pin (from "
        ".env, however it got there) rather than unconditionally deriving from OT_IMAGE_TAG"
    )


def test_pin_persists_to_env_for_the_documented_subshell_usage(tmp_path: Path):
    """The documented `compose-files` usage is `docker compose $(./opentranscribe.sh
    compose-files) up` — a SEPARATE process runs that `up`, so an `export` made inside the
    completed `compose-files` subprocess cannot reach it. The pin must instead persist into
    .env, which `docker compose` re-reads from disk on every invocation regardless of process.
    """
    fn = _extract_function(MANAGER, "pin_diar_native_image_for_blackwell")
    assert ">> .env" in fn or ">>.env" in fn, (
        "pin_diar_native_image_for_blackwell must persist the computed pin into .env, not "
        "only export it in-process, or the compose-files $(...) usage never sees it"
    )
    # And it must not clobber a value that's already present.
    assert "grep" in fn and "DIAR_NATIVE_IMAGE=" in fn


def test_env_persistence_control_a_bare_export_is_invisible_to_a_subshell_caller():
    """Must-fire control demonstrating the mechanism: an export inside a command-substitution
    subshell never reaches the invoking shell.
    """
    out = _run_shell('out=$(export FOO=bar); echo "${FOO:-UNSET}"')
    assert out == "UNSET", "fixture is wrong: this must demonstrate the export not propagating"


# --------------------------------------------------------------------------- #
# 5. fix_model_cache_permissions returning 1 must not abort before the exit-7 remedy prints
# --------------------------------------------------------------------------- #


def test_fix_model_cache_permissions_failure_does_not_abort_under_set_e():
    """Every call site must tolerate a failed permission fix (fix_model_cache_permissions
    already prints its own warning and returns 1) and continue — under `set -e`, a bare call
    aborts the whole command before a later, more specific error (e.g. exit-7 NOT_WRITABLE in
    download_models_diar_native) ever gets a chance to run.
    """
    text = MANAGER.read_text()
    call_lines = [
        line for line in text.splitlines() if line.strip() == "fix_model_cache_permissions"
    ]
    assert not call_lines, (
        f"found {len(call_lines)} unguarded `fix_model_cache_permissions` call(s) — each "
        "must be `fix_model_cache_permissions || true` under this script's `set -e`:\n"
        + "\n".join(call_lines)
    )
    guarded_calls = re.findall(r"^\s*fix_model_cache_permissions \|\| true\s*$", text, re.M)
    assert len(guarded_calls) >= 5, (
        f"expected at least 5 guarded fix_model_cache_permissions call sites, found "
        f"{len(guarded_calls)}"
    )


def test_set_e_abort_control_an_unguarded_failing_call_does_abort(tmp_path: Path):
    """Must-fire control: proves the hazard is real — an unguarded function returning 1 under
    `set -e` DOES abort before a later statement runs.
    """
    script = tmp_path / "demo.sh"
    script.write_text(
        "#!/bin/bash\nset -e\nfail_fn() { return 1; }\nfail_fn\necho SHOULD_NOT_PRINT\n"
    )
    out = _run_shell(f"bash {script}")
    assert "SHOULD_NOT_PRINT" not in out, "fixture is wrong: set -e must abort here"

    script_guarded = tmp_path / "demo_guarded.sh"
    script_guarded.write_text(
        "#!/bin/bash\nset -e\nfail_fn() { return 1; }\nfail_fn || true\necho SHOULD_PRINT\n"
    )
    out_guarded = _run_shell(f"bash {script_guarded}")
    assert "SHOULD_PRINT" in out_guarded
