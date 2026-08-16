"""The QMSum ↔ synthetic agreement analysis must be able to report disagreement.

``scripts/rag_corpus_agreement.py`` answers one question — *do the two evaluation corpora
rank retrieval arms the same way?* — and the answer is the justification for the
both-corpora gate. A statistics helper that silently returns ``0.0`` where it means
"undefined", or that counts an arm which moved nothing as agreement, would report a
comfortable number for a corpus pair that in fact disagrees. That is the same failure mode
as a test that cannot fail, so the arithmetic is pinned here against hand-checkable inputs
rather than against the sweep's own output.

Three properties carry the analysis and each has a test that goes red without it:

* **tau-b reads ±1 on the extremes** — a reversed ranking must come back as −1.0, not as a
  small positive number, or "anti-correlated" is not a claim this tool can make.
* **rank measures survive a monotone transform where Pearson does not** — this is the
  stated reason the report leads with tau-b, so it is asserted rather than argued.
* **inert arms inflate agreement** — several budget arms move nothing on either corpus,
  and two zeros are a concordant pair. ``--exclude-inert`` exists because of that, and the
  size of the effect is measured here.

No network, no index, no ``/tmp/sweep403``: every fixture is written into ``tmp_path``.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "rag_corpus_agreement.py"


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location("rag_corpus_agreement", _SCRIPT)
    assert spec and spec.loader, f"cannot load {_SCRIPT}"
    module = importlib.util.module_from_spec(spec)
    sys.modules["rag_corpus_agreement"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def agreement() -> Any:
    if not _SCRIPT.exists():
        pytest.fail(f"{_SCRIPT} is missing — the corpus-agreement analysis has no implementation")
    return _load_module()


def _arm(
    agreement: Any, qmsum: float, synthetic: float, *, name: str = "a", axis: str = "x"
) -> Any:
    return agreement.ArmDelta(family="f", arm=name, axis=axis, qmsum=qmsum, synthetic=synthetic)


def _write_run(directory: Path, rows: dict[tuple[str, str], dict[str, float]]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "rows": [
            {"corpus": corpus, "query_class": query_class, "metrics": metrics}
            for (corpus, query_class), metrics in rows.items()
        ]
    }
    (directory / "metrics.json").write_text(json.dumps(payload))


# --------------------------------------------------------------------------- verdicts


def test_opposite_signs_are_recorded_as_disagreement(agreement: Any) -> None:
    """An arm that helps QMSum and hurts synthetic is the case the whole page is about."""
    assert _arm(agreement, +0.0015, -0.0687).verdict == "disagree"
    assert _arm(agreement, -0.0093, -0.0082).verdict == "agree"
    assert _arm(agreement, +0.0128, +0.0342).verdict == "agree"


def test_an_arm_that_moved_nothing_is_inert_not_agreement(agreement: Any) -> None:
    """Two zeros are not two corpora concurring; they are a metric that cannot see the knob."""
    assert _arm(agreement, 0.0, 0.0).verdict == "inert"
    assert _arm(agreement, -0.0009, 0.0).verdict == "partial"
    assert _arm(agreement, 0.0, +0.0110).verdict == "partial"


def test_sign_agreement_rate_excludes_arms_that_did_not_move(agreement: Any) -> None:
    deltas = [
        _arm(agreement, +1.0, +1.0, name="agree-1"),
        _arm(agreement, -1.0, -2.0, name="agree-2"),
        _arm(agreement, +1.0, -1.0, name="disagree-1"),
        _arm(agreement, 0.0, 0.0, name="inert-1"),
        _arm(agreement, 0.0, 0.0, name="inert-2"),
        _arm(agreement, 0.0, +1.0, name="one-sided-1"),
    ]
    counts = agreement.sign_agreement(deltas)

    assert (counts.agree, counts.disagree, counts.inert, counts.partial) == (2, 1, 2, 1)
    assert counts.decided == 3
    assert counts.rate == pytest.approx(2 / 3)


def test_sign_agreement_rate_is_none_when_nothing_moved(agreement: Any) -> None:
    """``None``, never ``0.0`` — "no arm moved" and "no arm agreed" are different facts."""
    counts = agreement.sign_agreement([_arm(agreement, 0.0, 0.0)])

    assert counts.decided == 0
    assert counts.rate is None


# ------------------------------------------------------------------- rank correlation


def test_tau_b_reads_minus_one_on_a_perfectly_reversed_ranking(agreement: Any) -> None:
    """Without this, "the corpora rank the arms in opposite order" is unsayable."""
    reversed_ranking = [
        _arm(agreement, 1.0, 4.0, name="a"),
        _arm(agreement, 2.0, 3.0, name="b"),
        _arm(agreement, 3.0, 2.0, name="c"),
        _arm(agreement, 4.0, 1.0, name="d"),
    ]
    same_ranking = [
        _arm(agreement, 1.0, 10.0, name="a"),
        _arm(agreement, 2.0, 20.0, name="b"),
        _arm(agreement, 3.0, 30.0, name="c"),
        _arm(agreement, 4.0, 40.0, name="d"),
    ]

    assert agreement.rank_agreement(reversed_ranking).kendall_tau_b == pytest.approx(-1.0)
    assert agreement.rank_agreement(same_ranking).kendall_tau_b == pytest.approx(1.0)


def test_rank_measures_ignore_a_monotone_transform_that_moves_pearson(agreement: Any) -> None:
    """The stated reason tau-b leads the report, asserted instead of argued.

    Cubing is strictly increasing, so it cannot change any arm's rank — but it does change
    the linear fit. A report led by Pearson would therefore move when nothing about the
    ordering did.
    """
    pairs = [(-3.0, -2.0), (-1.0, 1.0), (2.0, -4.0), (5.0, 6.0), (7.0, 3.0)]
    plain = [_arm(agreement, q, s, name=f"a{i}") for i, (q, s) in enumerate(pairs)]
    cubed = [_arm(agreement, q, s**3, name=f"a{i}") for i, (q, s) in enumerate(pairs)]

    before = agreement.rank_agreement(plain)
    after = agreement.rank_agreement(cubed)

    assert after.kendall_tau_b == pytest.approx(before.kendall_tau_b)
    assert after.spearman_rho == pytest.approx(before.spearman_rho)
    assert abs(after.pearson_r - before.pearson_r) > 0.05


def test_rank_agreement_is_undefined_not_zero_when_a_corpus_is_constant(agreement: Any) -> None:
    """scipy returns ``nan`` here; ``nan`` printed as 0.000 would read as "no agreement"."""
    constant = [
        _arm(agreement, 0.0, 1.0, name="a"),
        _arm(agreement, 0.0, 2.0, name="b"),
        _arm(agreement, 0.0, 3.0, name="c"),
    ]
    result = agreement.rank_agreement(constant)

    assert result.kendall_tau_b is None
    assert result.note == "a corpus is constant"


def test_rank_agreement_refuses_a_sample_too_small_to_rank(agreement: Any) -> None:
    result = agreement.rank_agreement([_arm(agreement, 1.0, 1.0), _arm(agreement, 2.0, 2.0)])

    assert result.n == 2
    assert result.kendall_tau_b is None
    assert result.note == "n < 3"


def test_inert_arms_inflate_tau_which_is_why_they_can_be_excluded(agreement: Any) -> None:
    """The measured budget family, with and without its four arms that moved nothing.

    Real deltas, not invented ones: half that family's arms are exact zeros on both
    corpora (``diversity_sample`` is prefix-invariant in ``cap``), and every zero pairs
    concordantly with a real arm. tau-b reads 0.909 with them and 0.667 without — the
    difference between "significant agreement" and "four arms, p = 0.33".
    """
    moved = [
        _arm(agreement, -0.0013, -0.0028, name="budget-48-08-4"),
        _arm(agreement, +0.0009, +0.0095, name="budget-24-12-4"),
        _arm(agreement, +0.0004, +0.0091, name="budget-96-12-4"),
        _arm(agreement, +0.0007, +0.0057, name="budget-96-12-4-pairs96"),
    ]
    inert = [_arm(agreement, 0.0, 0.0, name=f"inert-{i}") for i in range(4)]

    with_inert = agreement.rank_agreement(moved + inert)
    without_inert = agreement.rank_agreement(moved)

    assert with_inert.kendall_tau_b == pytest.approx(0.9091, abs=5e-5)
    assert without_inert.kendall_tau_b == pytest.approx(0.6667, abs=5e-5)
    assert with_inert.kendall_p < 0.05 < without_inert.kendall_p


# ------------------------------------------------------------------------- deltas / IO


def test_deltas_are_arm_minus_control_and_rounded_to_the_files_precision(
    agreement: Any, tmp_path: Path
) -> None:
    """0.3 − 0.1 is 0.19999999999999998 in IEEE754, and an unrounded tie is not a tie."""
    _write_run(tmp_path / "ctl", {("qmsum", "all"): {"nDCG@10": 0.1}})
    _write_run(tmp_path / "arm", {("qmsum", "all"): {"nDCG@10": 0.3}})

    control = agreement.load_rows(tmp_path / "ctl")
    arm = agreement.load_rows(tmp_path / "arm")

    assert agreement.delta(control, arm, "qmsum", "all", "nDCG@10") == 0.2


def test_delta_names_the_measure_it_could_not_find(agreement: Any, tmp_path: Path) -> None:
    """A typo'd measure must fail loudly; ``.get(name, 0.0)`` here would invent a delta."""
    _write_run(tmp_path / "ctl", {("qmsum", "all"): {"nDCG@10": 0.1}})
    _write_run(tmp_path / "arm", {("qmsum", "all"): {"nDCG@10": 0.3}})
    control = agreement.load_rows(tmp_path / "ctl")
    arm = agreement.load_rows(tmp_path / "arm")

    with pytest.raises(KeyError, match="nDCG@3"):
        agreement.delta(control, arm, "qmsum", "all", "nDCG@3")

    with pytest.raises(KeyError, match="summarize"):
        agreement.delta(control, arm, "qmsum", "summarize", "nDCG@10")


