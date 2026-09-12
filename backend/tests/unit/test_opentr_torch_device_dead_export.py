"""Issue #884: `opentr.sh` used to `export TORCH_DEVICE=...` and print it in the startup
banner (`echo "  Device: $TORCH_DEVICE"`) as if it configured the stack. It never did — no
compose file interpolates `${TORCH_DEVICE}`, `opentr.sh` never writes it into `.env`, and it
never becomes a `--build-arg`. Its only two readers were the two banner `echo` lines. An
operator on Apple Silicon read "Device: mps" in the console and reasonably concluded the
containers were using Metal; they were not, because the value never crossed the container
boundary from that path.

The corrected finding (this issue's body carries a self-correction) is narrower than the
original sweep claimed: `setup-opentranscribe.sh`'s `TORCH_DEVICE` sed-into-`.env` writes
(`cuda`/`mps`/`cpu`) are LIVE — `docker-compose.yml` loads `.env` wholesale via `env_file`, so
that path genuinely reaches the container — and `hardware_detection.py` / `diarizer.py` really
do branch on `mps`. Only `opentr.sh`'s own export/echo mechanism was dead, and it was dead on
every branch (`auto`/`cuda`/`cpu`/`mps`), not just the `mps` one originally cited, since none of
them ever reached a compose file either.

The fix (option 1 of the two the issue offered) deletes the dead export and the two display
lines entirely, rather than option 2 (having `opentr.sh` write the value into `.env` the way
`setup-opentranscribe.sh` does) — `opentr.sh` has no other `.env`-write behavior anywhere, and
this repo's own convention is that `.env` is never overwritten without confirmation. Adding a
first `.env` mutation to `opentr.sh` purely to make a cosmetic banner line accurate was judged
not worth that risk.

This is a static test: it parses the scripts and compose files, it does not execute them.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
OPENTR = REPO_ROOT / "opentr.sh"
SETUP_SCRIPT = REPO_ROOT / "setup-opentranscribe.sh"

pytestmark = pytest.mark.skipif(
    not OPENTR.exists(), reason="opentr.sh not present in this checkout"
)

#: Every `*.yml`/`*.yaml` compose file directly under the repo root (base + all overlays).
#: Matches the "all 29 compose files" sweep this issue's body performed.
_COMPOSE_FILES = tuple(
    p for p in sorted(REPO_ROOT.glob("*.yml")) if p.name.startswith("docker-compose")
)

_TORCH_DEVICE_INTERPOLATION_RE = re.compile(r"\$\{?TORCH_DEVICE")


def test_no_compose_file_interpolates_torch_device() -> None:
    """Re-verifies the issue's zero-matches premise, rather than trusting the filed number.

    If this ever fires, `TORCH_DEVICE` has become a real container-facing setting and the
    banner in `opentr.sh` should come back (or move to option 2 — writing it into `.env`).
    """
    assert _COMPOSE_FILES, "expected to find docker-compose*.yml files at the repo root"

    offenders = []
    for compose_file in _COMPOSE_FILES:
        text = compose_file.read_text(encoding="utf-8")
        if _TORCH_DEVICE_INTERPOLATION_RE.search(text):
            offenders.append(compose_file.name)

    assert not offenders, (
        f"TORCH_DEVICE is now interpolated by {offenders} — it is no longer dead, so "
        "opentr.sh should display/export it again (or write it into .env, per option 2)"
    )


def test_opentr_no_longer_exports_or_displays_torch_device() -> None:
    """Pins the fix: `opentr.sh` must not mention `TORCH_DEVICE` at all.

    Before the fix, `opentr.sh` exported `TORCH_DEVICE` on every hardware-detection branch
    and echoed it in two startup banners, despite nothing downstream ever reading it. This
    would fail on the pre-fix script (multiple `export TORCH_DEVICE=...` and two
    `echo "  Device: $TORCH_DEVICE"` lines) and passes once the dead mechanism is removed.
    """
    text = OPENTR.read_text(encoding="utf-8")
    assert "TORCH_DEVICE" not in text, (
        "opentr.sh still references TORCH_DEVICE, but no compose file interpolates it and "
        "opentr.sh never writes it into .env — either finish removing the dead display, or "
        "switch to option 2 (write it into .env like setup-opentranscribe.sh does) instead "
        "of leaving a half-removed mechanism"
    )


@pytest.mark.skipif(
    not SETUP_SCRIPT.exists(), reason="setup-opentranscribe.sh not present in this checkout"
)
def test_setup_opentranscribe_torch_device_env_write_is_untouched() -> None:
    """The issue is explicit that `setup-opentranscribe.sh`'s `TORCH_DEVICE` writes are LIVE
    (docker-compose.yml's `env_file: .env` genuinely delivers them to the container) and must
    not be removed as collateral damage from cleaning up opentr.sh's dead copy.
    """
    text = SETUP_SCRIPT.read_text(encoding="utf-8")
    for value in ("cuda", "mps", "cpu"):
        assert re.search(rf"TORCH_DEVICE={value}", text), (
            f"setup-opentranscribe.sh lost its TORCH_DEVICE={value} .env write"
        )
    assert ".env" in text
