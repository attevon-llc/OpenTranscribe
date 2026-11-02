"""Turn the ``--fusion`` and retrieval-budget flags into a retrieval configuration."""

from __future__ import annotations

import argparse

FUSION_FLAGS = (
    'fusion',
    'rank_constant',
    'normalization_technique',
    'combination_technique',
    'combination_weights',
)


def add_fusion_arguments(parser: argparse.ArgumentParser) -> None:
    """The #363 A/B arm selector.

    Every flag defaults to ``None`` rather than to the shipped value, so
    "measure whatever this deployment is configured for" and "measure RRF at
    k=30" stay distinguishable in the results file. A sweep arm that cannot be
    told apart from the default is how a table of numbers loses its labels.
    """
    group = parser.add_argument_group(
        'fusion (#363)',
        'Hybrid fusion strategy for THIS run. Pass nothing to measure the '
        "deployment's configured default. The pipeline id is derived from these "
        'parameters, so two arms are never aliased onto one pipeline.',
    )
    group.add_argument(
        '--fusion',
        default=None,
        choices=('rrf', 'normalization'),
        help='rrf = score-ranker-processor; normalization = normalization-processor',
    )
    group.add_argument(
        '--rank-constant', type=int, default=None, help='RRF only. #363 asks for 30 and 60.'
    )
    group.add_argument(
        '--normalization-technique',
        default=None,
        choices=('min_max', 'l2', 'z_score'),
        help='normalization only: how each leg is normalised before combining',
    )
    group.add_argument(
        '--combination-technique',
        default=None,
        choices=('arithmetic_mean', 'geometric_mean', 'harmonic_mean'),
        help='normalization only: how the normalised legs are combined',
    )
    group.add_argument(
        '--combination-weights',
        default=None,
        help='normalization only: per-leg weights, e.g. "0.7,0.3" (BM25 leg first). '
        'Encoded into the pipeline id as integer percent; more precision is refused.',
    )


def build_fusion(args):
    """Resolve the fusion flags into a ``FusionConfig``, or None for the default.

    Unspecified parameters inherit from the *configured* default rather than
    from a second set of literals here — one source of truth for what
    "unspecified" means, and it keeps ``--fusion rrf`` alone meaning "RRF as
    this deployment configures it".

    Args:
        args: Parsed CLI namespace.

    Returns:
        The requested config, or None when no fusion flag was passed.

    Raises:
        SystemExit: If the requested combination is one OpenSearch would reject.
            Refused here rather than at the wire, because a pipeline that was
            never created makes the next search run UNFUSED — a plausible
            number, not an error.
    """
    if all(getattr(args, name) is None for name in FUSION_FLAGS):
        return None

    from app.services.search.fusion import FusionConfig, FusionConfigError, parse_weights

    base = FusionConfig.default()
    try:
        return FusionConfig(
            strategy=args.fusion or base.strategy,
            rank_constant=(
                base.rank_constant if args.rank_constant is None else args.rank_constant
            ),
            normalization_technique=(args.normalization_technique or base.normalization_technique),
            combination_technique=(args.combination_technique or base.combination_technique),
            weights=(
                base.weights
                if args.combination_weights is None
                else parse_weights(args.combination_weights)
            ),
        )
    except FusionConfigError as exc:
        raise SystemExit(f'Unusable --fusion configuration: {exc}') from exc


def build_budget(args) -> dict[str, int]:
    """The 48/12/4 sweep's overrides, omitting anything the caller did not set.

    Omitted rather than defaulted here so the harness's own defaults — which
    are pinned to the shipped ``chat.rag.*`` values — remain the single source
    of what "the production budget" is.

    Args:
        args: Parsed CLI namespace.

    Returns:
        Keyword overrides for ``RetrievalConfig``.
    """
    return {
        name: value
        for name, value in (
            ('final_chunks', args.final_chunks),
            ('max_per_file', args.max_per_file),
            ('rerank_max_pairs', args.rerank_max_pairs),
        )
        if value is not None
    }
