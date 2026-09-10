"""`.env` is not reliably `chmod 600` on install (issue #857).

`.env` holds `JWT_SECRET_KEY`, `ENCRYPTION_KEY`, and every DB/MinIO/OpenSearch/Redis
password. `cp .env.example .env` inherits the process umask (0644 on a default host), and
GNU `sed -i` preserves whatever mode it found — so the nine secrets
`setup-opentranscribe.sh`'s `_create_initial_env()` writes into a freshly-copied `.env`
landed world-readable, and stayed that way, on a normal install.

The pre-existing `chmod 600` lived **only** inside `scripts/common.sh`'s
`ensure_minio_kms_secret()`, gated on `case "$current" in *CHANGE_ME*)` — and
`_create_initial_env` replaces the `MINIO_KMS_SECRET_KEY` placeholder with a real value
*before* that function ever runs, so on the normal install path the case never matches and
the chmod was provably unreachable. A test asserting the *string* `chmod 600` merely
appears somewhere in the tree would have passed throughout — see
`test_the_string_alone_would_have_passed_throughout` below, which pins that trap directly
rather than just describing it.

The fix is one shared implementation, `ensure_env_permissions()`, called unconditionally
(not gated on any placeholder) from every path that creates or writes a live `.env`:
`scripts/common.sh` (canonical), a fallback of the same name in `setup-opentranscribe.sh`
(the bootstrap script, which cannot `source scripts/common.sh` — it may run before
`scripts/` exists locally), and call sites in `opentr.sh` / `opentranscribe.sh` beside
their existing `ensure_minio_kms_secret` calls.

These tests are STATIC — they scan the shell source, never execute the installer (which
pulls images and mutates the host). Same brace-matching technique as
`test_opentr_minio_kms_first_run.py` (duplicated, not imported — independent modules).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
OPENTR = REPO_ROOT / "opentr.sh"
MANAGER = REPO_ROOT / "opentranscribe.sh"
COMMON = REPO_ROOT / "scripts" / "common.sh"
SETUP = REPO_ROOT / "setup-opentranscribe.sh"

pytestmark = pytest.mark.skipif(
    not all(p.exists() for p in (OPENTR, MANAGER, COMMON, SETUP)),
    reason="opentr.sh / opentranscribe.sh / scripts/common.sh / setup-opentranscribe.sh "
    "not present in this checkout",
)


def _function_body(text: str, name: str) -> str:
    """Source of one top-level ``name() { ... }`` block, closing brace included.

    Brace-counted from the function's own opening ``{`` rather than matched to the
    next top-level ``^}``, which a nested ``case``/``if`` block containing its own
    brace group would fool.
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


def test_the_function_extractor_can_actually_fail():
    """Guard the guard: a brace-matcher that finds nothing would pass every case below."""
    with pytest.raises(AssertionError):
        _function_body("echo hello\n", "not_a_real_function")


def test_the_string_alone_would_have_passed_throughout():
    """Pins the exact vacuous-test trap this issue's plan calls out: a bare substring
    check for "chmod 600" was already true before the fix (common.sh's placeholder-gated
    call), so it is not evidence the ACTUAL install path is protected. This assertion
    documents that the substring alone is insufficient — it must stay true even after the
    fix, since ``ensure_env_permissions`` itself still contains the literal string.
    """
    assert "chmod 600" in COMMON.read_text(encoding="utf-8")
    # ...and yet the bug was real. The tests below check ASSOCIATION with the actual
    # `.env`-creating call sites, not mere presence in the file.


# ─── scripts/common.sh — the canonical implementation ───────────────────────────────


def test_common_sh_defines_ensure_env_permissions():
    source = COMMON.read_text(encoding="utf-8")
    assert "ensure_env_permissions() {" in source
    body = _function_body(source, "ensure_env_permissions")
    assert "chmod 600" in body


def test_ensure_minio_kms_secret_delegates_to_the_shared_helper():
    """One implementation, not two: the placeholder-gated chmod inside
    ensure_minio_kms_secret must call the shared helper rather than repeating its own
    inline chmod — otherwise a future edit to one silently stops matching the other."""
    body = _function_body(COMMON.read_text(encoding="utf-8"), "ensure_minio_kms_secret")
    assert "ensure_env_permissions" in body


# ─── setup-opentranscribe.sh — the bootstrap script's fallback + call sites ─────────


