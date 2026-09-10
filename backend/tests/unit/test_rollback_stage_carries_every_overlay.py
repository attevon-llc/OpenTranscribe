"""The rollback stage must be able to address every service the stack it replaces created.

``get_compose_files()`` can only include an overlay that is present in the staged directory,
and ``docker compose down`` can only stop a service some file in the chain defines. So a
rollback tree missing an overlay cannot tear down the services that overlay declares.

Measured 2026-09-07: ``phase_13_stage_rollback_tree`` copied ``docker-compose.yml`` and
``docker-compose.prod.yml`` by NAME (plus ``gpu.yml`` conditionally).
``docker-compose.diar-native.yml`` was therefore absent, ``down`` left
``opentranscribe-diar-native-1`` running, and::

    ❌ Teardown failed and 1 container(s) remain
    FAIL  B-1: update --rollback exits 0  expected='0' actual='1'

which cascaded into both ``B-4a`` assertions — three failures, one missing file. Exactly the
enumeration trap ``cp_inject_labels_all`` was introduced for, in a second place.

⚠️ The fix has a second half that is easy to miss. Copying **every** overlay is
unconditional, so the GPU overlay must now be actively REMOVED in CPU mode — previously it
was simply never copied. Without that, a ``--cpu`` rehearsal would silently roll back onto a
GPU stack, which is a worse bug than the one being fixed.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
UPGRADE = REPO_ROOT / "scripts" / "release-tests" / "test-upgrade.sh"

pytestmark = pytest.mark.skipif(
    not UPGRADE.is_file() or shutil.which("bash") is None,
    reason="scripts/release-tests/test-upgrade.sh or bash is not present in this checkout",
)


def _stage_rollback_body() -> str:
    text = UPGRADE.read_text(encoding="utf-8")
    match = re.search(r"^phase_13_stage_rollback_tree\(\) \{(?P<body>.*?)^\}", text, re.S | re.M)
    assert match, "phase_13_stage_rollback_tree has moved or been renamed; re-point this guard"
    return match.group("body")


def test_the_rollback_stage_copies_every_compose_overlay():
    body = _stage_rollback_body()
    assert 'for overlay in "$stage_after"/docker-compose*.yml' in body, (
        "the rollback stage names its compose files individually again. Any overlay not in "
        "that list is absent from the rollback tree, so `docker compose down` cannot stop "
        "the services it declares — that is how a surviving diar-native container made "
        "`update --rollback` exit 1 and failed three assertions."
    )


def test_the_prod_overlay_is_still_taken_from_the_repo():
    """The loop must not silently undo the deliberate prod.yml override.

    The rollback rehearses `update-full` (new compose + old images via .env alone), so it
    needs the CURRENT prod file with its ${OT_IMAGE_TAG} indirection — not the after-stack's
    pinned copy. Order matters: the override has to come after the loop.
    """
    body = _stage_rollback_body()
    loop_at = body.index('for overlay in "$stage_after"/docker-compose*.yml')
    override = 'cp "$REPO_ROOT/docker-compose.prod.yml" "$stage_rollback/docker-compose.prod.yml"'
    assert override in body, (
        "the rollback stage no longer takes docker-compose.prod.yml from the repo; the "
        "after-stack's copy has its image tags pinned and would not roll back"
    )
    assert body.index(override) > loop_at, (
        "the prod.yml override now runs BEFORE the copy loop, so the loop overwrites it "
        "with the after-stack's pinned copy — the rollback would not move images at all"
    )


def test_cpu_mode_actively_removes_the_gpu_overlay():
    """The second half of the fix, and the one a reviewer would skip.

    Copying every overlay unconditionally means "not copied" is no longer available as the
    CPU-mode behaviour; it must be an explicit removal.
    """
    body = _stage_rollback_body()
    assert 'rm -f "$stage_rollback/docker-compose.gpu.yml"' in body, (
        "nothing removes the GPU overlay from the rollback stage. Since the copy loop is "
        "unconditional, a --cpu rehearsal would roll back onto a GPU stack."
    )
    guard = re.search(
        r'if \[\[ "\$TEST_USE_GPU" != "true" \]\]; then\s*\n\s*rm -f "\$stage_rollback/docker-compose\.gpu\.yml"',
        body,
    )
    assert guard, (
        "the GPU overlay removal is not gated on TEST_USE_GPU != true — it would either "
        "never fire or strip the overlay from a GPU rehearsal too"
    )


def test_the_diar_native_overlay_is_the_case_this_protects():
    """Prove the premise rather than asserting it.

    If diar-native stops being a separate overlay, this module's story is stale and should
    be re-checked rather than left passing for free.
    """
    overlay = REPO_ROOT / "docker-compose.diar-native.yml"
    if not overlay.is_file():
        pytest.skip("docker-compose.diar-native.yml is not in this checkout")
    assert "diar-native:" in overlay.read_text(encoding="utf-8"), (
        "docker-compose.diar-native.yml no longer declares the diar-native service"
    )


# ------------------------------------------------------------- R-6 must be able to fail


def _phase_15_region() -> str:
    """The restore-verification region, located by R-6 itself rather than by line number."""
    text = UPGRADE.read_text(encoding="utf-8")
    anchor = text.index("R-6: no post-FROM-migration table survives")
    return text[max(0, anchor - 4000) : anchor + 2000]


def test_the_restored_table_list_is_actually_written():
    """R-6 read `snapshots/restored/tables.txt` and NOTHING wrote it.

    `sort` on a missing file prints nothing, so `comm -12` saw an empty set, `leaked` was
    always empty, and R-6 passed unconditionally from the day it was written — in the
    release gate's rollback tail, on an assertion that claims nothing leaked. Observed live
    on 2026-09-07:

        sort: cannot read: .../snapshots/restored/tables.txt: No such file or directory
        PASS  R-6: no post-FROM-migration table survives the restore
    """
    text = UPGRADE.read_text(encoding="utf-8")
    writes = re.findall(
        r'dbs_table_list[^\n]*>\s*"\$TEST_ROOT/snapshots/restored/tables\.txt"', text
    )
    assert len(writes) == 1, (
        "nothing writes snapshots/restored/tables.txt (or it is written more than once), so "
        "R-6 compares against an empty set and cannot fail. Write it exactly once, beside "
        f"the restored fingerprint, as phases 13 and 14 do for before/ and after/. Found: {writes}"
    )
    # The sibling snapshots must still be written too, or R-6's other two operands are the
    # same unfailable empty set.
    for snap in ("before", "after"):
        assert re.search(
            rf'dbs_table_list[^\n]*>\s*"\$TEST_ROOT/snapshots/{snap}/tables\.txt"', text
        ), f"snapshots/{snap}/tables.txt is no longer written; R-6 would silently pass again"


def test_a_missing_snapshot_is_a_refusal_not_an_empty_set():
    region = _phase_15_region()
    assert "missing_snaps" in region, (
        "R-6 does not check that its three table-list snapshots exist and are non-empty. "
        "Without that, a path change silently restores the unfailable version — and it "
        "fails OPEN, reporting a clean rollback while verifying nothing."
    )
    assert 'as_record FAIL "R-6' in region, (
        "an unreadable snapshot must be recorded as a FAILURE (or NOT MEASURED), never "
        "skipped past into a PASS"
    )


def test_r6_reports_vacuity_when_the_upgrade_adds_no_tables():
    """A PASS that is free is not evidence.

    If FROM -> TO introduces no new tables there is nothing for R-6 to detect, and banking
    that as a pass is how a check stops meaning anything without anyone noticing.
    """
    region = _phase_15_region()
    assert 'as_record SKIP "R-6' in region, (
        "R-6 does not distinguish 'nothing leaked' from 'there was nothing to leak'. With "
        "an empty new_tables set the assertion passes for free."
    )
