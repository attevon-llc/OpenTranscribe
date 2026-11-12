"""The prettier pre-commit hook and ``frontend/package.json`` must pin the same prettier.

``.pre-commit-config.yaml`` runs prettier from ``pre-commit/mirrors-prettier`` at a fixed ``rev``.
With no prettier in ``frontend/package.json``, ``npx prettier`` fetched the newest release, which
lays out multi-value custom properties differently; on 2026-11-11 that reformatted three global
stylesheets, the hook then reverted them, and the commit failed for reasons unrelated to the change.

Pinning prettier in the frontend to exactly the hook's version makes ``npx prettier`` and the hook
the same formatter. This test keeps the two pins equal, so one is never bumped without the other.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


def _hook_rev() -> str:
    config = (REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    match = re.search(
        r"repo: https://github\.com/pre-commit/mirrors-prettier\n\s+rev: v?(\S+)", config
    )
    assert match, "the mirrors-prettier hook (and its rev) is missing from .pre-commit-config.yaml"
    return match.group(1)


def _frontend_pin() -> str:
    package = json.loads((REPO_ROOT / "frontend" / "package.json").read_text(encoding="utf-8"))
    pin = package.get("devDependencies", {}).get("prettier")
    assert isinstance(pin, str) and pin, (
        "frontend/package.json has no prettier devDependency, so `npx prettier` floats"
    )
    return pin


def test_frontend_prettier_is_pinned_exactly():
    pin = _frontend_pin()
    assert re.fullmatch(r"\d+\.\d+\.\d+", pin), (
        f"prettier must be an exact version, not a range: {pin!r}. A range lets npm resolve a "
        "newer prettier than the hook runs."
    )


def test_frontend_prettier_matches_the_hook_rev():
    assert _frontend_pin() == _hook_rev(), (
        "frontend/package.json prettier and the mirrors-prettier hook rev differ; bump both together"
    )
