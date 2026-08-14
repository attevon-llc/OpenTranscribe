"""Selectable hybrid fusion strategies (issue #363).

OpenSearch fuses the two legs of a ``hybrid`` query in a **search pipeline**, and a
search pipeline is attached *per request* via the ``search_pipeline`` query
parameter. That is the property this module exists to exploit: two fusion
strategies can be measured side by side against one live index with **no global
state swap and no reindex**, because a pipeline is query-time metadata and the
documents never move.

Two strategies:

* ``rrf`` — the shipped default. ``score-ranker-processor`` with
  ``rank_constant`` (``SEARCH_RRF_RANK_CONSTANT``, 30). Scores are sums of
  ``1/(k + rank)`` over integer ranks, so a single-leg hit scores exactly
  ``1/(k+1)`` and nothing can exceed ``2/(k+1)``.
* ``normalization`` — ``normalization-processor``: normalise each leg's scores
  (``min_max`` / ``l2`` / ``z_score``) then combine them (arithmetic / geometric /
  harmonic mean, optionally weighted). OpenSearch's own BEIR benchmark measured
  this ~3.86% higher nDCG@10 than RRF; whether that holds on transcript retrieval
  is what #363 measures, and this module is only the plumbing.

**The pipeline id is derived from the parameters, never chosen.** Two sweep arms
that differ in any parameter therefore get two ids and can be in flight at once.
The historical id ``transcript-hybrid-search`` is reserved for RRF at the
*configured* rank constant, so the pipeline the live cluster already holds keeps
its name and its existing self-heal, and no configuration change can ever
repoint that name at a non-RRF body.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.core.config import settings

logger = logging.getLogger(__name__)

#: Reciprocal rank fusion — ``score-ranker-processor``.
RRF = "rrf"
#: Score normalisation + weighted combination — ``normalization-processor``.
NORMALIZATION = "normalization"

FUSION_STRATEGIES: tuple[str, ...] = (RRF, NORMALIZATION)

#: Per-leg score normalisation techniques OpenSearch 3.4 accepts.
NORMALIZATION_TECHNIQUES: tuple[str, ...] = ("min_max", "l2", "z_score")
#: Cross-leg combination techniques OpenSearch 3.4 accepts.
COMBINATION_TECHNIQUES: tuple[str, ...] = (
    "arithmetic_mean",
    "geometric_mean",
    "harmonic_mean",
)


class FusionConfigError(ValueError):
    """A fusion configuration OpenSearch would reject, refused before it is sent.

    Validation happens where the config is built rather than where the pipeline
    is created: a bad technique name otherwise surfaces as a pipeline that was
    never written, and the next search silently runs *unfused* — the
    "accepted but not honoured" shape this whole issue is written against.
    """


def _parse_weights(raw: str) -> tuple[float, ...] | None:
    """Parse ``SEARCH_COMBINATION_WEIGHTS`` (``"0.7,0.3"``) into a tuple.

    Args:
        raw: Comma-separated weights, or an empty/blank string for "unweighted".

    Returns:
        The weights, or None when none were configured.

    Raises:
        FusionConfigError: If a component is not a number.
    """
    text = (raw or "").strip()
    if not text:
        return None
    try:
        return tuple(float(part) for part in text.split(","))
    except ValueError as exc:
        raise FusionConfigError(f"SEARCH_COMBINATION_WEIGHTS is not numeric: {raw!r}") from exc


def _weight_slug(weights: tuple[float, ...]) -> str:
    """Encode weights into an id fragment, refusing anything that would collide.

    Weights become integer percentages, so ``0.7`` is ``70``. A weight needing
    more precision than that is **refused** rather than rounded: two configs
    quietly sharing one pipeline id would have one arm of a sweep measuring the
    other arm's pipeline, and produce a plausible number.

    Args:
        weights: Per-leg weights.

    Returns:
        A slug fragment such as ``w70_30``.

    Raises:
        FusionConfigError: If a weight is not expressible to two decimals.
    """
    parts = []
    for weight in weights:
        scaled = weight * 100
        rounded = round(scaled)
        if abs(scaled - rounded) > 1e-9:
            raise FusionConfigError(
                f"weight {weight!r} needs more than two decimals; the pipeline id "
                "cannot distinguish it from a neighbouring value"
            )
        parts.append(str(rounded))
    return "w" + "_".join(parts)


@dataclass(frozen=True)
class FusionConfig:
    """One fully-specified fusion strategy — the unit an A/B arm is expressed in.

    Frozen and value-compared on purpose: it is the cache key for "has this
    pipeline been verified", and it is written verbatim into a results file.
    """

    strategy: str = RRF
    #: RRF only. Lower values weight the top of each leg more heavily.
    rank_constant: int = 30
    #: ``normalization`` only.
    normalization_technique: str = "min_max"
    #: ``normalization`` only.
    combination_technique: str = "arithmetic_mean"
    #: ``normalization`` only. None means equal weights (OpenSearch's default).
    weights: tuple[float, ...] | None = None

    def __post_init__(self) -> None:
        """Reject anything OpenSearch would reject, at construction time.

        Raises:
            FusionConfigError: On an unknown strategy or technique, a
                non-positive ``rank_constant``, or unusable weights.
        """
        if self.strategy not in FUSION_STRATEGIES:
            raise FusionConfigError(
                f"unknown fusion strategy {self.strategy!r}; expected one of {FUSION_STRATEGIES}"
            )
        if self.rank_constant < 1:
            raise FusionConfigError(f"rank_constant must be >= 1, got {self.rank_constant!r}")
        if self.normalization_technique not in NORMALIZATION_TECHNIQUES:
            raise FusionConfigError(
                f"unknown normalization technique {self.normalization_technique!r}; "
                f"expected one of {NORMALIZATION_TECHNIQUES}"
            )
        if self.combination_technique not in COMBINATION_TECHNIQUES:
            raise FusionConfigError(
                f"unknown combination technique {self.combination_technique!r}; "
                f"expected one of {COMBINATION_TECHNIQUES}"
            )
        if self.weights is not None:
            if not self.weights:
                raise FusionConfigError("weights must be non-empty when given")
            if any(weight <= 0 for weight in self.weights):
                raise FusionConfigError(f"weights must all be positive, got {self.weights!r}")
            _weight_slug(self.weights)

    @classmethod
    def default(cls) -> FusionConfig:
        """The strategy a request that names none will use.

        Read from the environment (``SEARCH_FUSION_STRATEGY`` and friends) the
        same way ``SEARCH_RRF_RANK_CONSTANT`` always has been — these are
        measurement knobs, not admin settings, so they are deliberately **not**
        in the DB-backed ``SystemSettings`` plane.

        Returns:
            The configured default.
        """
        return cls(
            strategy=settings.SEARCH_FUSION_STRATEGY,
            rank_constant=settings.SEARCH_RRF_RANK_CONSTANT,
            normalization_technique=settings.SEARCH_NORMALIZATION_TECHNIQUE,
            combination_technique=settings.SEARCH_COMBINATION_TECHNIQUE,
            weights=_parse_weights(settings.SEARCH_COMBINATION_WEIGHTS),
        )

    def slug(self) -> str:
        """The parameter fingerprint that distinguishes this config's pipeline.

        Returns:
            A dash-separated fragment, e.g. ``rrf-60`` or
            ``norm-min_max-arithmetic_mean-w70_30``.
        """
        if self.strategy == RRF:
            return f"rrf-{self.rank_constant}"
        parts = ["norm", self.normalization_technique, self.combination_technique]
        if self.weights is not None:
            parts.append(_weight_slug(self.weights))
        return "-".join(parts)

    def pipeline_body(self) -> dict[str, Any]:
        """The search-pipeline definition OpenSearch is asked to store.

        Returns:
            A ``phase_results_processors`` pipeline body.
        """
        if self.strategy == RRF:
            return {
                "description": "Hybrid BM25 + vector search with RRF",
                "phase_results_processors": [
                    {
                        "score-ranker-processor": {
                            "combination": {
                                "technique": "rrf",
                                "rank_constant": self.rank_constant,
                            }
                        }
                    }
                ],
            }

        combination: dict[str, Any] = {"technique": self.combination_technique}
        if self.weights is not None:
            combination["parameters"] = {"weights": list(self.weights)}
        return {
            "description": (
                f"Hybrid BM25 + vector search with {self.normalization_technique} "
                f"normalization and {self.combination_technique} combination"
            ),
            "phase_results_processors": [
                {
                    "normalization-processor": {
                        "normalization": {"technique": self.normalization_technique},
                        "combination": combination,
                    }
                }
            ],
        }


def resolve_fusion(fusion: FusionConfig | None) -> FusionConfig:
    """Return ``fusion``, or the configured default when a caller named none.

    Args:
        fusion: An explicitly requested strategy, or None.

    Returns:
        The strategy to use for this request.
    """
    return fusion if fusion is not None else FusionConfig.default()


def search_pipeline_id(fusion: FusionConfig | None = None) -> str:
    """The pipeline id for ``fusion`` — derived from its parameters.

    RRF at the *configured* ``SEARCH_RRF_RANK_CONSTANT`` keeps the historical
    ``OPENSEARCH_SEARCH_PIPELINE`` name, so the pipeline the cluster already
    holds is untouched by this change, and no ``SEARCH_FUSION_STRATEGY`` value
    can repoint that name at a normalization body.

    Args:
        fusion: The strategy, or None for the configured default.

    Returns:
        The OpenSearch search-pipeline id.
    """
    cfg = resolve_fusion(fusion)
    base = settings.OPENSEARCH_SEARCH_PIPELINE
    if cfg.strategy == RRF and cfg.rank_constant == settings.SEARCH_RRF_RANK_CONSTANT:
        return base
    return f"{base}-{cfg.slug()}"


def pipeline_matches(existing: dict[str, Any], fusion: FusionConfig) -> bool:
    """Whether a pipeline already stored in the cluster is the one we want.

    OpenSearch echoes a search-pipeline body back verbatim — it injects no
    defaults — so this is an exact structural comparison of the processor
    block rather than a field-by-field allowance. A mismatch means drift, and
    the caller recreates.

    Args:
        existing: The body returned by ``GET /_search/pipeline/{id}``, already
            unwrapped from its single-key envelope.
        fusion: The strategy the pipeline is supposed to implement.

    Returns:
        True when the stored processors equal the wanted ones.
    """
    wanted: list[dict[str, Any]] = fusion.pipeline_body()["phase_results_processors"]
    stored: object = existing.get("phase_results_processors")
    return stored == wanted
