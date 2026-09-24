"""``scripts/build_probe_question_set.py`` — pure question-assembly logic.

Loaded via ``importlib`` the same way ``test_eval_probe_cli.py`` loads
``probe_chat_rag.py`` — it lives outside the ``tests.eval`` package tree, under
``scripts/``, and is never installed as a package. Nothing here touches Postgres,
the filesystem glob loaders, or a live stack: :func:`indexed_meetings` (subprocess
``docker exec``), :func:`load_qmsum`/:func:`load_ami_abstractive` (real NAS globs)
and :func:`main` are never called. Only the pure assembly functions — :func:`build`,
:func:`build_corpus_scale`, :func:`find_series`, :func:`series_reference` — are
exercised, against small in-memory fixtures standing in for what those loaders would
have returned.

This file's whole reason to exist is issue #829: before it, AMI-81 had no test
proving the corpus-scale question-set builder actually widens scope past a single
meeting/series, which is the exact thing that makes it able to catch a
ranking-vs-mapping divergence a single-file eval structurally cannot.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def _module():
    path = Path(__file__).resolve().parents[3] / "scripts" / "build_probe_question_set.py"
    spec = importlib.util.spec_from_file_location("build_probe_question_set_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


bpqs = _module()


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _qmsum_meeting(mid: str, n_specific: int = 1, n_general: int = 1) -> dict:
    return {
        "specific_query_list": [
            {"query": f"{mid} specific question {i}", "answer": f"{mid} specific answer {i}"}
            for i in range(n_specific)
        ],
        "general_query_list": [
            {"query": f"{mid} general question {i}", "answer": f"{mid} general answer {i}"}
            for i in range(n_general)
        ],
    }


# A real AMI four-session series (TS3005) plus two standalone meetings, matching the
# shape build_probe_question_set.py actually consumes.
_SERIES_MEETINGS = ["TS3005a", "TS3005b", "TS3005c", "TS3005d"]
_STANDALONE_MEETINGS = ["IS1006c", "ES2006a"]


def _qmsum_fixture() -> dict[str, dict]:
    return {mid: _qmsum_meeting(mid) for mid in _SERIES_MEETINGS + _STANDALONE_MEETINGS}


def _indexed_fixture() -> dict[str, str]:
    return {mid: f"uuid-{mid}" for mid in _SERIES_MEETINGS + _STANDALONE_MEETINGS}


def _distractor_fixture(n: int = 34) -> dict[str, str]:
    return {f"DIST{i:03d}": f"uuid-dist-{i:03d}" for i in range(n)}


def _ami_fixture() -> dict[str, dict[str, list[str]]]:
    return {
        mid: {
            "decisions": [f"{mid} decision sentence"],
            "actions": [f"{mid} action sentence"],
            "problems": [f"{mid} problem sentence"],
            "abstract": [f"{mid} abstract sentence"],
        }
        for mid in _SERIES_MEETINGS
    }


# ---------------------------------------------------------------------------
# find_series
# ---------------------------------------------------------------------------


def test_find_series_groups_by_series_prefix() -> None:
    series = bpqs.find_series(_SERIES_MEETINGS + _STANDALONE_MEETINGS, min_size=3)
    assert series == {"TS3005": sorted(_SERIES_MEETINGS)}


def test_find_series_excludes_series_below_min_size() -> None:
    series = bpqs.find_series(["ES2004b", "ES2004c"], min_size=3)
    assert series == {}


def test_find_series_ignores_non_series_ids() -> None:
    series = bpqs.find_series(_STANDALONE_MEETINGS, min_size=3)
    assert series == {}


# ---------------------------------------------------------------------------
# build_corpus_scale — the guard when no distractors were injected
# ---------------------------------------------------------------------------


def test_build_corpus_scale_returns_empty_without_distractors() -> None:
    out = bpqs.build_corpus_scale(_qmsum_fixture(), _indexed_fixture(), {}, _ami_fixture(), seed=1)
    assert out == []


# ---------------------------------------------------------------------------
# build_corpus_scale — the actual widening behaviour
# ---------------------------------------------------------------------------


def _build_scale(n_needle: int = 2, n_broad: int = 2) -> list[dict]:
    result: list[dict] = bpqs.build_corpus_scale(
        _qmsum_fixture(),
        _indexed_fixture(),
        _distractor_fixture(),
        _ami_fixture(),
        seed=20260820,
        n_needle=n_needle,
        n_broad=n_broad,
    )
    return result


def test_every_question_is_scoped_to_the_full_corpus_not_just_its_own_meeting() -> None:
    """The whole point of #829: file_uuids must be the FULL corpus union, not the
    1-4 files the question is actually "about" — that is what `build()`'s strata do,
    and is exactly what cannot catch a ranking-vs-mapping divergence."""
    out = _build_scale()
    indexed = _indexed_fixture()
    distractors = _distractor_fixture()
    full_scope = sorted(set(indexed.values()) | set(distractors.values()))

    assert out, "fixture produced no corpus-scale questions to check"
    for row in out:
        assert row["file_uuids"] == full_scope, row["label"]
    # Scope must be strictly larger than any single meeting/series scope `build()`
    # would use (at most 4 files for a series) — otherwise this stratum measures
    # nothing beyond what AMI-81 already measures.
    assert len(full_scope) > 4


def test_multi_file_corpus_scale_reuses_the_series_shapes_and_ami_reference() -> None:
    out = _build_scale()
    multi = [r for r in out if r["category"] == "multi_file_corpus_scale"]
    assert len(multi) == 1  # exactly one series (TS3005) in the fixture
    row = multi[0]
    assert row["label"].startswith("scale-")
    assert "TS3005" in row["scope_desc"]
    assert row["reference"] is not None
    assert row["reference_source"] is not None


def test_needle_questions_use_real_qmsum_text_from_non_series_meetings() -> None:
    out = _build_scale(n_needle=2)
    specific = [r for r in out if r["category"] == "single_specific_corpus_scale"]
    general = [r for r in out if r["category"] == "single_general_corpus_scale"]
    # Only 2 standalone (non-series) meetings exist in the fixture, so at most 2 of
    # each can be sampled regardless of n_needle.
    assert 1 <= len(specific) <= 2
    assert 1 <= len(general) <= 2
    for row in specific + general:
        assert row["reference"] is not None
        assert (
            "question" in row["question"]
            or "specific" in row["question"]
            or ("general" in row["question"])
        )
        # The meeting the question is genuinely about must not be a series meeting —
        # needle questions are drawn from the non-series pool on purpose.
        mid = row["label"].split("-", 2)[2].rsplit("-", 2)[0]
        assert mid in _STANDALONE_MEETINGS


def test_broad_prompts_carry_no_reference_and_are_capped_by_the_prompt_bank() -> None:
    out = _build_scale(n_broad=100)  # ask for far more than the bank holds
    broad = [r for r in out if r["category"] == "corpus_scale_broad"]
    assert len(broad) == len(bpqs.CORPUS_SCALE_BROAD_PROMPTS)
    for row in broad:
        assert row["reference"] is None


def test_negative_control_corpus_scale_expects_refusal_at_full_scope() -> None:
    out = _build_scale()
    neg = [r for r in out if r["category"] == "negative_control_corpus_scale"]
    assert len(neg) == 1
    assert neg[0]["expect_refusal"] is True
    assert neg[0]["reference"] is None


def test_every_label_is_unique() -> None:
    out = _build_scale()
    labels = [row["label"] for row in out]
    assert len(labels) == len(set(labels)), labels


# ---------------------------------------------------------------------------
# build_multi_file_expanded — #532 follow-up (Unit U8)
# ---------------------------------------------------------------------------


def test_expanded_emits_every_grounded_series_shape_pair() -> None:
    """One series (TS3005) x four shapes, every shape grounded in the AMI
    fixture -> exactly 4 entries, deterministic (no seed needed)."""
    out = bpqs.build_multi_file_expanded(_qmsum_fixture(), _indexed_fixture(), _ami_fixture())
    assert len(out) == 4
    assert {row["category"] for row in out} == {"multi_file"}
    kinds = {row["label"].rsplit("-", 1)[-1] for row in out}
    assert kinds == {"decisions", "action_items", "problems", "evolution"}


def test_expanded_labels_are_stable_and_seed_independent() -> None:
    """Same corpus, called twice: identical labels in identical order — no
    `random.Random` involved, unlike every other stratum in this module."""
    out1 = bpqs.build_multi_file_expanded(_qmsum_fixture(), _indexed_fixture(), _ami_fixture())
    out2 = bpqs.build_multi_file_expanded(_qmsum_fixture(), _indexed_fixture(), _ami_fixture())
    assert [r["label"] for r in out1] == [r["label"] for r in out2]
    for row in out1:
        assert row["label"] == f"multi-TS3005-{row['label'].rsplit('-', 1)[-1]}"


def test_expanded_drops_an_ungrounded_series_shape_pair() -> None:
    """A series with no AMI layer for one shape is dropped for THAT shape only,
    never emitted with a null reference — an ungrounded multi_file question
    cannot be scored on content coverage."""
    ami = _ami_fixture()
    del ami["TS3005a"]["problems"]  # series_reference unions across sessions,
    del ami["TS3005b"]["problems"]  # so every session's layer must be removed
    del ami["TS3005c"]["problems"]  # to make the whole series ungrounded for
    del ami["TS3005d"]["problems"]  # this one shape.

    out = bpqs.build_multi_file_expanded(_qmsum_fixture(), _indexed_fixture(), ami)
    kinds = {row["label"].rsplit("-", 1)[-1] for row in out}
    assert "problems" not in kinds
    assert len(out) == 3


def test_expanded_excludes_series_below_min_size() -> None:
    """The two standalone meetings never form a series and contribute nothing."""
    qmsum = {mid: _qmsum_meeting(mid) for mid in _STANDALONE_MEETINGS}
    indexed = {mid: f"uuid-{mid}" for mid in _STANDALONE_MEETINGS}
    out = bpqs.build_multi_file_expanded(qmsum, indexed, {})
    assert out == []


def test_expanded_every_entry_scopes_to_its_own_series_only() -> None:
    """Unlike build_corpus_scale, this stratum stays series-scoped — it exists
    to widen the DECISION SET size (more series x shapes), not the per-question
    scope width."""
    out = bpqs.build_multi_file_expanded(_qmsum_fixture(), _indexed_fixture(), _ami_fixture())
    assert out, "fixture produced no expanded entries to check"
    for row in out:
        assert row["file_uuids"] == sorted(f"uuid-{m}" for m in _SERIES_MEETINGS)


def test_expanded_is_a_superset_of_ami_81s_series_shape_pairs_for_the_same_corpus() -> None:
    """The AMI-81 (per_stratum-sampled) multi_file set must never name a
    (series, shape) pair the expanded set does not also cover — the expanded
    set is meant to be strictly wider, never a different sample."""
    ami81 = bpqs.build(_qmsum_fixture(), _indexed_fixture(), _ami_fixture(), per_stratum=25, seed=1)
    ami81_multi = [row for row in ami81 if row["category"] == "multi_file"]
    # build()'s label shape: "multi-{i:03d}-{series}-{kind}" -> parts[2]=series, parts[3]=kind.
    ami81_pairs = {(row["label"].split("-")[2], row["label"].split("-")[3]) for row in ami81_multi}

    expanded = bpqs.build_multi_file_expanded(_qmsum_fixture(), _indexed_fixture(), _ami_fixture())
    # build_multi_file_expanded's label shape: "multi-{series}-{kind}".
    expanded_pairs = {(row["label"].split("-")[1], row["label"].split("-")[2]) for row in expanded}

    assert ami81_pairs <= expanded_pairs


def test_n_needle_zero_still_produces_multi_file_and_broad_and_negative() -> None:
    out = bpqs.build_corpus_scale(
        _qmsum_fixture(),
        _indexed_fixture(),
        _distractor_fixture(),
        _ami_fixture(),
        seed=1,
        n_needle=0,
        n_broad=0,
    )
    categories = {row["category"] for row in out}
    assert categories == {"multi_file_corpus_scale", "negative_control_corpus_scale"}
