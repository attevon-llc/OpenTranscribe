"""The sweep's arm selector: does a `--fusion` flag reach the wire, and get recorded?

Phase 1 (#363 plumbing) proved a fusion strategy can be switched per request.
This file guards the half the sweep adds: that the *harness* selects one, that
both retrieval legs get the same one, and that the results file says which.

Three specific ways a bake-off can produce a clean-looking table of wrong
conclusions, one test each:

1. **The flag is accepted and the default runs.** ``retrieve_chunks`` resolves
   ``fusion=None`` to the configured default, so an arm whose config never
   reaches the call scores exactly like the control — a null delta that reads
   as "normalization does not help on our corpus".
2. **One leg gets the arm and the other does not.** ``retrieve_digests`` is a
   separate call; a routed run with the arm on only the chunk leg is two
   strategies in one number.
3. **The results file cannot name its own arm.** Nine arms differing by one CLI
   flag, and a ``metrics.json`` that records none of them, is a directory of
   numbers nobody can attribute afterwards.

Nothing here touches OpenSearch: the two retrieval functions are substituted at
their import site, which is the only way to observe *what was asked for* rather
than what came back.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from app.services.search.fusion import FusionConfig
from tests.eval.harness.corpora import EvalQuery
from tests.eval.harness.qrels import GoldSpan
from tests.eval.harness.runner import DEFAULT_FINAL_CHUNKS
from tests.eval.harness.runner import DEFAULT_MAX_PER_FILE
from tests.eval.harness.runner import DEFAULT_RERANK_MAX_PAIRS
from tests.eval.harness.runner import DEFAULT_SIZE
from tests.eval.harness.runner import RetrievalConfig
from tests.eval.harness.runner import RouteRecord
from tests.eval.harness.runner import execute

pytestmark = pytest.mark.unit

FILE = "8c1d5b20-0000-0000-0000-000000000000"

#: One arm from each processor family, so a test that passes for RRF and fails
#: for normalization cannot hide behind a shared code path.
NORM_ARM = FusionConfig(
    strategy="normalization",
    normalization_technique="l2",
    combination_technique="harmonic_mean",
)
RRF_60 = FusionConfig(strategy="rrf", rank_constant=60)


class _Hit:
    """The three attributes the runner reads off a retrieval result."""

    def __init__(self, chunk_index: int) -> None:
        self.file_uuid = FILE
        self.chunk_index = chunk_index
        self.score = 1.0


def _query(query_id: str = "q-1", text: str = "Summarise every planning session.") -> EvalQuery:
    return EvalQuery(
        query_id=query_id,
        text=text,
        query_class="summarize",
        corpus="synthetic",
        license_tier="A",
        spans=(GoldSpan(FILE, 1, 2),),
    )


@pytest.fixture
def legs(monkeypatch):
    """Substitute both retrieval legs and record the kwargs each was called with."""
    seen: dict[str, list[dict]] = {"chunks": [], "digests": []}

    def _chunks(_text, **kwargs):
        seen["chunks"].append(kwargs)
        return [_Hit(0), _Hit(1)]

    def _digests(_text, **kwargs):
        seen["digests"].append(kwargs)
        return [_Hit(-1)]

    monkeypatch.setattr("app.services.search.chunk_retrieval.retrieve_chunks", _chunks)
    monkeypatch.setattr("app.services.search.chunk_retrieval.retrieve_digests", _digests)
    return seen


def _run(
    config: RetrievalConfig, retrieval_ms: list[float] | None = None
) -> dict[str, RouteRecord]:
    records: dict[str, RouteRecord] = {}
    execute(
        [_query()],
        user_id=1,
        config=config,
        records=records if config.stage == "route" else None,
        retrieval_ms=retrieval_ms,
    )
    return records


def test_the_arm_reaches_the_chunk_leg_rather_than_being_accepted_and_dropped(legs) -> None:
    _run(RetrievalConfig(workers=1, fusion=NORM_ARM))
    assert legs["chunks"], "the chunk leg was never called"
    assert legs["chunks"][0]["fusion"] is NORM_ARM


def test_the_arm_reaches_the_digest_leg_too(legs) -> None:
    """A routed run with the arm on one leg only is two strategies in one number."""
    _run(RetrievalConfig(stage="route", workers=1, fusion=NORM_ARM))
    assert legs["digests"], "the digest leg was never called — nothing to measure"
    assert legs["digests"][0]["fusion"] is NORM_ARM


def test_naming_no_arm_passes_none_rather_than_a_reconstructed_default(legs) -> None:
    """``None`` must survive to the call site, where the default is resolved once.

    Rebuilding "the default" in the harness would put a second copy of that
    decision in the tree, and a control run would then measure the harness's
    idea of the default rather than the deployment's.
    """
    _run(RetrievalConfig(workers=1))
    assert legs["chunks"][0]["fusion"] is None


def test_the_results_file_records_the_pipeline_id_the_arm_resolves_to() -> None:
    control = RetrievalConfig().fusion_provenance()
    arm = RetrievalConfig(fusion=NORM_ARM).fusion_provenance()

    assert control["pipeline_id"] != arm["pipeline_id"], (
        "two arms sharing a pipeline id means one of them measured the other's pipeline"
    )
    assert arm["strategy"] == "normalization"
    assert arm["selected_explicitly"] is True
    assert control["selected_explicitly"] is False
    # And the whole block is what lands in the results document.
    assert RetrievalConfig(fusion=NORM_ARM).as_dict()["fusion"] == arm


def test_two_arms_of_the_same_family_are_still_told_apart() -> None:
    """``rrf-30`` and ``rrf-60`` differ only in a number the id has to carry."""
    thirty = RetrievalConfig(fusion=FusionConfig(strategy="rrf", rank_constant=30))
    sixty = RetrievalConfig(fusion=RRF_60)
    assert thirty.fusion_provenance()["pipeline_id"] != sixty.fusion_provenance()["pipeline_id"]
    assert sixty.fusion_provenance()["rank_constant"] == 60


def test_an_arm_records_only_the_knobs_its_strategy_actually_uses() -> None:
    """``rank_constant`` is inert under normalization; recording it there lies."""
    rrf = RetrievalConfig(fusion=RRF_60).fusion_provenance()
    norm = RetrievalConfig(fusion=NORM_ARM).fusion_provenance()

    assert "rank_constant" in rrf
    assert "normalization_technique" not in rrf
    assert "rank_constant" not in norm
    assert norm["normalization_technique"] == "l2"
    assert norm["combination_technique"] == "harmonic_mean"
    assert norm["weights"] is None


def test_the_harness_measures_the_shipped_retrieval_budget() -> None:
    """48/12/4 is Stage 5's sweep, and its centre point must be what ships.

    The harness used to default the rerank stage to 20 chunks / 3 per file
    while chat shipped 12 / 4, so every ``--stage rerank`` number described a
    deployment nobody runs.
    """
    from app.core import constants as C  # noqa: N812

    assert DEFAULT_SIZE == C.DEFAULT_CHAT_RAG_CANDIDATE_POOL
    assert DEFAULT_FINAL_CHUNKS == C.DEFAULT_CHAT_RAG_FINAL_CHUNKS
    assert DEFAULT_MAX_PER_FILE == C.DEFAULT_CHAT_RAG_MAX_CHUNKS_PER_FILE
    assert DEFAULT_RERANK_MAX_PAIRS == C.DEFAULT_CHAT_RAG_RERANK_MAX_PAIRS

    config = RetrievalConfig(stage="rerank")
    recorded = config.as_dict()
    assert recorded["final_chunks"] == C.DEFAULT_CHAT_RAG_FINAL_CHUNKS
    assert recorded["max_chunks_per_file"] == C.DEFAULT_CHAT_RAG_MAX_CHUNKS_PER_FILE


def test_every_query_contributes_one_retrieval_timing(legs) -> None:
    """Phase 7 gates each A/B on p95 added latency, so the samples must be there."""
    samples: list[float] = []
    execute(
        [_query("q-1"), _query("q-2"), _query("q-3")],
        user_id=1,
        config=RetrievalConfig(workers=1),
        retrieval_ms=samples,
    )
    assert len(samples) == 3
    assert all(value >= 0.0 for value in samples)


def test_a_run_that_asks_for_no_timings_still_works(legs) -> None:
    """The out-parameter is optional; every existing caller passes nothing."""
    run = execute([_query("q-1")], user_id=1, config=RetrievalConfig(workers=1))
    assert len(legs["chunks"]) == 1
    assert [doc.doc_id for doc in run["q-1"]] == [f"{FILE}_0", f"{FILE}_1"]


# --------------------------------------------------------------------------
# The CLI half: scripts/benchmark_rag.py's flag -> FusionConfig resolution.
# --------------------------------------------------------------------------


def _benchmark_rag():
    """Load the sweep script as a module (it lives outside the package tree)."""
    path = Path(__file__).resolve().parents[3] / "scripts" / "benchmark_rag.py"
    spec = importlib.util.spec_from_file_location("benchmark_rag_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _parse(argv: list[str]):
    module = _benchmark_rag()
    return module, module.build_parser().parse_args(argv)


def test_no_fusion_flag_means_the_deployments_configured_default() -> None:
    module, args = _parse([])
    assert module._build_fusion(args) is None


#: Each fusion flag, alone, and the attribute it must land on. A flag missing
#: from ``FUSION_FLAGS`` reads as "no arm requested" and the run silently
#: measures the configured default — labelled on the command line as something
#: it is not. Asserting the attribute (rather than "a config came back") is what
#: makes the case fail for a flag that is wired to the wrong field.
FLAG_LANDINGS = (
    ("--fusion", "normalization", "strategy", "normalization"),
    ("--rank-constant", "60", "rank_constant", 60),
    ("--normalization-technique", "l2", "normalization_technique", "l2"),
    ("--combination-technique", "harmonic_mean", "combination_technique", "harmonic_mean"),
    ("--combination-weights", "0.7,0.3", "weights", (0.7, 0.3)),
)


@pytest.mark.parametrize(("flag", "value", "attribute", "expected"), FLAG_LANDINGS)
def test_every_fusion_flag_on_its_own_lands_on_its_own_parameter(
    flag: str, value: str, attribute: str, expected: object
) -> None:
    module, _ = _parse([])
    config = module._build_fusion(module.build_parser().parse_args([flag, value]))
    assert config is not None, f"{flag} did not select an arm at all"
    assert getattr(config, attribute) == expected


def test_a_rank_constant_alone_stays_rrf_and_carries_the_number() -> None:
    module, args = _parse(["--rank-constant", "60"])
    config = module._build_fusion(args)
    assert config.strategy == "rrf"
    assert config.rank_constant == 60


def test_normalization_flags_compose_into_one_arm() -> None:
    module, args = _parse(
        [
            "--fusion",
            "normalization",
            "--normalization-technique",
            "z_score",
            "--combination-technique",
            "geometric_mean",
            "--combination-weights",
            "0.3,0.7",
        ]
    )
    config = module._build_fusion(args)
    assert config.slug() == "norm-z_score-geometric_mean-w30_70"


def test_a_configuration_opensearch_would_reject_is_refused_before_any_query() -> None:
    """A pipeline that was never created makes the next search run UNFUSED.

    Weights needing more than two decimals cannot be distinguished in the
    derived pipeline id, so two arms would alias onto one pipeline and one of
    them would measure the other's.
    """
    module, args = _parse(["--combination-weights", "0.705,0.295"])
    with pytest.raises(SystemExit, match="Unusable --fusion configuration"):
        module._build_fusion(args)


def test_the_budget_flags_reach_the_run_config_and_are_recorded() -> None:
    module, args = _parse(
        ["--stage", "rerank", "--final-chunks", "24", "--max-per-file", "2", "--size", "96"]
    )
    config = RetrievalConfig(stage=args.stage, size=args.size, **module._build_budget(args))
    recorded = config.as_dict()
    assert (recorded["candidate_pool"], recorded["final_chunks"]) == (96, 24)
    assert recorded["max_chunks_per_file"] == 2
    # Untouched knobs keep the shipped value rather than becoming None.
    assert recorded["rerank_max_pairs"] == DEFAULT_RERANK_MAX_PAIRS


def test_an_unset_budget_flag_leaves_the_shipped_value_alone() -> None:
    module, args = _parse([])
    assert module._build_budget(args) == {}


def test_the_latency_quantiles_are_values_that_were_actually_observed() -> None:
    """Nearest-rank, not interpolated: an arm's p95 must be a real measurement."""
    module = _benchmark_rag()
    summary = module._latency_summary([float(n) for n in range(1, 101)], workers=4)
    assert summary["p50"] == 50.0
    assert summary["p95"] == 95.0
    assert summary["p99"] == 99.0
    assert summary["max"] == 100.0
    assert summary["samples"] == 100
    assert summary["concurrency"] == 4


def test_no_latency_samples_says_so_rather_than_reporting_zero() -> None:
    """A ``p95: 0`` would read as "this arm is free" — the opposite of no data."""
    module = _benchmark_rag()
    summary = module._latency_summary([], workers=4)
    assert summary == {"samples": 0}