def test_collect_deltas_compares_each_arm_against_its_own_family_control(
    agreement: Any, tmp_path: Path
) -> None:
    """Two families with different controls must not be differenced against each other.

    The real sweep's fusion control sits at 0.0983 QMSum and its rerank control at 0.0748;
    a shared control would manufacture a +0.023 "improvement" out of a stage change.
    """
    _write_run(
        tmp_path / "ctl-a",
        {("qmsum", "all"): {"nDCG@10": 0.0983}, ("synthetic", "all"): {"nDCG@10": 0.2952}},
    )
    _write_run(
        tmp_path / "arm-a",
        {("qmsum", "all"): {"nDCG@10": 0.0998}, ("synthetic", "all"): {"nDCG@10": 0.2265}},
    )
    _write_run(
        tmp_path / "ctl-b",
        {("qmsum", "all"): {"nDCG@10": 0.0748}, ("synthetic", "all"): {"nDCG@10": 0.1605}},
    )
    _write_run(
        tmp_path / "arm-b",
        {("qmsum", "all"): {"nDCG@10": 0.0876}, ("synthetic", "all"): {"nDCG@10": 0.1947}},
    )
    manifest = {
        "families": [
            {
                "name": "fusion",
                "control": {"name": "ctl-a", "run": "ctl-a"},
                "arms": [{"name": "arm-a", "run": "arm-a", "axis": "normalization"}],
            },
            {
                "name": "pool",
                "control": {"name": "ctl-b", "run": "ctl-b"},
                "arms": [{"name": "arm-b", "run": "arm-b", "axis": "candidate_pool"}],
            },
        ]
    }

    deltas = agreement.collect_deltas(manifest, "all", "nDCG@10", tmp_path)

    assert [(d.family, d.arm, d.qmsum, d.synthetic, d.verdict) for d in deltas] == [
        ("fusion", "arm-a", 0.0015, -0.0687, "disagree"),
        ("pool", "arm-b", 0.0128, 0.0342, "agree"),
    ]


