"""One boolean vocabulary for the process environment.

``ENABLE_OVERLAP_DETECTION=1`` read as OFF in two readers (``== "true"``), ``DIAR_OVERLAP``
used a negative list, ``PIPELINE_SCRATCH_SHARED`` accepted only the literal ``false``. Every
boolean env read goes through ``app.core.env.env_bool`` (true/1/yes/on, false/0/no/off).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parents[2] / "app"

#: ``os.getenv("X", ...).lower() == "true"`` and its in/!= variants: a bespoke boolean parser.
BESPOKE = re.compile(
    r"""(?:getenv|environ\.get)\(\s*["'](?P<name>[A-Z0-9_]+)["'][^)]*\)"""
    r"""(?:\s*\.strip\(\))?\s*\.lower\(\)\s*(?:==|!=|not in|in)\s*[("']"""
)

#: Names that are not application booleans, with the reason.
NOT_A_BOOLEAN = {
    "TESTING": "bootstrap guard read before app.core.env is importable in conftest and settings",
    "WHISPER_HYBRID_MODE": "tri-state: true | false | auto",
    "ENGINE_GPU_SPLIT": (
        "routes work to queues only the split overlay serves; opentranscribe.sh starts that "
        "overlay on the literal 'true', so a looser app-side reading would route to a queue "
        "nothing consumes (test_dispatch pins 'yes' as false)"
    ),
    "MEDIACMS_VERIFY_SSL": (
        "a TLS-verification switch: only false/0/no turn it off, any other spelling keeps it "
        "on (test_mediacms_source_resolution pins 'off' as verified)"
    ),
}


def _offenders() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in sorted(APP_DIR.rglob("*.py")):
        for match in BESPOKE.finditer(path.read_text(encoding="utf-8")):
            if match["name"] not in NOT_A_BOOLEAN:
                found.setdefault(str(path.relative_to(APP_DIR)), []).append(match["name"])
    return found


def test_no_bespoke_boolean_parser_remains():
    assert not _offenders()


def test_the_scan_fires_on_a_bespoke_parser():
    """Guard the guard: a scan that matches nothing would pass the check above forever."""
    assert BESPOKE.search('x = os.getenv("FOO", "true").lower() == "true"')
    assert BESPOKE.search('x = os.environ.get("FOO", "").strip().lower() in ("1", "true")')
    assert not BESPOKE.search('x = env_bool("FOO", True)')


@pytest.mark.parametrize("raw", ["0", "no", "off", "false"])
def test_scratch_sharing_turns_off_for_every_falsy_spelling(monkeypatch, raw):
    from app.core.scratch_paths import engine_shared_volume_enabled
    from app.core.scratch_paths import pipeline_scratch_shared

    monkeypatch.delenv("ENGINE_SHARED_VOLUME_ENABLED", raising=False)
    monkeypatch.setenv("PIPELINE_SCRATCH_SHARED", raw)
    assert pipeline_scratch_shared() is False
    assert engine_shared_volume_enabled() is False


def test_a_typo_keeps_the_documented_default(monkeypatch):
    from app.core.scratch_paths import pipeline_scratch_shared

    monkeypatch.setenv("PIPELINE_SCRATCH_SHARED", "flase")
    assert pipeline_scratch_shared() is True
