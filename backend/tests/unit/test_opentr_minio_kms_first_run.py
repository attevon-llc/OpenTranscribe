"""A truly-fresh `cp .env.example .env` + `./opentr.sh start dev` must boot MinIO (#614).

MinIO's KMS auto-encryption (`MINIO_KMS_AUTO_ENCRYPTION=on`, the `.env.example` default)
requires `MINIO_KMS_SECRET_KEY` in the form `<key-name>:<base64-encoded-32-byte-key>` — see
the `minio` service's own comment in `docker-compose.yml`. `.env.example` ships
`MINIO_KMS_SECRET_KEY=CHANGE_ME_auto_generated_on_install`, which is not that format, so a
genuinely fresh checkout refused to boot MinIO at all:

    FATAL Failed to connect to KMS: kms: invalid secret key format

until an operator manually generated a real key — reproduced live against the real MinIO
image (RELEASE.2025-09-07T16-13-09Z) while building this fix: the placeholder crash-loops
the container (`unhealthy`, `FATAL Failed to connect to KMS...`), and a generated
`opentranscribe-key:$(openssl rand -base64 32)` boots it `healthy` immediately with no
KMS-related log line at all.

`scripts/install-offline-package.sh` and `windows-installer/generate-secrets.ps1` already
generate a value in this exact format for THEIR OWN first-run paths — this fix adds the same
generation to `scripts/common.sh` (`ensure_minio_kms_secret`) and wires it into `opentr.sh`'s
`start_app()` and `reset_and_init()`, the one first-run path neither of those covered.

Three tiers, matching the pattern in `test_opentr_reset_flag_parity.py` /
`test_shell_env_var_guards.py`:

1. Static — the function exists and both call sites use it (a `start_app`-only wiring would
   silently skip generation on `./opentr.sh reset dev`, which also brings MinIO up fresh).
2. Behavioural — the real function, sourced from the real `scripts/common.sh`, run against a
   scratch env file. This is deliberately NOT run against a file literally named `.env`:
   `.env` (bare, not `.env.example`) is a hard-denied path in this environment's own tooling
   permissions, by design, so every scratch file below uses a different name.
3. Regression guard linking the two files — `.env.example`'s shipped placeholder is exactly
   the string the function recognizes, so the fix and the file it targets cannot drift apart.
"""

from __future__ import annotations

import base64
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
OPENTR = REPO_ROOT / "opentr.sh"
MANAGER = REPO_ROOT / "opentranscribe.sh"
COMMON = REPO_ROOT / "scripts" / "common.sh"
ENV_EXAMPLE = REPO_ROOT / ".env.example"

pytestmark = pytest.mark.skipif(
    not OPENTR.exists() or not COMMON.exists() or not ENV_EXAMPLE.exists(),
    reason="opentr.sh / scripts/common.sh / .env.example not present in this checkout",
)

PLACEHOLDER = "CHANGE_ME_auto_generated_on_install"


def _function_body(text: str, name: str) -> str:
    """Source of one top-level ``name() { ... }`` block, closing brace included.

    Brace-counted from the function's own opening `{` (the
    `test_opentr_reset_flag_parity.py` pattern) rather than matched to the next
    top-level `^}`, which a nested `case`/`if` block containing its own brace
    group would fool.
    """
    match = re.search(rf"^{re.escape(name)}\(\)\s*\{{", text, re.MULTILINE)
    assert match, f"{name}() not found"
    start = match.end() - 1
    depth = 0
    for index in range(start, len(text)):
        char = text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    raise AssertionError(f"unbalanced braces scanning {name}()")


