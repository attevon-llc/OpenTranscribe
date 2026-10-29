"""Speaker-matching threshold defaults must be the named constants, with unchanged values.

The literals ``0.5`` / ``0.7`` / ``0.75`` were repeated as default arguments across the
speaker services while ``SPEAKER_CONFIDENCE_*`` held the same numbers (K4). The values are a
product contract, so one test pins the number and a second pins that the default is spelled as
the constant, so a retune of the constant cannot leave a stale copy behind.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import textwrap

import pytest

from app.core import constants

# (module, qualified function name, parameter, pinned value, constant it must be spelled as)
_SITES = [
    (
        "app.services.opensearch_service.matching",
        "find_matching_speaker",
        "threshold",
        0.5,
        "SPEAKER_CONFIDENCE_MEDIUM",
    ),
    (
        "app.services.opensearch_service.matching",
        "batch_find_matching_speakers",
        "threshold",
        0.5,
        "SPEAKER_CONFIDENCE_MEDIUM",
    ),
    (
        "app.services.opensearch_service.profiles",
        "msearch_profile_knn_batch",
        "threshold",
        0.5,
        "SPEAKER_CONFIDENCE_MEDIUM",
    ),
    (
        "app.services.opensearch_service.profiles",
        "find_matching_profiles",
        "threshold",
        0.7,
        "SPEAKER_PROFILE_MATCH_THRESHOLD",
    ),
    (
        "app.services.opensearch_service.clusters",
        "find_matching_clusters",
        "threshold",
        0.75,
        "SPEAKER_CONFIDENCE_HIGH",
    ),
    (
        "app.services.profile_embedding_service",
        "ProfileEmbeddingService.calculate_profile_similarity",
        "threshold",
        0.7,
        "SPEAKER_PROFILE_MATCH_THRESHOLD",
    ),
    (
        "app.services.speaker_matching_service",
        "SpeakerMatchingService.find_and_store_speaker_matches",
        "threshold",
        0.5,
        "SPEAKER_CONFIDENCE_MEDIUM",
    ),
    (
        "app.services.smart_speaker_suggestion_service",
        "SmartSpeakerSuggestionService.consolidate_suggestions_batch",
        "confidence_threshold",
        0.5,
        "SPEAKER_CONFIDENCE_MEDIUM",
    ),
]
_IDS = [f"{s[0].rsplit('.', 1)[-1]}.{s[1]}" for s in _SITES]


def _constant(name: str) -> float:
    # The profile threshold lives beside the profile kNN code, the rest in core.constants.
    from app.services.opensearch_service import profiles

    value: float = getattr(constants if hasattr(constants, name) else profiles, name)
    return value


def _resolve(module: str, qualname: str):
    obj = importlib.import_module(module)
    for part in qualname.split("."):
        obj = getattr(obj, part)
    return obj


@pytest.mark.parametrize(("module", "qualname", "param", "value", "_const"), _SITES, ids=_IDS)
def test_default_value_is_pinned(module, qualname, param, value, _const):
    default = inspect.signature(_resolve(module, qualname)).parameters[param].default
    assert default == value


@pytest.mark.parametrize(("module", "qualname", "param", "value", "const"), _SITES, ids=_IDS)
def test_default_is_spelled_as_the_named_constant(module, qualname, param, value, const):
    assert _constant(const) == value
    func = inspect.unwrap(_resolve(module, qualname))
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    fn = tree.body[0]
    assert isinstance(fn, ast.FunctionDef)
    args = fn.args
    positional = args.posonlyargs + args.args
    defaults = dict(
        zip(
            (a.arg for a in positional[len(positional) - len(args.defaults) :]),
            args.defaults,
            strict=True,
        )
    )
    defaults.update(
        {a.arg: d for a, d in zip(args.kwonlyargs, args.kw_defaults, strict=True) if d is not None}
    )
    node = defaults[param]
    assert isinstance(node, ast.Name), f"{param} default is a literal, not a constant"
    assert node.id == const
