"""Every transcript ordering must be a TOTAL order (issue #433).

``ORDER BY TranscriptSegment.start_time`` is not a total order. Overlapping speech and
interpolated backchannels routinely share an onset — measured on the QMSum eval corpus,
**3,072 tie groups covering 6,152 segments**. Postgres may return tied rows in any order, and
in practice returns physical storage order, which a delete-then-bulk-insert reshuffles.

How it surfaced: re-indexing **one unchanged corpus** three times produced three different
results (119,950 / 119,949 / 120,540 chunks; nDCG@10 0.1052 / 0.1023 / 0.1029) with
byte-identical input rows. Tied segments swap, speaker-turn grouping changes, chunk
boundaries move. A real pair from that corpus::

    471.983  471.993  "Uh - huh ."
    471.983  473.233  "I mean , if you did it at th..."

Whether the 10 ms backchannel sorts before or after the 1.25 s utterance it overlaps decides
whether that turn is split.

Several consequences are user-visible, not just internal: SRT/VTT cue order flips between
exports of the same file, the transcript the UI renders can reorder between loads, diffs
report spurious changes, and the summariser is shown a different transcript each run.

``(start_time, end_time, id)``, with ``id`` last because ``start_time`` **and** ``end_time``
can both tie — identical spans with different text, which ``uq_transcript_segment_content``
explicitly permits since it includes ``md5(text)``.

An AST check rather than 23 hand-edits, because the next person to write a transcript query
will copy whichever nearby example they find.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_APP = Path(__file__).resolve().parents[2] / "app"

#: The model whose ordering must be total.
_MODEL = "TranscriptSegment"

#: The column that makes any ordering on this table total. It is the primary key, so it
#: breaks every remaining tie by definition.
_TIEBREAKER = "id"

#: ``file::function`` entries that may order by something non-total, each with a written
#: reason. Empty: every site found by #433 was fixed rather than waived. Kept because a
#: genuinely order-insensitive aggregate could appear later — but it must say why, and a
#: stale entry fails :func:`test_the_allowlist_has_no_stale_entries`.
_ALLOWLIST: dict[str, str] = {}


def _order_by_calls() -> list[tuple[Path, int, str]]:
    """Every ``.order_by(...)`` in app code that mentions the transcript model.

    Returns ``(path, lineno, unparsed-arguments)``. Matching on the model name inside the
    call arguments — rather than trying to resolve the queried entity — keeps this honest
    about what it can see: a query built through a helper that hides the model is out of
    reach, and that limit is stated in the module docstring rather than pretended away.
    """
    found: list[tuple[Path, int, str]] = []
    for path in sorted(_APP.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(errors="ignore"))
        except SyntaxError:  # pragma: no cover - app code must parse
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "order_by"
                and node.args
            ):
                rendered = ", ".join(ast.unparse(a) for a in node.args)
                if _MODEL in rendered:
                    found.append((path, node.lineno, rendered))
    return found


def test_the_scan_finds_the_orderings_it_claims_to():
    """Guard the guard: a scan that finds nothing would pass every assertion below.

    23 sites were fixed for #433; if this ever drops to zero the walker is broken, not the
    codebase suddenly free of transcript queries.
    """
    calls = _order_by_calls()
    assert len(calls) >= 20, (
        f"only {len(calls)} transcript orderings found — #433 touched 23, so the AST walker "
        "is no longer matching them"
    )


def test_every_transcript_ordering_ends_in_the_primary_key():
    offenders = [
        f"{path.relative_to(_APP.parent)}:{lineno}  order_by({rendered})"
        for path, lineno, rendered in _order_by_calls()
        if f"{_MODEL}.{_TIEBREAKER}" not in rendered
        and f"{path.relative_to(_APP.parent)}" not in _ALLOWLIST
    ]
    assert not offenders, (
        "these orderings are not total, so tied rows come back in physical storage order and "
        "a delete-then-bulk-insert silently reshuffles them (issue #433). Add "
        f"{_MODEL}.{_TIEBREAKER} last:\n  " + "\n  ".join(offenders)
    )


def test_the_tiebreaker_comes_last():
    """``id`` must be the LAST key, not merely present.

    ``order_by(id, start_time)`` contains the tiebreaker and destroys the ordering: the
    transcript comes back in insertion order. Presence alone is not the property.
    """
    offenders = []
    for path, lineno, rendered in _order_by_calls():
        keys = [k.strip() for k in rendered.split(",") if k.strip()]
        if not keys or f"{_MODEL}.{_TIEBREAKER}" not in rendered:
            continue
        if keys[-1] != f"{_MODEL}.{_TIEBREAKER}":
            offenders.append(f"{path.relative_to(_APP.parent)}:{lineno}  order_by({rendered})")
    assert not offenders, (
        f"{_MODEL}.{_TIEBREAKER} must be the final key — anywhere else it is the primary sort "
        "and the transcript comes back in insertion order:\n  " + "\n  ".join(offenders)
    )


def test_end_time_is_also_a_key_where_start_time_is():
    """``start_time`` alone still ties often; ``end_time`` resolves most of it before the PK.

    Not merely stylistic: ordering by ``(start_time, id)`` is total but arbitrary among
    overlapping segments, so the transcript is *stable* yet not in reading order. The corpus
    pair in the module docstring is exactly that case.
    """
    offenders = [
        f"{path.relative_to(_APP.parent)}:{lineno}  order_by({rendered})"
        for path, lineno, rendered in _order_by_calls()
        if f"{_MODEL}.start_time" in rendered and f"{_MODEL}.end_time" not in rendered
    ]
    assert not offenders, (
        "ordering by start_time then the primary key is stable but not in reading order — "
        "add end_time between them:\n  " + "\n  ".join(offenders)
    )


def test_the_allowlist_has_no_stale_entries():
    """An entry for a file with no transcript ordering left is a lie about the codebase."""
    files_with_orderings = {str(path.relative_to(_APP.parent)) for path, _, _ in _order_by_calls()}
    stale = sorted(set(_ALLOWLIST) - files_with_orderings)
    assert not stale, f"these allowlist entries no longer match any ordering: {stale}"


@pytest.mark.parametrize(
    ("rendered", "is_total"),
    [
        ("TranscriptSegment.start_time", False),
        ("TranscriptSegment.start_time, TranscriptSegment.end_time", False),
        ("TranscriptSegment.start_time, TranscriptSegment.end_time, TranscriptSegment.id", True),
        ("TranscriptSegment.id, TranscriptSegment.start_time", False),
    ],
)
def test_the_predicate_itself_is_right(rendered, is_total):
    """The rules above, exercised directly on synthetic argument lists.

    Without this the detector's own logic is only ever tested against a codebase that
    currently passes — so a rule that accidentally accepts everything would look clean.
    """
    keys = [k.strip() for k in rendered.split(",") if k.strip()]
    total = keys[-1] == f"{_MODEL}.{_TIEBREAKER}"
    assert total is is_total


# ---------------------------------------------------------------------------
# alembic's autogenerate must actually see the models (issue #431 / #403 handoff).
#
# `alembic/env.py` set `target_metadata = Base.metadata` and never imported `app.models`.
# `Base.metadata` is populated as a SIDE EFFECT of importing the model modules, so it was
# empty: measured, 0 tables from `app.db.base` alone versus 54 after the import. Autogenerate
# therefore compared the live database against nothing and reported no model-side differences
# whatever — which is how 24 database constraints came to exist with no ORM declaration while
# a tool whose entire purpose is detecting that stayed silent.
#
# Same class as every other finding on this branch: a check that runs, produces output, and
# cannot fail. The one-line import is the fix; this is the test that keeps it.
# ---------------------------------------------------------------------------


def test_alembic_env_imports_the_models():
    """The import is a side effect, so an "unused import" cleanup silently breaks the tool.

    Asserted on the source text rather than by running alembic: the failure mode is the import
    being *removed*, and a linter or a well-meaning tidy-up is the likely cause. `# noqa` on
    the line is not enough on its own — nothing else states why it must stay.
    """
    env_source = (Path(__file__).resolve().parents[2] / "alembic" / "env.py").read_text()
    assert "import app.models" in env_source, (
        "alembic/env.py must import app.models — Base.metadata is populated by importing the "
        "model modules, and without it target_metadata is EMPTY and --autogenerate reports no "
        "model differences at all (it saw 0 tables instead of 54)"
    )


def test_base_metadata_is_empty_without_the_model_import():
    """The premise, proven rather than asserted in prose.

    If importing `app.db.base` ever registered the tables by itself, the test above would be
    guarding nothing — and this test is what would say so.
    """
    import subprocess
    import sys

    backend = Path(__file__).resolve().parents[2]
    probe = (
        f"import sys; sys.path.insert(0, {str(backend)!r});"
        "from app.db.base import Base;"
        "print(len(Base.metadata.tables));"
        "import app.models;"
        "print(len(Base.metadata.tables))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd=str(backend),
        timeout=180,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    before, after = (int(x) for x in result.stdout.split())
    assert before == 0, (
        f"app.db.base now registers {before} tables by itself — the env.py import is no longer "
        "load-bearing and test_alembic_env_imports_the_models is guarding nothing"
    )
    assert after > 40, f"importing app.models registered only {after} tables"