def _extract_case_block(script: Path, start_label: str, end_label: str) -> str:
    """Pull one `case` arm out of a script, from its label through the NEXT label's
    line (exclusive), with the terminating `;;` stripped so the result is directly
    executable as a standalone snippet (outside its enclosing `case`/`esac`, a bare
    `;;` is a syntax error). Same base technique as `test_install_upgrade_scripts.py`'s
    helper of the same name — duplicated rather than imported, since these are
    independent test modules and pytest collection order should not matter for either;
    the `;;`-stripping is new here because this file's tests assert on the real process
    exit code, not just substring containment, so a trailing syntax error would fail
    every behavioural test even when the wiring under test is correct.
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
    stripped = body.rstrip()
    assert stripped.endswith(";;"), f"expected the case arm to end in ';;': {body!r}"
    return stripped[: -len(";;")]


# ─── static ─────────────────────────────────────────────────────────────────


def test_ensure_minio_kms_secret_is_defined_in_common_sh():
    source = COMMON.read_text(encoding="utf-8")
    assert "ensure_minio_kms_secret() {" in source


@pytest.mark.parametrize("function_name", ["start_app", "reset_and_init"])
def test_both_start_and_reset_call_ensure_minio_kms_secret(function_name):
    """Both functions bring MinIO up fresh, so both must generate the key first.

    `start_app()`-only wiring would leave `./opentr.sh reset dev` (which does
    `docker compose down -v` then a full `up`) hitting the exact same placeholder
    crash on its very first real-world use: a reset IS a fresh boot of every
    volume, MinIO's included.
    """
    body = _function_body(OPENTR.read_text(encoding="utf-8"), function_name)
    assert "ensure_minio_kms_secret" in body, (
        f"{function_name}() never calls ensure_minio_kms_secret — a fresh .env with the "
        f".env.example placeholder still crash-loops MinIO from this path (issue #614)"
    )


def test_the_function_extractor_can_actually_fail():
    """Guard the guard: a brace-matcher that finds nothing would pass every case above."""
    with pytest.raises(AssertionError):
        _function_body("echo hello\n", "not_a_real_function")


def test_opentranscribe_sh_start_arm_calls_ensure_minio_kms_secret():
    """A follow-up finding on this same issue (#613/#614): #613 promoted
    backup_database/restore_database to opentranscribe.sh as the real production entry
    point, but ensure_minio_kms_secret was never wired in there — so a curl-install /
    production user got NO first-run protection at all, only `./opentr.sh` users did.
    `start)` is opentranscribe.sh's equivalent of opentr.sh's start_app(): the command
    that brings MinIO up from a truly fresh `.env`.
    """
    body = _extract_case_block(MANAGER, r"    start)", r"    stop)")
    assert "ensure_minio_kms_secret" in body, (
        "opentranscribe.sh's start) arm never calls ensure_minio_kms_secret — a fresh "
        ".env with the .env.example placeholder still crash-loops MinIO from a "
        "production/curl install (issue #613 follow-up)"
    )


def test_opentranscribe_sh_start_arm_guards_a_missing_function():
    """The call must be guarded, not unconditional: an install whose scripts/common.sh
    predates this fix (or is somehow missing — see require_db_helpers' own remedy
    message for the same scenario) must still be able to `start`, just without the
    auto-fix, rather than crashing on "unknown function" mid-startup.
    """
    body = _extract_case_block(MANAGER, r"    start)", r"    stop)")
    assert "declare -F ensure_minio_kms_secret" in body, (
        "the start) arm calls ensure_minio_kms_secret unconditionally — an install "
        "whose scripts/common.sh predates this fix would crash on `start` instead of "
        "just missing the auto-fix"
    )


def test_opentranscribe_sh_start_arm_actually_patches_a_fresh_env(tmp_path: Path):
    """Behavioural: drives the REAL start) arm body (sourced from the real common.sh,
    with docker/check_environment/etc. stubbed — this is a fast unit test, not an
    integration one) against a scratch `.env` carrying the shipped placeholder, and
    proves the file actually gets patched. Textual containment (the tests above) cannot
    tell a real call from one sitting inside a comment or a string.
    """
    (tmp_path / ".env").write_text(f"MINIO_KMS_SECRET_KEY={PLACEHOLDER}\n")
    body = _extract_case_block(MANAGER, r"    start)", r"    stop)")
    snippet = f"""
