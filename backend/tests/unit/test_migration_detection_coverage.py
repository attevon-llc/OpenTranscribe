"""Every alembic revision needs a detection arm, or a written reason it has none.

Step 4 of the five-step procedure in ``backend/app/db/CLAUDE.md`` — "add a detection guard
for the new version in ``_detect_schema_version()``" — was enforced only by whoever
remembered it. Eight revisions have no arm (64 arms for 72 revisions), and the cost is
specific: ``run_migrations()`` stamps an *untracked* database by schema fingerprint, so a
database whose newest markers are ``v363`` is stamped ``v352`` and then re-runs
``v353``…``v363``. That is survivable only because those revisions are idempotent — which
is why :func:`test_an_exempt_revision_is_safe_to_re_run` checks that claim instead of taking
the exemption on trust.

This is a static test: it reads the chain and the ladder, so it needs no database and cannot
be made to pass by the shape of whatever database happens to be running.
"""

from __future__ import annotations

import ast
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[2]
_MIGRATIONS_PATH = _BACKEND / "app" / "db" / "migrations.py"
_VERSIONS_DIR = _BACKEND / "alembic" / "versions"

#: Any of these makes a statement safe to replay: ``IF NOT EXISTS`` / ``IF EXISTS`` on the
#: object, ``ON CONFLICT`` on a seed INSERT, or an ``information_schema``/``pg_catalog``
#: existence check inside a ``DO $$`` block.
_IDEMPOTENCY_GUARDS = ("IF NOT EXISTS", "IF EXISTS", "ON CONFLICT", "DO $$")

#: Revisions with no arm in ``_detect_schema_version()``, and why that is accepted.
#:
#: Each reason names the revision an untracked database at that point is stamped as
#: instead. None of these is unprobeable — every one adds a table or a column that could
#: key an arm — so an entry here is an accepted debt, not a design. **A new revision does
#: not belong in this list**: write the arm (the ladder's newest-first order means it goes
#: at the TOP) and pair it with a consistency test, as v374-v386 all do.
_EXEMPT: dict[str, str] = {
    "v150_add_file_retention_settings": (
        "Seeds system_settings keys only — no schema marker at all, and the keys are "
        "indistinguishable from ones an admin edited. An untracked DB stamps v140 and "
        "re-runs the seed, which is ON CONFLICT DO NOTHING."
    ),
    "v280_add_upload_sessions": (
        "upload_session was DROPPED again by v385 as an orphan table, so an arm keyed on "
        "it would have been correct for five revisions and wrong afterwards. An untracked "
        "DB stamps v270_add_asr_provider_support and re-runs the guarded CREATE."
    ),
    "v353_fix_segment_unique_index": (
        "Replaces uq_transcript_segment_content with an md5() functional index; both "
        "spellings carry the same name, so the probe would have to inspect indexdef "
        "rather than existence. An untracked DB stamps v352 and re-runs the swap."
    ),
    "v355_add_diarization_settings": (
        "No arm; an untracked DB stamps v352 (v353 has none either) and re-runs the "
        "guarded CREATE TABLE for user_diarization_settings."
    ),
    "v360_add_file_pipeline_timing": (
        "Timing instrumentation, read by nothing in the request path — a mis-stamp costs a "
        "re-run of one guarded CREATE TABLE, not a missing column under live code."
    ),
    "v361_add_media_file_imohash": (
        "media_file.imohash is added with ADD COLUMN IF NOT EXISTS; a DB stamped v352 "
        "re-runs it as a no-op."
    ),
    "v362_add_pipeline_timing_markers": (
        "Adds nullable BIGINT marker columns to file_pipeline_timing, each guarded. Same "
        "instrumentation-only blast radius as v360."
    ),
    "v363_add_asr_access_key_id": (
        "user_asr_settings.access_key_id, ADD COLUMN IF NOT EXISTS. This is the newest "
        "arm-less revision, so it defines the whole re-run span: v353…v363."
    ),
}


