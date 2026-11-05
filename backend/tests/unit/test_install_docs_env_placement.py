"""An installer env var must sit on ``bash``, not on ``curl``.

``VAR=1 curl ... | bash`` sets ``VAR`` for the ``curl`` process only; the piped ``bash``
(the installer) never sees it, so a documented "unattended CPU install" silently performs
a GPU install. The working form is ``curl ... | VAR=1 bash``.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

# `NAME=value curl ... | [sudo] [env] bash|sh` on one line.
VAR_ON_CURL = re.compile(
    r"^\s*(?:[A-Z_][A-Z0-9_]*=\S+\s+)+curl\b[^\n]*\|\s*(?:sudo\s+)?(?:ba)?sh\b",
    re.MULTILINE,
)
SCANNED_SUFFIXES = {".md", ".mdx", ".sh"}


def _tracked_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO_ROOT, capture_output=True, check=True
    ).stdout
    names = [n for n in out.decode().split("\0") if n]
    return [REPO_ROOT / n for n in names if Path(n).suffix in SCANNED_SUFFIXES]


def test_pattern_catches_the_broken_form_and_accepts_the_working_one():
    broken = "OPENTRANSCRIBE_FORCE_CPU=1 curl -fsSL https://example.test/x.sh | bash"
    working = "curl -fsSL https://example.test/x.sh | OPENTRANSCRIBE_FORCE_CPU=1 bash"
    assert VAR_ON_CURL.search(broken)
    assert not VAR_ON_CURL.search(working)


def test_no_documented_install_command_puts_an_env_var_on_curl():
    offenders = []
    for path in _tracked_files():
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        offenders += [
            f"{path.relative_to(REPO_ROOT)}: {m.group(0).strip()[:90]}"
            for m in VAR_ON_CURL.finditer(text)
        ]
    assert offenders == [], "env var must be set on bash, not curl:\n" + "\n".join(offenders)
