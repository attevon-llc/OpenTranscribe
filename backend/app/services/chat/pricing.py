"""Estimate the cost of recorded LLM usage.

Every number here is an **estimate shown to a human**, never an invoice. Rates
change, deployments negotiate their own, and a self-hoster on a local model pays
nothing at all — so the API and UI label these as estimates and fall back to
showing raw token counts whenever a model is unpriced.

Two rules this module exists to enforce:

* **Partner platforms are priced separately from first-party.** Amazon Bedrock is
  operated by AWS with its own rate card, so a Bedrock call must never be priced
  off Anthropic's published first-party rates. Rates are therefore keyed by
  ``(provider, model)``, not by model alone.
* **Cache tokens are not input tokens.** Cache *reads* bill far below the uncached
  input rate and cache *writes* above it. Folding either into the input count
  misprices every cache-enabled deployment, so they are priced on their own lines.

Unknown model → ``None``, not zero. A deployment running a model we have no rate
for should show "not priced", because a confident $0.00 is a worse answer than an
honest blank.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

logger = logging.getLogger(__name__)

#: When the rates below were last checked against the vendors' published pricing.
#: Surfaced through the API so a stale table is visible rather than silently trusted.
RATES_VERIFIED_ON = "2026-08-06"


@dataclass(frozen=True)
class ModelRate:
    """Rates in USD per 1,000,000 tokens."""

    input_per_mtok: Decimal
    output_per_mtok: Decimal
    #: Cache read/write rates. Left None where a vendor does not publish them
    #: separately, in which case cache tokens are priced at the input rate.
    cache_read_per_mtok: Decimal | None = None
    cache_write_per_mtok: Decimal | None = None


def _r(inp: str, out: str, cr: str | None = None, cw: str | None = None) -> ModelRate:
    return ModelRate(
        input_per_mtok=Decimal(inp),
        output_per_mtok=Decimal(out),
        cache_read_per_mtok=Decimal(cr) if cr is not None else None,
        cache_write_per_mtok=Decimal(cw) if cw is not None else None,
    )


#: First-party Anthropic API rates. Cache reads are ~0.1x input and 5-minute cache
#: writes ~1.25x input, per Anthropic's published multipliers.
_ANTHROPIC_RATES: dict[str, ModelRate] = {
    "claude-opus-5": _r("5", "25", "0.5", "6.25"),
    "claude-opus-4-8": _r("5", "25", "0.5", "6.25"),
    "claude-sonnet-5": _r("3", "15", "0.3", "3.75"),
    "claude-sonnet-4-6": _r("3", "15", "0.3", "3.75"),
    "claude-haiku-4-5": _r("1", "5", "0.1", "1.25"),
}

#: Local/self-hosted runtimes cost nothing per token — the operator already paid for
#: the hardware. Recording them as explicitly free is what lets the UI distinguish
#: "free" from "unpriced".
_FREE_PROVIDERS = frozenset({"ollama", "vllm"})

#: Providers whose model IDs are the first-party Anthropic strings.
_ANTHROPIC_PROVIDERS = frozenset({"anthropic", "claude"})


def get_rate(provider: str, model: str) -> ModelRate | None:
    """Look up the rate for a provider/model pair, or None when unpriced."""
    provider = (provider or "").lower()
    model = (model or "").strip()

    if provider in _FREE_PROVIDERS:
        return _r("0", "0")

    if provider in _ANTHROPIC_PROVIDERS:
        return _ANTHROPIC_RATES.get(model)

    if provider == "bedrock":
        # Bedrock is AWS-operated with its own rate card. We deliberately do NOT
        # reuse the first-party table: the numbers differ, and quietly pricing a
        # Bedrock call at Anthropic's rates would produce a confidently wrong
        # figure. Bedrock usage reports tokens only until a rate table is added
        # from AWS's published pricing (which also needs a model-ID normalizer,
        # since Bedrock IDs carry geo/vendor prefixes and a dated version suffix).
        return None

    return None


def estimate_cost_usd(
    *,
    provider: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> Decimal | None:
    """Estimate the USD cost of one exchange, or None when the model is unpriced.

    ``prompt_tokens`` is treated as the *uncached* input count. Providers report
    cache reads and writes separately from it, so they are added on their own
    lines rather than assumed to be included.
    """
    rate = get_rate(provider, model)
    if rate is None:
        return None

    million = Decimal(1_000_000)
    cost = (Decimal(prompt_tokens) * rate.input_per_mtok) / million
    cost += (Decimal(completion_tokens) * rate.output_per_mtok) / million

    read_rate = (
        rate.cache_read_per_mtok if rate.cache_read_per_mtok is not None else rate.input_per_mtok
    )
    write_rate = (
        rate.cache_write_per_mtok if rate.cache_write_per_mtok is not None else rate.input_per_mtok
    )
    cost += (Decimal(cache_read_tokens) * read_rate) / million
    cost += (Decimal(cache_write_tokens) * write_rate) / million

    return cost
