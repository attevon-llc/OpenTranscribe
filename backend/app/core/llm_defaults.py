"""LLM provider defaults: request defaults, reasoning-probe settings, context-window
probe settings and the Bedrock model/region tables.
"""

# LLM service settings
LLM_DEFAULT_MAX_TOKENS = 2000
LLM_DEFAULT_TEMPERATURE = 0.3
LLM_DEFAULT_TIMEOUT = 60

# =============================================================================
# Reasoning ("thinking") as a per-MODEL capability (issue #64)
# =============================================================================
# A provider accepting an "off" parameter is NOT evidence the model honours it.
# Measured against a real vLLM serving gemma-4-e4b, `enable_thinking: false` was
# byte-identical to omitting the key: HTTP 200, and 931 characters of reasoning
# either way. So the off-switch is *probed*, per model, and the UI control only
# exists where the probe proved it works. Everything below is the probe's
# instrument settings; there are deliberately no `.env` vars, and the recorded
# verdict is a measurement rather than an admin-editable setting (see
# `services/llm_reasoning.py`).

#: `SystemSettings` key prefix. The suffix is a fingerprint of
#: (provider, base_url, model) — see `llm_reasoning.capability_key`.
LLM_REASONING_CAPABILITY_KEY_PREFIX = "llm.reasoning.off_switch."

#: What an unprobed model reports. "unknown" must render no control at all:
#: a toggle the user believes turns reasoning off, over a model that reasons
#: anyway, is worse than having no toggle.
DEFAULT_LLM_REASONING_OFF_SWITCH = "unknown"

#: Temperature for every probe arm. Not a "make it deterministic" claim —
#: greedy decoding merely removes sampling as an explanation for a difference
#: between the arms, which is the whole measurement.
LLM_REASONING_PROBE_TEMPERATURE = 0.0

#: Ceiling per arm. Large enough that a truncated thought is not read as a
#: suppressed one (the measured "on" arm spent 1,123 tokens).
LLM_REASONING_PROBE_MAX_TOKENS = 1200

#: Per-arm HTTP timeout. Three arms, so the whole probe is bounded well inside
#: a request's patience; a slow endpoint yields UNKNOWN rather than hanging.
LLM_REASONING_PROBE_TIMEOUT_S = 120

#: The "off" arm must remove at least 90% of the reasoning the control arms
#: produced before the switch is called real. Justification: the control is
#: labelled *off*. A switch that halves the thinking still leaves the user
#: reading a claim that is false, which is the failure this whole capability
#: exists to prevent — so 90% suppression is the weakest threshold under which
#: "off" is an honest word. The measured negative sits at 1.00 of the omitted
#: control and 0.56 of the activated arm, i.e. 5.6x clear of the boundary on
#: the tighter of the two conditions.
LLM_REASONING_PROBE_SUPPRESSION_RATIO = 0.1

#: Below this many characters the "reasoning" is a stray boundary token rather
#: than a chain of thought, and a ratio computed over it is noise. Observed
#: arms were 931-1,656 characters, ~30x above it.
LLM_REASONING_PROBE_MIN_CHARS = 32

#: Elicits multi-step arithmetic in a couple of hundred tokens. Deliberately
#: not a transcript question: the probe must not depend on retrieval, on the
#: user's library, or on anything that could send recorded content to a
#: provider as a side effect of pressing a button.
LLM_REASONING_PROBE_PROMPT = (
    "A meeting had three speakers. Two of them each spoke for 12 minutes and "
    "the third spoke for half as long as one of them. How many minutes were "
    "spoken in total? Show your working."
)

# --- Context-window discovery (issue #533) — the reasoning probe's sibling. ---
# Same design: the RESULT is a measurement in `SystemSettings`, keyed by the
# (provider, base_url, model) fingerprint, written only by the probe. The values
# below are the instrument's settings, not tunables.

#: `SystemSettings` key prefix. Suffix = the same fingerprint scheme as
#: `llm_reasoning.capability_key`, under its own prefix so the two measurements
#: can never shadow each other.
LLM_CONTEXT_WINDOW_KEY_PREFIX = "llm.context_window."

#: One metadata HTTP call (`/v1/models` or `/api/show`) — no generation, so a
#: short timeout: a slow endpoint yields "unreachable", it does not hang the
#: settings page.
LLM_CONTEXT_WINDOW_PROBE_TIMEOUT_S = 10

# =============================================================================
# Amazon Bedrock
# =============================================================================
# Cross-region inference profiles are addressed by a geography-prefixed model ID.
# The prefix is derived from the AWS region's leading segment so an operator only
# has to set a bare model ID; a fully-qualified ID or profile ARN bypasses this.
# `us-gov` is intentionally absent: GovCloud regions split as "us"/"gov"/"west" and
# would otherwise pick up the commercial "us." prefix, so GovCloud deployments must
# set BEDROCK_MODEL_NAME to an explicit `us-gov.`-prefixed ID.
BEDROCK_GEO_PREFIX_BY_REGION = {
    "us": "us.",
    "eu": "eu.",
    "ap": "apac.",
    "ca": "us.",  # Canada routes into the US geography
    "sa": "us.",  # South America likewise
    "me": "eu.",  # Middle East routes into the EU geography
    "af": "eu.",
}

# Amazon Bedrock defaults. A Bedrock LLM configuration carries its own region and (optionally)
# AWS credentials, stored encrypted (services/bedrock_settings.py); a configuration that leaves
# them blank uses the server's region (BEDROCK_REGION / AWS_REGION) and AWS credential chain.
# Newer Claude models are offered only through inference profiles (a "global." or geo prefix);
# their bare on-demand base ids are refused, so the default and the suggestions are profile ids.
DEFAULT_BEDROCK_MODEL_ID = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
BEDROCK_SUGGESTED_MODELS = (
    DEFAULT_BEDROCK_MODEL_ID,
    "global.anthropic.claude-sonnet-4-6",
)
# Suggestions for the region select. Validation is the strict pattern in
# schemas/llm_settings.py, not this list, so a region AWS adds later still works.
BEDROCK_REGIONS = (
    "us-east-1",
    "us-east-2",
    "us-west-2",
    "ca-central-1",
    "sa-east-1",
    "eu-central-1",
    "eu-west-1",
    "eu-west-2",
    "eu-west-3",
    "eu-north-1",
    "eu-south-1",
    "ap-northeast-1",
    "ap-northeast-2",
    "ap-south-1",
    "ap-southeast-1",
    "ap-southeast-2",
    "us-gov-west-1",
    "us-gov-east-1",
)