YELLOW=''; GREEN=''; RED=''; BLUE=''; NC=''
cd {tmp_path}
source "{COMMON}"
check_environment() {{ :; }}
fix_model_cache_permissions() {{ :; }}
get_compose_files() {{ echo ""; }}
docker() {{ return 0; }}
show_access_info() {{ :; }}
{body}
"""
    proc = subprocess.run(
        ["bash", "-c", snippet],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, f"start) arm aborted: {proc.stderr}"
    after = (tmp_path / ".env").read_text()
    match = re.search(r"^MINIO_KMS_SECRET_KEY=(.+)$", after, re.MULTILINE)
    assert match, f"MINIO_KMS_SECRET_KEY line missing after opentranscribe.sh start: {after!r}"
    assert match.group(1) != PLACEHOLDER, (
        f"opentranscribe.sh's start) arm ran but never actually replaced the "
        f"placeholder: {after!r} (stdout={proc.stdout!r})"
    )


# ─── behavioural: the real function against a scratch (non-`.env`) file ────


def _run_ensure(tmp_path: Path, initial_content: str) -> tuple[str, str]:
    """Source common.sh, run ensure_minio_kms_secret against a scratch file.

    Returns (file_contents_after, exported_shell_value). The scratch file is
    deliberately never named `.env` — a bare `.env` path is denied to this
    environment's own tooling, by design, and the function takes an explicit
    path argument for exactly this kind of test.
    """
    env_file = tmp_path / "scratch.env"
    env_file.write_text(initial_content)
    proc = subprocess.run(
        [
            "bash",
            "-c",
            # An explicit `declare -F` check, not reliance on `set -e` (whose
            # "command not found" behaviour has enough edge cases to be worth
            # not trusting here): without it, a bash-unknown-command failure
            # for `ensure_minio_kms_secret` would just fall through to the
            # final `echo` and exit 0 — indistinguishable from "correctly did
            # nothing", which is exactly the false-pass this guards against.
            f'source "{COMMON}"; '
            f"declare -F ensure_minio_kms_secret >/dev/null || "
            f'{{ echo "FUNCTION_MISSING" >&2; exit 1; }}; '
            f'ensure_minio_kms_secret "{env_file}"; '
            f'echo "EXPORTED=${{MINIO_KMS_SECRET_KEY:-}}"',
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, f"ensure_minio_kms_secret aborted: {proc.stderr}"
    exported = ""
    for line in proc.stdout.splitlines():
        if line.startswith("EXPORTED="):
            exported = line[len("EXPORTED=") :]
    return env_file.read_text(), exported


def _parse_kms_value(value: str) -> tuple[str, str]:
    name, _, b64 = value.partition(":")
    assert b64, f"generated value has no <name>:<base64> separator: {value!r}"
    return name, b64


@pytest.mark.parametrize(
    "initial",
    [
        f"MINIO_KMS_SECRET_KEY={PLACEHOLDER}\n",
        f"OTHER=1\nMINIO_KMS_SECRET_KEY={PLACEHOLDER}\nMORE=2\n",
    ],
)
def test_placeholder_becomes_a_format_minio_actually_accepts(tmp_path, initial):
    after, exported = _run_ensure(tmp_path, initial)

    match = re.search(r"^MINIO_KMS_SECRET_KEY=(.+)$", after, re.MULTILINE)
    assert match, f"MINIO_KMS_SECRET_KEY line missing after generation:\n{after}"
    generated = match.group(1)

    assert generated != PLACEHOLDER, "the placeholder was not replaced at all"
    name, b64 = _parse_kms_value(generated)
    assert name, "generated key has an empty key-name component"
    decoded = base64.b64decode(b64, validate=True)
    assert len(decoded) == 32, (
        f"MinIO KMS requires a 32-byte key; got {len(decoded)} bytes from {generated!r}"
    )
    # The file and the current shell's exported value must agree — opentr.sh
    # already `set -a; source ./.env`'d the placeholder before this runs, and
    # docker compose's variable interpolation prefers an inherited shell env
    # var over re-reading .env, so a patched file with a stale export would
    # leave the very `docker compose up` this exists to unblock still broken.
    assert exported == generated, (
        f"file was patched to {generated!r} but the shell's exported "
        f"MINIO_KMS_SECRET_KEY is {exported!r} — docker compose would still see the old value"
    )


def test_generated_keys_are_not_reused_across_runs():
    """Two independent scratch files must not coincidentally get the same key."""
    import tempfile

    with tempfile.TemporaryDirectory() as d1, tempfile.TemporaryDirectory() as d2:
        after1, _ = _run_ensure(Path(d1), f"MINIO_KMS_SECRET_KEY={PLACEHOLDER}\n")
        after2, _ = _run_ensure(Path(d2), f"MINIO_KMS_SECRET_KEY={PLACEHOLDER}\n")
    match1 = re.search(r"^MINIO_KMS_SECRET_KEY=(.+)$", after1, re.MULTILINE)
    match2 = re.search(r"^MINIO_KMS_SECRET_KEY=(.+)$", after2, re.MULTILINE)
    assert match1 and match2, (
        f"MINIO_KMS_SECRET_KEY line missing after generation: {after1!r} {after2!r}"
    )
    assert match1.group(1) != match2.group(1), "generation is not actually random"


def test_an_already_real_value_is_left_untouched(tmp_path):
    """Idempotency: a second run (e.g. `start` then `reset`) must not rotate the key.

    MinIO decrypts existing objects with whatever key was active when they were
    written — rotating it on every invocation would make previously-encrypted
    data unreadable.
    """
    real_value = "opentranscribe-key:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
    after, exported = _run_ensure(tmp_path, f"MINIO_KMS_SECRET_KEY={real_value}\n")
    assert after.strip() == f"MINIO_KMS_SECRET_KEY={real_value}"
    # Not the placeholder case, so no re-export is expected or needed.
    assert exported in ("", real_value)


def test_an_empty_value_is_left_untouched(tmp_path):
    """An operator who deliberately blanked the key (KMS off) made a real choice.

    Generating a key nobody asked for on every invocation would be a surprise,
    not a fix — this function only replaces the SHIPPED placeholder.
    """
    after, _ = _run_ensure(tmp_path, "MINIO_KMS_SECRET_KEY=\n")
    assert after.strip() == "MINIO_KMS_SECRET_KEY="


def test_two_matching_lines_refuse_rather_than_guess(tmp_path):
    """A follow-up finding on this same issue: `sed -i` with no line-count guard could
    silently pick the wrong line (`tail -1`) or rewrite an ambiguous file if `.env`
    somehow ends up with two `MINIO_KMS_SECRET_KEY=` lines — e.g. a real key first and a
    leftover placeholder line second. Must fail closed and name the fix, not guess.
    """
    env_file = tmp_path / "scratch.env"
    real_value = "opentranscribe-key:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
    env_file.write_text(f"MINIO_KMS_SECRET_KEY={real_value}\nMINIO_KMS_SECRET_KEY={PLACEHOLDER}\n")
    proc = subprocess.run(
        [
            "bash",
            "-c",
            f'source "{COMMON}"; '
            f"declare -F ensure_minio_kms_secret >/dev/null || "
            f'{{ echo "FUNCTION_MISSING" >&2; exit 2; }}; '
            f'ensure_minio_kms_secret "{env_file}"',
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 1, (
        f"expected ensure_minio_kms_secret to fail closed on an ambiguous file, "
        f"got rc={proc.returncode}, stdout={proc.stdout!r}, stderr={proc.stderr!r}"
    )
    after = env_file.read_text()
    assert after == f"MINIO_KMS_SECRET_KEY={real_value}\nMINIO_KMS_SECRET_KEY={PLACEHOLDER}\n", (
        f"the file must be left completely untouched when the guard refuses: {after!r}"
    )


def test_two_matching_lines_control_a_single_line_is_unaffected(tmp_path):
    """Must-stay-clean control: the guard must not fire on the ordinary one-line case
    the rest of this file already exercises — only on genuine ambiguity.
    """
    after, _ = _run_ensure(tmp_path, f"MINIO_KMS_SECRET_KEY={PLACEHOLDER}\n")
    match = re.search(r"^MINIO_KMS_SECRET_KEY=(.+)$", after, re.MULTILINE)
    assert match and match.group(1) != PLACEHOLDER, (
        f"the single-line case must still generate normally: {after!r}"
    )


def test_a_missing_env_file_is_a_silent_noop(tmp_path):
    """Called before `.env` exists (e.g. a probe/help path) must not abort under `set -u`."""
    missing = tmp_path / "does-not-exist.env"
    proc = subprocess.run(
        ["bash", "-c", f'set -uo pipefail; source "{COMMON}"; ensure_minio_kms_secret "{missing}"'],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert not missing.exists()


# ─── the two files cannot drift apart ──────────────────────────────────────


def test_env_example_placeholder_is_exactly_what_the_function_recognizes():
    """If either side's placeholder string changes alone, generation silently stops firing."""
    source = ENV_EXAMPLE.read_text(encoding="utf-8")
    match = re.search(r"^MINIO_KMS_SECRET_KEY=(.+)$", source, re.MULTILINE)
    assert match, ".env.example no longer declares MINIO_KMS_SECRET_KEY"
    assert match.group(1) == PLACEHOLDER, (
        f".env.example's MINIO_KMS_SECRET_KEY placeholder is {match.group(1)!r}, "
        f"but ensure_minio_kms_secret() only recognizes {PLACEHOLDER!r} — "
        "generation would silently never fire on a real fresh checkout"
    )