def test_collect_deltas_honours_the_drop_list(agreement: Any, tmp_path: Path) -> None:
    _write_run(
        tmp_path / "ctl",
        {("qmsum", "all"): {"nDCG@10": 0.1}, ("synthetic", "all"): {"nDCG@10": 0.2}},
    )
    _write_run(
        tmp_path / "keep",
        {("qmsum", "all"): {"nDCG@10": 0.2}, ("synthetic", "all"): {"nDCG@10": 0.3}},
    )
    _write_run(
        tmp_path / "gone",
        {("qmsum", "all"): {"nDCG@10": 0.3}, ("synthetic", "all"): {"nDCG@10": 0.4}},
    )
    manifest = {
        "families": [
            {
                "name": "f",
                "control": {"name": "ctl", "run": "ctl"},
                "arms": [
                    {"name": "keep", "run": "keep", "axis": "a"},
                    {"name": "gone", "run": "gone", "axis": "a"},
                ],
            }
        ]
    }

    deltas = agreement.collect_deltas(manifest, "all", "nDCG@10", tmp_path, frozenset({"gone"}))

    assert [d.arm for d in deltas] == ["keep"]


def test_missing_run_directory_is_reported_by_path(agreement: Any, tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="absent"):
        agreement.load_rows(tmp_path / "absent")


