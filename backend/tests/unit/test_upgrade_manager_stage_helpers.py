"""Staging ``common.sh`` alone produces a manager that fails at run time.

``scripts/common.sh`` resolves its helpers relative to **its own directory**::

    voiceprint_helper_path() {
      here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
      echo "$here/voiceprint-backup.py"
    }

``test-upgrade.sh``'s ``_stage_manager_at`` copied ``opentranscribe.sh`` and
``scripts/common.sh`` into a staging directory and nothing else, so the staged manager had
a ``common.sh`` whose one runtime dependency was absent.

Observed 2026-09-07: the upgrade hop reached phase 06b, ran ``./opentranscribe.sh backup``,
and aborted with

    ❌ Voiceprint export failed — this backup is INCOMPLETE.

Phases 07-18 never ran. **And the reason was invisible**: the helper-missing message was
echoed to *stdout*, which in ``export`` mode IS the artifact
(``_voiceprint_run ... > "$out_file"``), and the failure path then ``rm -f``'d it. Diagnosing
it meant reading the staging function, not the logs. Both halves are fixed — the message goes
to stderr, and the helper is staged.

⚠️ This test exists because the *enumeration* is the fragile part, not the copy. A second
sibling helper added to ``common.sh`` would reproduce this exactly, and would again only show
up as an unexplained failure several phases into a long rehearsal.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
COMMON_SH = REPO_ROOT / "scripts" / "common.sh"
UPGRADE = REPO_ROOT / "scripts" / "release-tests" / "test-upgrade.sh"

# `$here/<file>` — how common.sh names a sibling it needs at run time.
_SIBLING = re.compile(r'"?\$here/([A-Za-z0-9._-]+)"?')


def _siblings_common_sh_needs() -> set[str]:
    return set(_SIBLING.findall(COMMON_SH.read_text(encoding="utf-8")))


@pytest.mark.skipif(not COMMON_SH.is_file(), reason="scripts/common.sh not in this checkout")
def test_the_sibling_helper_set_is_not_empty():
    """Non-vacuity: an empty set would make the real check below pass for free."""
    siblings = _siblings_common_sh_needs()
    assert siblings, (
        "no `$here/<file>` references found in scripts/common.sh. Either the helper "
        "resolution changed shape (re-point this scanner) or the coupling is gone and "
        "this module can be deleted — do not leave it passing over nothing."
    )
    assert "voiceprint-backup.py" in siblings, (
        f"voiceprint-backup.py is no longer resolved via $here (found: {sorted(siblings)}); "
        "this module's premise needs re-checking"
    )


@pytest.mark.skipif(
    not COMMON_SH.is_file() or not UPGRADE.is_file(),
    reason="scripts/common.sh or test-upgrade.sh not in this checkout",
)
def test_every_helper_common_sh_needs_is_staged_beside_it():
    staging = UPGRADE.read_text(encoding="utf-8")
    missing = [name for name in sorted(_siblings_common_sh_needs()) if name not in staging]
    assert not missing, (
        "test-upgrade.sh stages scripts/common.sh but never copies these helpers it "
        f"resolves at run time: {missing}. The staged manager will fail mid-rehearsal — "
        "this is exactly how `./opentranscribe.sh backup` aborted the upgrade hop at "
        "phase 06b, taking phases 07-18 with it."
    )


@pytest.mark.skipif(not COMMON_SH.is_file(), reason="scripts/common.sh not in this checkout")
def test_the_helper_missing_message_goes_to_stderr_not_the_artifact():
    """In export mode stdout IS the backup artifact, and it is deleted on failure.

    A diagnostic written there is not merely misplaced — it is destroyed, which is what
    made this cost a full rehearsal cycle to find.
    """
    text = COMMON_SH.read_text(encoding="utf-8")
    block = re.search(r'if \[ ! -f "\$helper" \]; then(?P<body>.*?)\n    return 1', text, re.S)
    assert block, "the helper-missing branch in _voiceprint_run has moved; re-point this guard"

    offenders = [
        line.strip()
        for line in block.group("body").splitlines()
        if line.strip().startswith("echo ") and ">&2" not in line
    ]
    assert not offenders, (
        "these diagnostics go to stdout, which os_export_speaker_indices redirects into "
        "the artifact file and then rm -f's on failure — so the operator is told the "
        "export failed and never told why:\n  " + "\n  ".join(offenders)
    )
