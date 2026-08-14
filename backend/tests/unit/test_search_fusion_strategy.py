"""Fusion strategies are selectable per request, and the ids cannot collide (#363).

#363 measures RRF against OpenSearch's ``normalization-processor`` on *our* corpus.
The measurement is only trustworthy if two things hold, and both are properties of
the plumbing rather than of the numbers:

* **each arm has its own pipeline id**, so two arms can be in flight against one
  live index with no global state swap and no reindex; and
* **no configuration can repoint the historical ``transcript-hybrid-search`` id at
  a non-RRF body**, because that pipeline already exists on every deployment and
  an arm that silently measured a *different* pipeline than it named would produce
  a plausible number rather than an error.

The second is the class of failure this repo keeps hitting — #437 found vLLM
returning HTTP 200 while ignoring a parameter, #64 found ``enable_thinking: false``
byte-identical to omitting it. Accepting a setting and honouring it are different
claims, so the id derivation is asserted directly.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.core.config import settings
from app.services.search.fusion import NORMALIZATION
from app.services.search.fusion import RRF
from app.services.search.fusion import FusionConfig
from app.services.search.fusion import FusionConfigError
from app.services.search.fusion import pipeline_matches
from app.services.search.fusion import resolve_fusion
from app.services.search.fusion import search_pipeline_id

pytestmark = pytest.mark.unit

_HISTORICAL_ID = "transcript-hybrid-search"


def _processors(cfg: FusionConfig) -> Any:
    return cfg.pipeline_body()["phase_results_processors"]


class TestPipelineIdDerivation:
    def test_rrf_at_the_configured_rank_constant_keeps_the_historical_id(self):
        cfg = FusionConfig(strategy=RRF, rank_constant=settings.SEARCH_RRF_RANK_CONSTANT)
        assert search_pipeline_id(cfg) == _HISTORICAL_ID
        assert settings.OPENSEARCH_SEARCH_PIPELINE == _HISTORICAL_ID

    def test_a_normalization_arm_gets_its_own_id(self):
        norm = search_pipeline_id(FusionConfig(strategy=NORMALIZATION))
        assert norm != _HISTORICAL_ID
        assert norm.startswith(f"{_HISTORICAL_ID}-norm-")

    def test_a_different_rank_constant_gets_its_own_id(self):
        sixty = search_pipeline_id(FusionConfig(strategy=RRF, rank_constant=60))
        assert sixty == f"{_HISTORICAL_ID}-rrf-60"
        assert sixty != search_pipeline_id(
            FusionConfig(strategy=RRF, rank_constant=settings.SEARCH_RRF_RANK_CONSTANT)
        )

    def test_every_normalization_parameter_moves_the_id(self):
        """A sweep arm that shared an id with another would measure its pipeline."""
        base = FusionConfig(strategy=NORMALIZATION)
        variants = [
            FusionConfig(strategy=NORMALIZATION, normalization_technique="l2"),
            FusionConfig(strategy=NORMALIZATION, normalization_technique="z_score"),
            FusionConfig(strategy=NORMALIZATION, combination_technique="geometric_mean"),
            FusionConfig(strategy=NORMALIZATION, combination_technique="harmonic_mean"),
            FusionConfig(strategy=NORMALIZATION, weights=(0.7, 0.3)),
            FusionConfig(strategy=NORMALIZATION, weights=(0.3, 0.7)),
        ]
        ids = [search_pipeline_id(base), *(search_pipeline_id(v) for v in variants)]
        assert len(set(ids)) == len(ids), ids

    def test_no_configuration_can_repoint_the_historical_id_at_a_non_rrf_body(self, monkeypatch):
        """The default *strategy* moves; the historical *name* does not.

        Without this, ``SEARCH_FUSION_STRATEGY=normalization`` would give the
        normalization body the id every deployment's RRF pipeline already holds,
        and ``ensure_search_pipeline_exists`` would overwrite it in place.
        """
        monkeypatch.setattr(settings, "SEARCH_FUSION_STRATEGY", NORMALIZATION)

        assert resolve_fusion(None).strategy == NORMALIZATION
        assert search_pipeline_id(None) != _HISTORICAL_ID
        assert search_pipeline_id(FusionConfig(strategy=RRF)) == _HISTORICAL_ID


class TestPipelineBody:
    def test_rrf_builds_a_score_ranker_processor(self):
        procs = _processors(FusionConfig(strategy=RRF, rank_constant=42))
        assert procs == [
            {"score-ranker-processor": {"combination": {"technique": "rrf", "rank_constant": 42}}}
        ]

    def test_normalization_builds_a_normalization_processor(self):
        procs = _processors(
            FusionConfig(
                strategy=NORMALIZATION,
                normalization_technique="l2",
                combination_technique="harmonic_mean",
            )
        )
        assert procs == [
            {
                "normalization-processor": {
                    "normalization": {"technique": "l2"},
                    "combination": {"technique": "harmonic_mean"},
                }
            }
        ]

    def test_weights_reach_the_pipeline_body(self):
        procs = _processors(FusionConfig(strategy=NORMALIZATION, weights=(0.7, 0.3)))
        combination = procs[0]["normalization-processor"]["combination"]
        assert combination["parameters"] == {"weights": [0.7, 0.3]}

    def test_unweighted_omits_the_parameters_block_entirely(self):
        """Absent, not ``[0.5, 0.5]`` — an explicit weight is a claim about legs."""
        procs = _processors(FusionConfig(strategy=NORMALIZATION))
        assert "parameters" not in procs[0]["normalization-processor"]["combination"]


class TestValidation:
    @pytest.mark.parametrize(
        "kwargs",
        [
            {"strategy": "reciprocal"},
            {"strategy": NORMALIZATION, "normalization_technique": "minmax"},
            {"strategy": NORMALIZATION, "combination_technique": "mean"},
            {"rank_constant": 0},
            {"strategy": NORMALIZATION, "weights": ()},
            {"strategy": NORMALIZATION, "weights": (0.7, 0.0)},
        ],
    )
    def test_a_configuration_opensearch_would_reject_is_refused_here(self, kwargs):
        with pytest.raises(FusionConfigError):
            FusionConfig(**kwargs)

    def test_weights_needing_more_than_two_decimals_are_refused(self):
        """Refused rather than rounded: rounding aliases two arms onto one id."""
        with pytest.raises(FusionConfigError, match="two decimals"):
            FusionConfig(strategy=NORMALIZATION, weights=(0.666, 0.334))


class TestDriftDetection:
    def test_an_identical_stored_body_matches(self):
        cfg = FusionConfig(strategy=NORMALIZATION)
        assert pipeline_matches(cfg.pipeline_body(), cfg) is True

    def test_a_changed_technique_is_drift(self):
        stored = FusionConfig(strategy=NORMALIZATION, normalization_technique="l2")
        wanted = FusionConfig(strategy=NORMALIZATION, normalization_technique="min_max")
        assert pipeline_matches(stored.pipeline_body(), wanted) is False

    def test_a_changed_rank_constant_is_drift(self):
        stored = FusionConfig(strategy=RRF, rank_constant=60)
        wanted = FusionConfig(strategy=RRF, rank_constant=30)
        assert pipeline_matches(stored.pipeline_body(), wanted) is False

    def test_a_body_from_the_other_strategy_is_drift(self):
        stored = FusionConfig(strategy=RRF)
        wanted = FusionConfig(strategy=NORMALIZATION)
        assert pipeline_matches(stored.pipeline_body(), wanted) is False

    def test_an_empty_response_is_drift_rather_than_agreement(self):
        assert pipeline_matches({}, FusionConfig(strategy=RRF)) is False


class TestDefaultFromEnvironment:
    def test_every_knob_is_read_from_settings(self, monkeypatch):
        monkeypatch.setattr(settings, "SEARCH_FUSION_STRATEGY", NORMALIZATION)
        monkeypatch.setattr(settings, "SEARCH_NORMALIZATION_TECHNIQUE", "z_score")
        monkeypatch.setattr(settings, "SEARCH_COMBINATION_TECHNIQUE", "geometric_mean")
        monkeypatch.setattr(settings, "SEARCH_COMBINATION_WEIGHTS", "0.7,0.3")

        cfg = FusionConfig.default()

        assert cfg.strategy == NORMALIZATION
        assert cfg.normalization_technique == "z_score"
        assert cfg.combination_technique == "geometric_mean"
        assert cfg.weights == (0.7, 0.3)

    def test_blank_weights_mean_unweighted_not_zero(self, monkeypatch):
        monkeypatch.setattr(settings, "SEARCH_COMBINATION_WEIGHTS", "  ")
        assert FusionConfig.default().weights is None

    def test_non_numeric_weights_are_refused(self, monkeypatch):
        monkeypatch.setattr(settings, "SEARCH_COMBINATION_WEIGHTS", "0.7,half")
        with pytest.raises(FusionConfigError):
            FusionConfig.default()

    def test_the_shipped_default_is_still_rrf(self):
        """Plumbing only — #363 has not adopted anything yet."""
        assert settings.SEARCH_FUSION_STRATEGY == RRF
        assert FusionConfig.default().strategy == RRF