# ----------------------------------------------------------------------------- winners


def test_each_corpus_picks_its_own_winner(agreement: Any) -> None:
    """The fusion family's real shape: QMSum's best arm is synthetic's worst."""
    deltas = [
        _arm(agreement, +0.0015, -0.0687, name="norm-zscore-arith"),
        _arm(agreement, -0.0003, +0.0063, name="rrf-60"),
        _arm(agreement, -0.0161, -0.2059, name="norm-minmax-harm"),
    ]

    best = agreement.winner(deltas, "fusion")

    assert best.qmsum_arm == "norm-zscore-arith"
    assert best.synthetic_arm == "rrf-60"
    assert best.same is False


def test_the_same_winner_is_reported_as_the_same_winner(agreement: Any) -> None:
    deltas = [
        _arm(agreement, +0.0128, +0.0342, name="pool-12"),
        _arm(agreement, +0.0036, +0.0095, name="pool-24"),
    ]

    best = agreement.winner(deltas, "pool")

    assert (best.qmsum_arm, best.synthetic_arm) == ("pool-12", "pool-12")
    assert best.same is True


def test_winner_of_an_empty_set_is_none(agreement: Any) -> None:
    assert agreement.winner([], "empty") is None


# -------------------------------------------------------------------------- by axis


def test_by_axis_groups_disagreement_by_the_knob_that_moved(agreement: Any) -> None:
    deltas = [
        _arm(agreement, +0.0008, -0.0555, name="minmax", axis="normalization"),
        _arm(agreement, +0.0015, -0.0687, name="zscore", axis="normalization"),
        _arm(agreement, +0.0128, +0.0342, name="pool-12", axis="candidate_pool"),
    ]

    grouped = agreement.by_axis(deltas)

    assert grouped["normalization"].disagree == 2
    assert grouped["normalization"].agree == 0
    assert grouped["candidate_pool"].agree == 1


# ---------------------------------------------------------------------- the built-in manifest


def test_the_builtin_manifest_describes_stage_5s_twenty_four_arms(agreement: Any) -> None:
    """Three controls plus 21 arms. A silently dropped family would shrink the analysis."""
    families = agreement.DEFAULT_MANIFEST["families"]
    arms = [arm for family in families for arm in family["arms"]]

    assert len(families) == 3
    assert len(arms) + len(families) == 24
    assert len({arm["name"] for arm in arms}) == len(arms)


def test_every_manifest_arm_declares_the_axis_it_moves(agreement: Any) -> None:
    """The by-axis summary is only meaningful if no arm is filed under a blank axis."""
    arms = [arm for family in agreement.DEFAULT_MANIFEST["families"] for arm in family["arms"]]
    axis_less = [arm["name"] for arm in arms if not arm.get("axis")]

    assert len(arms) == 21, "21 non-control arms — an empty manifest would pass vacuously"
    assert axis_less == []
