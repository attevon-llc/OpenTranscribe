"""`read_env_value()` strips dotenv inline comments correctly (#590 slice).

`opentranscribe.sh` read config values out of `.env` with ad-hoc `grep | cut` pipelines that
treated ANY `#` as the start of a comment — including one embedded in a value with no
preceding whitespace. `ENVIRONMENT=production  # prod box` is the dotenv-standard way to
comment a line, and yielded `production#prodbox`, silently breaking every prod-hardening
string comparison built on that read (`case "$env_name" in development|dev|...)`).

`scripts/common.sh`'s `read_env_value()` is the fix: a `#` only starts a comment when
preceded by whitespace, matching the dotenv inline-comment convention. `opentranscribe.sh`
also carries a byte-identical fallback definition (guarded by
`declare -F read_env_value`) for installs whose `scripts/common.sh` predates this fix — see
`test_opentr_minio_kms_first_run.py` for the established fallback-definition test pattern
this reuses.

Three tiers:
1. Behavioural — the real function, sourced from the real `scripts/common.sh`, run against a
   scratch env file (never named `.env` — that literal path is denied to this environment's
   own tooling, by design).
2. Static drift guard — no remaining `KEY=$(grep ... .env` assignment for the converted
   call sites in `opentranscribe.sh`, and the three secret reads (REDIS_PASSWORD /
   JWT_SECRET_KEY / ENCRYPTION_KEY) still use the raw `cut -d= -f2-` form (they may
   legitimately contain a `#`, even ` #`, so routing them through the comment-stripping
   helper would silently truncate a real secret).
3. Standalone guard — `opentranscribe.sh` defines its own fallback so it keeps working on an
   install whose `scripts/common.sh` is missing or predates this fix.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
OPENTR_SH = REPO_ROOT / "opentr.sh"
MANAGER = REPO_ROOT / "opentranscribe.sh"
COMMON = REPO_ROOT / "scripts" / "common.sh"

pytestmark = pytest.mark.skipif(
    not OPENTR_SH.exists() or not COMMON.exists() or not MANAGER.exists(),
    reason="opentr.sh / scripts/common.sh / opentranscribe.sh not present in this checkout",
)

# The 12 keys converted to read_env_value() in opentranscribe.sh (issue #590 slice).
CONVERTED_KEYS = [
    "MODEL_CACHE_DIR",
    "FORCE_CPU_MODE",
    "NGINX_SERVER_NAME",
    "ENVIRONMENT",
    "BACKEND_PORT",
    "OT_IMAGE_TAG",
    "POSTGRES_USER",
    "POSTGRES_DB",
    "BACKUP_HOST_PATH",
]

SECRET_KEYS = ["REDIS_PASSWORD", "JWT_SECRET_KEY", "ENCRYPTION_KEY"]


# ─── behavioural ────────────────────────────────────────────────────────────


def _read_env_value(
    tmp_path: Path, content: str, key: str, env_filename: str = "scratch.env"
) -> str:
    """Source common.sh, call read_env_value against a scratch file, return stdout."""
    env_file = tmp_path / env_filename
    env_file.write_text(content)
    proc = subprocess.run(
        [
            "bash",
            "-c",
            f'source "{COMMON}"; '
            f"declare -F read_env_value >/dev/null || "
            f'{{ echo "FUNCTION_MISSING" >&2; exit 1; }}; '
            f'read_env_value "{key}" "{env_file}"',
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, f"read_env_value aborted: {proc.stderr}"
    return proc.stdout.rstrip("\n")


def test_inline_comment_with_preceding_whitespace_is_stripped(tmp_path):
    result = _read_env_value(tmp_path, "ENVIRONMENT=production  # prod box\n", "ENVIRONMENT")
    assert result == "production"


def test_no_comment_returns_full_value(tmp_path):
    result = _read_env_value(tmp_path, "ENVIRONMENT=production\n", "ENVIRONMENT")
    assert result == "production"


def test_hash_with_no_preceding_whitespace_is_not_stripped(tmp_path):
    """A bare `#` inside a value (no preceding whitespace) is NOT a dotenv comment."""
    result = _read_env_value(tmp_path, "TOKEN=abc#def\n", "TOKEN")
    assert result == "abc#def"


def test_quoted_value_with_trailing_comment(tmp_path):
    result = _read_env_value(
        tmp_path, 'NGINX_SERVER_NAME="ot.example.com" # tls\n', "NGINX_SERVER_NAME"
    )
    assert result == "ot.example.com"


def test_value_containing_equals_sign_is_preserved(tmp_path):
    """-f2- not -f2: a value may legitimately contain '='."""
    result = _read_env_value(tmp_path, "VAL=a=b\n", "VAL")
    assert result == "a=b"


def test_absent_key_returns_empty_and_exits_zero(tmp_path):
    result = _read_env_value(tmp_path, "OTHER=1\n", "MISSING_KEY")
    assert result == ""


def test_missing_file_returns_empty_and_exits_zero(tmp_path):
    env_file = tmp_path / "does-not-exist.env"
    proc = subprocess.run(
        [
            "bash",
            "-c",
            f'source "{COMMON}"; read_env_value "ANY_KEY" "{env_file}"',
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0
    assert proc.stdout.rstrip("\n") == ""


# ─── static drift guard ─────────────────────────────────────────────────────


@pytest.mark.parametrize("key", CONVERTED_KEYS)
def test_converted_keys_no_longer_use_a_raw_grep_pipeline(key):
    """Guard against a future edit reintroducing a raw `grep | cut` read for a key this
    fix already converted to read_env_value() (issue #590).
    """
    source = MANAGER.read_text(encoding="utf-8")
    # A raw dotenv read assigns from a `grep ... .env` pipeline naming this exact key.
    pattern = re.compile(rf"=\$\(grep[^\n]*\^{re.escape(key)}=[^\n]*\.env")
    offenders = pattern.findall(source)
    assert not offenders, (
        f"{key} is still read via a raw grep pipeline in opentranscribe.sh: {offenders!r}"
    )


@pytest.mark.parametrize("key", SECRET_KEYS)
def test_secret_keys_still_use_raw_cut_not_read_env_value(key):
    """REDIS_PASSWORD / JWT_SECRET_KEY / ENCRYPTION_KEY may legitimately contain a `#`
    (even ` #`), so these reads must deliberately NOT go through read_env_value's
    comment-stripping.
    """
    source = MANAGER.read_text(encoding="utf-8")
    pattern = re.compile(rf"grep -E '\^{re.escape(key)}=' \.env[^\n]*cut -d= -f2-")
    assert pattern.search(source), (
        f"{key} no longer uses the raw cut -d= -f2- form — verify it wasn't routed "
        f"through read_env_value, which would truncate a secret containing ' #'"
    )


# ─── standalone guard ────────────────────────────────────────────────────────


def test_opentranscribe_sh_defines_a_standalone_fallback():
    """opentranscribe.sh ships to end users independent of scripts/common.sh (sourced
    conditionally). It must define its own read_env_value so every command keeps working
    on an install whose common.sh predates this fix.
    """
    source = MANAGER.read_text(encoding="utf-8")
    assert "declare -F read_env_value" in source, (
        "opentranscribe.sh has no standalone fallback for read_env_value — an install "
        "without scripts/common.sh (or one that predates this fix) would abort with "
        "'command not found' on the first converted call site"
    )


def test_common_sh_defines_read_env_value():
    source = COMMON.read_text(encoding="utf-8")
    assert "read_env_value() {" in source
