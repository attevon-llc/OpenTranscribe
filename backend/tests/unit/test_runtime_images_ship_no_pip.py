"""Runtime images must not ship pip.

pip vendors its own copies of urllib3, requests, etc. and those lag the app's pinned
versions, so every vulnerability scan flags the vendored copy (e.g. pip 26.2.1 bundling
urllib3 2.7.0 while the app runs 2.8.0). Nothing runs pip after the build, so the final
stage deletes it. These are static checks over the Dockerfile text.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[2]
DOCKERFILES = ["Dockerfile.prod", "Dockerfile.lite", "Dockerfile.blackwell"]
# Blackwell is built on the NVIDIA PyTorch image, which has no app user and runs as the
# image default, so the final-USER assertion only applies to the python:slim images.
NON_ROOT_IMAGES = {"Dockerfile.prod", "Dockerfile.lite"}

pytestmark = pytest.mark.skipif(
    not all((BACKEND / name).exists() for name in DOCKERFILES),
    reason="runtime Dockerfiles not in this checkout",
)


def _final_stage_instructions(name: str) -> list[str]:
    """Logical instructions (continuations joined, comments dropped) of the last stage."""
    text = (BACKEND / name).read_text(encoding="utf-8")
    text = re.sub(r"\\\n", " ", text)
    lines = [ln.strip() for ln in text.splitlines()]
    instructions = [ln for ln in lines if ln and not ln.startswith("#")]
    last_from = max(i for i, ln in enumerate(instructions) if ln.upper().startswith("FROM "))
    return instructions[last_from:]


def _removes_pip(instruction: str) -> bool:
    if not instruction.upper().startswith("RUN "):
        return False
    return bool(
        re.search(r"\brm\b[^;&|]*\bpip\b", instruction)
        or re.search(r"\brm\b[^;&|]*/pip(-\[0-9\]\*|\*)?", instruction)
        or re.search(r"pip\s+uninstall\b[^;&|]*\bpip\b", instruction)
    )


@pytest.mark.parametrize("name", DOCKERFILES)
def test_final_stage_removes_pip_after_last_pip_install(name):
    instrs = _final_stage_instructions(name)
    removals = [i for i, ins in enumerate(instrs) if _removes_pip(ins)]
    assert removals, f"{name}: final stage never removes pip"
    installs = [
        i
        for i, ins in enumerate(instrs)
        if ins.upper().startswith("RUN ") and re.search(r"\bpip\d?\s+install\b", ins)
    ]
    if installs:
        assert max(installs) < max(removals), (
            f"{name}: pip is removed before the last `pip install` in the final stage"
        )


@pytest.mark.parametrize("name", sorted(NON_ROOT_IMAGES))
def test_final_user_is_non_root_app_user(name):
    instrs = _final_stage_instructions(name)
    users = [ins.split(None, 1)[1].strip() for ins in instrs if ins.upper().startswith("USER ")]
    assert users, f"{name}: no USER instruction in final stage"
    assert users[-1] == "appuser", f"{name}: final USER is {users[-1]!r}, expected 'appuser'"
    # Removal needs root, so it must be bracketed by USER root ... USER appuser.
    removal = next(i for i, ins in enumerate(instrs) if _removes_pip(ins))
    before = [
        ins.split(None, 1)[1].strip() for ins in instrs[:removal] if ins.upper().startswith("USER ")
    ]
    assert before and before[-1] == "root", f"{name}: pip removal does not run as USER root"
