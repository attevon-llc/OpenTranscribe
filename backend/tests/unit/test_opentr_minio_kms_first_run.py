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