def _chain_revisions() -> list[str]:
    """Revision ids from the alembic chain itself, oldest first."""
    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    # alembic.ini's script_location is cwd-relative; pin it for the test runner.
    config.set_main_option("script_location", str(_BACKEND / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    return [rev.revision for rev in reversed(list(scripts.walk_revisions()))]


def _arm_revisions() -> set[str]:
    """Every revision id ``_detect_schema_version()`` can return.

    Read by AST rather than by calling the function: the return value depends on a live
    schema, and what this module is about is the *set of arms that exist*.
    """
    tree = ast.parse(_MIGRATIONS_PATH.read_text())
    fn = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_detect_schema_version"
    )
    return {
        node.value.value
        for node in ast.walk(fn)
        if isinstance(node, ast.Return)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }


def _upgrade_source(revision: str) -> str:
    """The text of one revision's ``upgrade()`` — not the whole file.

    The whole file would pass the guard check on the strength of ``downgrade()``'s
    ``DROP … IF EXISTS``, which says nothing about whether the upgrade can re-run.
    """
    source = (_VERSIONS_DIR / f"{revision}.py").read_text()
    tree = ast.parse(source)
    fn = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "upgrade"
    )
    return ast.get_source_segment(source, fn) or ""


def test_every_revision_has_a_detection_arm_or_an_exemption():
    """A revision with no arm and no exemption mis-stamps every untracked database."""
    missing = [
        rev for rev in _chain_revisions() if rev not in _arm_revisions() and rev not in _EXEMPT
    ]
    assert not missing, (
        "These revisions have no arm in _detect_schema_version() and no exemption. An "
        "untracked database carrying their markers will be stamped at an EARLIER revision "
        "(step 4 of backend/app/db/CLAUDE.md):\n  " + "\n  ".join(missing)
    )


def test_every_arm_names_a_revision_that_exists():
    """An arm returning a stale id stamps a database at a revision alembic cannot find.

    This is the guard for a renumbering — the auth chain was renumbered v375-v381 to
    v377-v383 — where a missed arm would leave ``command.stamp(config, <gone>)`` to fail at
    startup, i.e. the backend refusing to boot.
    """
    unknown = sorted(_arm_revisions() - set(_chain_revisions()))
    assert not unknown, f"_detect_schema_version returns ids with no revision file: {unknown}"


def test_the_exemption_list_is_honest():
    """A stale exemption reads as deliberate while exempting nothing.

    Two ways to go stale: the revision now has an arm (the exemption is obsolete and should
    be deleted so the next reader is not told a lie), or the id no longer exists.
    """
    chain, arms = set(_chain_revisions()), _arm_revisions()
    now_covered = sorted(rev for rev in _EXEMPT if rev in arms)
    unknown = sorted(rev for rev in _EXEMPT if rev not in chain)
    unexplained = sorted(rev for rev, reason in _EXEMPT.items() if not reason.strip())

    assert not now_covered, f"these revisions have an arm now — drop the exemption: {now_covered}"
    assert not unknown, f"exemptions for revisions that do not exist: {unknown}"
    assert not unexplained, f"exemptions need a written reason: {unexplained}"


def test_an_exempt_revision_is_safe_to_re_run():
    """The claim every exemption rests on: the skipped span re-runs harmlessly.

    A mis-stamped database re-runs exactly the arm-less revisions above the stamp, so if
    one of them were non-idempotent the missing arm would not be a debt — it would be a
    backend that fails to start (a migration error is ``SystemExit(1)``).
    """
    unguarded = [
        rev
        for rev in _EXEMPT
        if not any(guard in _upgrade_source(rev) for guard in _IDEMPOTENCY_GUARDS)
    ]
    assert not unguarded, (
        "These revisions have no detection arm AND no idempotency guard in upgrade(), so a "
        f"mis-stamped database re-runs them and the backend refuses to start: {unguarded}"
    )


def test_the_arm_extraction_finds_the_ladder():
    """Guard the guard: an extraction that matched nothing would exempt everything.

    Every test above compares against ``_arm_revisions()``. If a refactor renamed
    ``_detect_schema_version`` or moved the returns behind a lookup table, the set would
    quietly go empty and ``test_every_revision_has_a_detection_arm_or_an_exemption`` would
    report only the 8 exemptions' worth of silence — a clean run for a dead check.
    """
    arms = _arm_revisions()
    chain = _chain_revisions()

    assert chain[-1] in arms, "the head revision must have an arm — the newest one always does"
    assert "v010_baseline" in arms, "the ladder's fallback return must be found"
    assert len(arms) > len(chain) // 2, f"only {len(arms)} arms found for {len(chain)} revisions"
