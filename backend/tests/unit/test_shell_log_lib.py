"""``scripts/lib/log.sh`` is the one home for dev-script colours, ``print_*`` and ``die``.

Eleven dev scripts used to carry byte-identical copies. These tests run the real library in a
real ``bash`` (no mocks) and pin the exact output the scripts always produced, then check that
the converted scripts source it and no longer redefine the helpers.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
LOG_SH = REPO / "scripts" / "lib" / "log.sh"
CONVERTED = [
    "scripts/build-all.sh",
    "scripts/docker-build-push.sh",
    "scripts/setup-remote-builder.sh",
    "scripts/security-scan.sh",
    "scripts/push-security-reports.sh",
]


def _bash(snippet: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", f'source "{LOG_SH}"; {snippet}'],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


@pytest.mark.parametrize(
    ("func", "code", "label"),
    [
        ("print_info", "0;34", "INFO"),
        ("print_success", "0;32", "SUCCESS"),
        ("print_warning", "1;33", "WARNING"),
        ("print_error", "0;31", "ERROR"),
    ],
)
def test_print_helpers_emit_coloured_level_prefix_on_stdout(
    func: str, code: str, label: str
) -> None:
    result = _bash(f"{func} 'hello world'")
    assert result.returncode == 0
    assert result.stdout == f"\033[{code}m[{label}]\033[0m hello world\n"
    assert result.stderr == ""


def test_print_header_frames_the_title() -> None:
    result = _bash("print_header 'My Title'")
    lines = result.stdout.splitlines()
    assert len(lines) == 3
    assert "My Title" in lines[1]
    assert lines[0] == lines[2]
    assert "=" * 40 in lines[0]


def test_die_prints_error_and_exits_with_given_code() -> None:
    result = _bash("die 'boom' 7; echo unreachable")
    assert result.returncode == 7
    assert "[ERROR]" in result.stdout
    assert "boom" in result.stdout
    assert "unreachable" not in result.stdout


def test_die_defaults_to_exit_1() -> None:
    assert _bash("die 'boom'").returncode == 1


def test_sourcing_twice_is_harmless_and_keeps_colours() -> None:
    result = _bash(f'source "{LOG_SH}"; printf "%s" "$RED$NC"')
    assert result.returncode == 0
    # The vars hold the literal backslash sequences; `echo -e` expands them later.
    assert result.stdout == r"\033[0;31m\033[0m"


@pytest.mark.parametrize("script", CONVERTED)
def test_converted_scripts_source_the_lib_and_drop_their_copies(script: str) -> None:
    text = (REPO / script).read_text()
    assert 'source "$(dirname "${BASH_SOURCE[0]}")/lib/log.sh"' in text
    for func in ("print_info", "print_success", "print_error", "print_warning"):
        assert not re.search(rf"^{func}\(\)", text, re.M), f"{script} still defines {func}"
    assert not re.search(r"^RED='", text, re.M), f"{script} still defines RED"
