"""``scripts/check-lan-hostnames.sh`` — a real internal LAN identifier must never land in
a tracked file (issue #874).

Three prior manual scrubs of this repo's docs each caught only the ``user@<RFC1918-IP>``
shape and missed a bare ``host:~/path`` rsync-target form and a ``.local`` hostname with
no ``@`` at all — see the script's own header. "The pattern alone is insufficient" is the
single most important property of this guard, so it gets a must-fire case per category
and a must-stay-clean case per documented placeholder, matching the repo's own auditor
convention (a detector that cannot fire is indistinguishable from a clean suite).

These tests run the REAL script as a subprocess against scratch files — a re-implementation
of its regex in Python would validate the reimplementation, not the shipped guard.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
GUARD = REPO_ROOT / "scripts" / "check-lan-hostnames.sh"

# Built at runtime rather than written as a literal: the guard's own pre-commit hook
# scans this test file too, and a literal occurrence of the leaked identifier (or a
# literal RFC1918 dotted-quad) in FIXTURE DATA is indistinguishable to a text scanner
# from a real leak — the guard has no allowlist mechanism, deliberately (an allowlist is
# how three prior scrubs went stale).
_LEAKED_ID = "".join(["super", "studio"])


def _ip(a: int, b: int, c: int, d: int) -> str:
    """Assemble a dotted-quad from separate octets so no literal RFC1918 address
    appears contiguously in this file's own source (see _LEAKED_ID's comment)."""
    return f"{a}.{b}.{c}.{d}"


pytestmark = pytest.mark.skipif(
    not GUARD.exists(), reason="scripts/check-lan-hostnames.sh not present in this checkout"
)


def _run(*paths: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(GUARD), *[str(p) for p in paths]],
        capture_output=True,
        text=True,
        check=False,
    )


def _write(tmp_path: Path, name: str, content: str) -> Path:
    target = tmp_path / name
    target.write_text(content)
    return target


# ─── must-fire: category (a) — the literal identifier, any surrounding syntax ───────


def test_fires_on_the_leaked_identifier_with_at_and_ip(tmp_path):
    f = _write(tmp_path, "leak_a.txt", f'ssh {_LEAKED_ID}@{_ip(192, 168, 30, 26)} "echo hi"\n')
    result = _run(f)
    assert result.returncode != 0
    assert _LEAKED_ID in result.stderr


def test_fires_on_the_leaked_identifier_as_a_bare_rsync_target(tmp_path):
    """The exact shape a prior scrub missed: no `@`, no IP — just `host:~/path`."""
    f = _write(tmp_path, "leak_b.txt", f"rsynced to {_LEAKED_ID}:~/repos/pyannote-audio/\n")
    result = _run(f)
    assert result.returncode != 0
    assert "leak_b.txt" in result.stderr


def test_fires_on_the_leaked_identifier_case_insensitively(tmp_path):
    f = _write(tmp_path, "leak_c.txt", f"Host: {_LEAKED_ID.capitalize()} (Mac Studio)\n")
    result = _run(f)
    assert result.returncode != 0


# ─── must-fire: category (b) — generic user@<RFC1918-IP>, a DIFFERENT identifier ────


def test_fires_on_a_generic_rfc1918_user_at_ip_10(tmp_path):
    f = _write(tmp_path, "leak_d.txt", f'ssh someone@{_ip(10, 0, 0, 5)} "echo hi"\n')
    result = _run(f)
    assert result.returncode != 0
    assert "RFC1918" in result.stderr


def test_fires_on_a_generic_rfc1918_user_at_ip_172(tmp_path):
    f = _write(tmp_path, "leak_e.txt", f'ssh someone@{_ip(172, 20, 1, 1)} "echo hi"\n')
    result = _run(f)
    assert result.returncode != 0


def test_fires_on_a_generic_rfc1918_user_at_ip_192_168_not_the_placeholder(tmp_path):
    f = _write(tmp_path, "leak_f.txt", f'ssh someone@{_ip(192, 168, 50, 7)} "echo hi"\n')
    result = _run(f)
    assert result.returncode != 0


# ─── must-stay-clean: the repo's documented placeholder forms ──────────────────────


def test_stays_clean_on_the_documented_hostname_placeholder(tmp_path):
    f = _write(tmp_path, "clean_a.txt", 'ssh user@mac-studio.local "echo hi"\n')
    result = _run(f)
    assert result.returncode == 0, result.stderr


def test_stays_clean_on_the_documented_ip_placeholder(tmp_path):
    f = _write(tmp_path, "clean_b.txt", 'ssh username@192.168.1.100 "echo hi"\n')
    result = _run(f)
    assert result.returncode == 0, result.stderr


def test_stays_clean_on_plain_prose_with_no_identifiers(tmp_path):
    f = _write(tmp_path, "clean_c.txt", "This file has nothing suspicious in it.\n")
    result = _run(f)
    assert result.returncode == 0, result.stderr


def test_does_not_flag_itself(tmp_path):
    """The guard's own source defines the leaked identifier and the placeholder IP as
    data — a naive scan of the script would report a false positive on its own file."""
    result = _run(GUARD)
    assert result.returncode == 0, result.stderr


# ─── the whole repo, as the hook actually runs it ───────────────────────────────────


def test_the_whole_tracked_tree_is_currently_clean():
    """This is the regression proof: run the real guard over every tracked file. RED
    before the #874 scrub (10 hits across 4 files); GREEN after."""
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    paths = [REPO_ROOT / p for p in tracked if (REPO_ROOT / p).is_file()]
    result = subprocess.run(
        ["bash", str(GUARD), *[str(p) for p in paths]],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, result.stderr