def test_setup_opentranscribe_defines_a_fallback():
    """This script is the BOOTSTRAP — it cannot `source scripts/common.sh` (may run
    before `scripts/` exists locally on a fresh `curl | bash`), so it needs its own copy,
    guarded so common.sh's wins when present (matches the existing
    read_env_value/ot_drain_gpu_workers fallback convention in opentranscribe.sh)."""
    source = SETUP.read_text(encoding="utf-8")
    assert "declare -F ensure_env_permissions" in source
    assert "ensure_env_permissions() {" in source
    # The fallback definition itself must actually chmod, not just exist as a stub.
    match = re.search(r"ensure_env_permissions\(\)\s*\{(?:[^{}]|\{[^{}]*\})*\}", source, re.DOTALL)
    assert match, "ensure_env_permissions() fallback body not found"
    assert "chmod 600" in match.group(0)


def test_create_initial_env_locks_permissions_immediately_after_the_copy():
    """THE bug this issue fixes: `_create_initial_env` writes nine secrets into a
    freshly-copied `.env` and never chmod'd it. RED before the fix — this is the one
    call site the plan's own investigation names as "provably unreachable" via the old
    placeholder-gated path."""
    body = _function_body(SETUP.read_text(encoding="utf-8"), "_create_initial_env")
    assert "cp .env.example .env" in body
    assert "ensure_env_permissions" in body
    # Must run BEFORE the sed block writes secrets into the file, not merely
    # somewhere in the function (a call after every sed has already run past a
    # world-readable window is a weaker fix, not the one described in the PR).
    copy_index = body.index("cp .env.example .env")
    first_secret_write = body.index("POSTGRES_PASSWORD=")
    permission_call = body.index("ensure_env_permissions")
    assert copy_index < permission_call < first_secret_write, (
        "ensure_env_permissions must run between the cp and the first secret sed, "
        "not after secrets have already been written to a world-readable file"
    )


def test_https_rerun_path_also_locks_permissions():
    """A re-run against a pre-existing `.env` that predates this fix must also end up
    locked down — this path runs whenever HTTPS/NGINX_SERVER_NAME is (re)configured,
    independent of whether `_create_initial_env` ran in this invocation."""
    source = SETUP.read_text(encoding="utf-8")
    match = re.search(
        r'echo "NGINX_SERVER_NAME=\$NGINX_SERVER_NAME" >> \.env'
        r".*?Updated \.env with NGINX_SERVER_NAME",
        source,
        re.DOTALL,
    )
    assert match, "the NGINX_SERVER_NAME .env-update block was not found where expected"
    assert "ensure_env_permissions" in match.group(0)


# ─── opentr.sh — both first-run paths that bring MinIO up fresh ─────────────────────


@pytest.mark.parametrize("function_name", ["start_app", "reset_and_init"])
def test_opentr_sh_locks_permissions_at_both_first_run_paths(function_name):
    """Same reasoning as the sibling ensure_minio_kms_secret test
    (test_opentr_minio_kms_first_run.py): a start_app-only wiring would leave
    `./opentr.sh reset dev` -- which also brings every volume up fresh -- unprotected."""
    body = _function_body(OPENTR.read_text(encoding="utf-8"), function_name)
    assert "ensure_env_permissions" in body, (
        f"{function_name}() never calls ensure_env_permissions — a .env left over from "
        f"an older install, or created by a path this fix doesn't cover, stays "
        f"world-readable (issue #857)"
    )


# ─── opentranscribe.sh — the production entry point ─────────────────────────────────


def test_opentranscribe_sh_start_arm_locks_permissions():
    from tests.unit.test_opentr_minio_kms_first_run import _extract_case_block

    body = _extract_case_block(MANAGER, r"    start)", r"    stop)")
    assert "ensure_env_permissions" in body, (
        "opentranscribe.sh's start) arm never calls ensure_env_permissions — a "
        "production/curl install's .env stays world-readable (issue #857)"
    )


def test_opentranscribe_sh_start_arm_guards_a_missing_function():
    from tests.unit.test_opentr_minio_kms_first_run import _extract_case_block

    body = _extract_case_block(MANAGER, r"    start)", r"    stop)")
    assert "declare -F ensure_env_permissions" in body, (
        "the call must be guarded — an install whose scripts/common.sh predates this "
        "fix must still be able to `start`, just without the auto-fix"
    )
