"""What a Claude model id will accept on the Messages API (and Bedrock's Converse).

The single source of truth for model-dependent request shape: assistant prefill,
sampling parameters and structured outputs. Anthropic removes these per model
generation, and the failure is a hard 400 on the whole call, so the table lives in
one function instead of ``"opus-4-7" in model`` checks scattered over payload builders.

Documentation this encodes (Claude API docs, platform.claude.com, checked 2026-11):

* Prompting best practices, "Migrating away from prefilled responses": a prefilled last
  assistant turn returns 400 "starting with Claude 4.6 models" (Opus 4.6+, Sonnet 4.6+);
  Sonnet 5.5 and Haiku 5.5 migration guides, "Replace assistant prefill", confirm it for
  5.5 and say Sonnet 4.5 / Haiku 4.5 and older still accept one.
* Opus 5.5 migration guide, "What every request ... must satisfy" and the 4.7 breaking
  changes: ``temperature`` / ``top_p`` / ``top_k`` are rejected on Opus 4.7 and later.
  Sonnet 5.5 guide ("Claude Sonnet 4.6 or earlier: remove temperature, top_p, top_k") and
  Haiku 5.5 guide ("Remove sampling parameters") extend that to Sonnet 5+ and Haiku 5+;
  Sonnet 4.6 and Haiku 4.5 still accept them.
* Structured outputs, "Supported models": Opus / Sonnet / Haiku 4.5 and later (plus the
  Fable and Mythos families) take ``output_config.format``; Opus 4.1 and older do not.

An id that cannot be parsed as a known Claude generation gets the SAFE answer (no
prefill, no sampling, no structured outputs) because the cost of a wrong "yes" is a
400, while the cost of a wrong "no" is a slightly different prompt. An id with no
``claude`` in it at all (an Anthropic-compatible gateway serving some other model) keeps
the pre-existing permissive behaviour: nothing is known to reject anything.
"""

import re
from dataclasses import dataclass

_NEW_STYLE = re.compile(
    r"claude-(?P<family>opus|sonnet|haiku|fable|mythos)-(?P<major>\d+)(?:-(?P<minor>\d{1,2})(?!\d))?"
)
_OLD_STYLE = re.compile(
    r"claude-(?P<major>\d+)(?:-(?P<minor>\d))?-(?P<family>opus|sonnet|haiku)(?![a-z])"
)

# First version of each family that REJECTS sampling parameters.
_SAMPLING_REMOVED_AT: dict[str, tuple[int, int]] = {
    "opus": (4, 7),
    "sonnet": (5, 0),
    "haiku": (5, 0),
}
# First version that rejects a prefilled final assistant turn.
_PREFILL_REMOVED_AT = (4, 6)
# First version of the families that accept ``output_config.format``.
_STRUCTURED_OUTPUTS_FROM = (4, 5)


@dataclass(frozen=True)
class ClaudeCapabilities:
    """Request features a model id accepts.

    Attributes:
        accepts_prefill: A final assistant turn is continued rather than rejected.
        accepts_sampling_params: ``temperature`` / ``top_p`` / ``top_k`` are accepted.
        supports_structured_outputs: ``output_config.format`` (JSON schema) is accepted.
    """

    accepts_prefill: bool
    accepts_sampling_params: bool
    supports_structured_outputs: bool


_PERMISSIVE = ClaudeCapabilities(True, True, False)
_SAFE = ClaudeCapabilities(False, False, False)


def claude_capabilities(model_id: str) -> ClaudeCapabilities:
    """Return what ``model_id`` accepts.

    Handles first-party ids (``claude-sonnet-4-6``, ``claude-opus-4-20250514``), the
    older ``claude-3-5-sonnet`` ordering, and Bedrock ids / inference-profile ARNs, which
    wrap the same name (``us.anthropic.claude-sonnet-5-v1:0``).

    Args:
        model_id: Model identifier as configured.

    Returns:
        The capabilities; the safe set for an unrecognised ``claude`` id, the permissive
        set for an id that does not mention Claude at all.
    """
    name = (model_id or "").lower()
    if "claude" not in name:
        return _PERMISSIVE

    match = _NEW_STYLE.search(name) or _OLD_STYLE.search(name)
    if match is None:
        return _SAFE

    family = match["family"]
    version = (int(match["major"]), int(match["minor"] or 0))

    if family in ("fable", "mythos"):
        return ClaudeCapabilities(False, False, True)

    sampling_removed_at = _SAMPLING_REMOVED_AT[family]
    return ClaudeCapabilities(
        accepts_prefill=version < _PREFILL_REMOVED_AT,
        accepts_sampling_params=version < sampling_removed_at,
        supports_structured_outputs=version >= _STRUCTURED_OUTPUTS_FROM,
    )
