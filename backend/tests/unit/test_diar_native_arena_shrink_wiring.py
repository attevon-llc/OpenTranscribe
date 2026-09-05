"""SPEAKRS_ARENA_SHRINK must be a BARE compose entry, never one with a default (#656).

The knob releases the ORT memory arena between jobs — a lower idle VRAM floor at a per-job
throughput cost. It is the #511 4 GB-tier prerequisite and it must default **off**.

The wiring is the whole problem. Compose has no conditional, so the shape used by every
other variable in that service::

    - SPEAKRS_ARENA_SHRINK=${DIAR_NATIVE_ARENA_SHRINK:-0}

would put the key in the merged config on EVERY install. It is not established whether
diar-server parses this by VALUE or merely by PRESENCE (``env::var(...).is_ok()`` is a
common Rust idiom), and if it is presence, ``:-0`` silently ENABLES the knob everywhere —
the exact inverse of the intent. ``SPEAKRS_LAZY_SESSIONS`` has the identical shape but
defaults to ``1``, so it has never exercised the ``0`` branch and proves nothing.

A bare entry removes the question. Measured against real containers before this landed::

    unset in .env and shell   -> key ABSENT from the container  -> off either way
    SPEAKRS_ARENA_SHRINK=1    -> passed through as "1"          -> on

(.env feeds a bare passthrough entry, not only ``${VAR}`` interpolation — verified,
because the operator sets this in ``.env``, not the shell.)

This test exists because the bare form looks like an oversight next to its neighbours and
is the obvious thing for a future reader to "fix".
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
OVERLAY = REPO_ROOT / "docker-compose.diar-native.yml"

pytestmark = pytest.mark.skipif(
    not OVERLAY.exists(), reason="docker-compose.diar-native.yml not in this checkout"
)


def _sidecar_environment() -> list[str]:
    doc = yaml.safe_load(OVERLAY.read_text(encoding="utf-8"))
    env = ((doc.get("services") or {}).get("diar-native") or {}).get("environment")
    assert isinstance(env, list), (
        "diar-native's environment must be a LIST for a bare passthrough entry to be "
        f"expressible at all; got {type(env).__name__}"
    )
    return env


def test_arena_shrink_is_wired_at_all():
    """Guards the other direction: silently dropping the entry re-blocks #511."""
    env = _sidecar_environment()
    assert any(e.split("=")[0].strip() == "SPEAKRS_ARENA_SHRINK" for e in env), (
        "SPEAKRS_ARENA_SHRINK is not passed to the sidecar at all — the #511 4 GB tier "
        "has no way to enable arena shrink"
    )


def test_arena_shrink_has_no_default_and_no_equals():
    """The load-bearing assertion. A default here could enable a knob that must be off."""
    env = _sidecar_environment()
    entries = [e for e in env if e.split("=")[0].strip() == "SPEAKRS_ARENA_SHRINK"]
    assert entries == ["SPEAKRS_ARENA_SHRINK"], (
        f"SPEAKRS_ARENA_SHRINK must be a BARE entry with no '=' and no default, got "
        f"{entries!r}. With a default, compose emits the key on every install; if "
        f"diar-server parses presence rather than value, that ENABLES arena shrink "
        f"everywhere — the inverse of the intended default-off. Bare means the key is "
        f"absent from the container unless the operator sets it."
    )


def test_no_interpolation_default_sneaks_in_via_the_raw_text():
    """Belt and braces: catch the shape even if the YAML parse above ever changes form."""
    # Comment lines are excluded deliberately: the overlay's own comment QUOTES the
    # dangerous form as the thing not to write, and a naive whole-file regex flags that
    # explanation as the defect it warns about. (It did, on the first run of this test.)
    directives = [
        ln
        for ln in OVERLAY.read_text(encoding="utf-8").splitlines()
        if not ln.strip().startswith("#")
    ]
    bad = re.findall(r"SPEAKRS_ARENA_SHRINK\s*=\s*\$\{[^}]*:-[^}]*\}", "\n".join(directives))
    assert not bad, (
        f"found a defaulted SPEAKRS_ARENA_SHRINK interpolation: {bad}. See this module's "
        f"docstring — the default is what makes it dangerous, not the variable."
    )


def test_the_neighbouring_lazy_sessions_entry_still_has_its_default():
    """Control: proves the assertions above are specific, not 'no defaults anywhere'.

    SPEAKRS_LAZY_SESSIONS legitimately carries ``:-1``; if a careless edit stripped every
    default in the block, the tests above would still pass while changing real behaviour.
    """
    env = _sidecar_environment()
    lazy = [e for e in env if e.split("=")[0].strip() == "SPEAKRS_LAZY_SESSIONS"]
    assert len(lazy) == 1 and ":-1}" in lazy[0], (
        f"SPEAKRS_LAZY_SESSIONS should still default to 1, got {lazy!r}"
    )
