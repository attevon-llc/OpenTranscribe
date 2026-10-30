"""Content-redaction model names, category/entity tables and coded defaults of the
DB-backed redaction settings.
"""

# =============================================================================
# Content Redaction Settings
# =============================================================================
# Detection models (pre-downloaded at build/startup; loaded only by the
# celery-redaction worker when PRELOAD_REDACTION_MODELS=true).
REDACTION_PII_GLINER_MODEL = "knowledgator/gliner-pii-base-v1.0"
# Standard-architecture toxicity classifiers (load natively, NO trust_remote_code —
# avoids executing arbitrary remote code). toxic-bert is multi-label
# (toxic/severe_toxic/obscene/threat/insult/identity_hate).
REDACTION_TOXICITY_MODEL_EN = "unitary/toxic-bert"
REDACTION_TOXICITY_MODEL_MULTI = "unitary/multilingual-toxic-xlm-roberta"

# Current detection "model version" — bump to invalidate cached spans on a model
# upgrade (the admin re-index targets files whose redaction_model_version differs).
REDACTION_MODEL_VERSION = "v1"

# Detector identifiers (which detectors a user/admin can toggle).
REDACTION_DETECTORS = ["profanity", "pii", "toxicity", "llm"]

# Language coverage per detector (ISO codes). A detector is SKIPPED for a transcript
# whose language is not listed, and the skip is reported to the user. The LLM detector
# is provider-dependent (no fixed list). GLiNER itself is multilingual but our Presidio
# NLP engine is English-only, so PII is en-only until a multilingual engine is added.
REDACTION_PROFANITY_LANGUAGES = ["en"]
REDACTION_PII_LANGUAGES = ["en"]
REDACTION_TOXICITY_LANGUAGES = ["en", "es", "fr", "it", "pt", "tr", "ru"]

# Categories that can be redacted (a span's `category`).
REDACTION_CATEGORIES = ["profanity", "pii", "toxicity", "custom"]

# PII entity types we surface (Presidio entity names + GLiNER labels mapped to these).
REDACTION_PII_ENTITIES = [
    "NAME",
    "EMAIL",
    "PHONE",
    "SSN",
    "CREDIT_CARD",
    "ADDRESS",
    "BANK_ACCOUNT",
    "IP_ADDRESS",
    "IBAN",
    "LOCATION",
    "ORGANIZATION",
]

# Mask styles.
REDACTION_STYLES = ["label", "asterisks", "first_letter", "blur"]

# Per-user redaction defaults (coded, DB-backed; the device/VRAM/GLiNER ops knobs are
# ``Settings`` fields, not constants). Mirrors DEFAULT_TRANSCRIPTION_* in core/constants.py.
# Redaction is OPT-OUT by default (off): it adds processing time and delays transcript
# display until the scan completes, so users enable it explicitly. Admins can force it.
DEFAULT_REDACTION_ENABLED = False
DEFAULT_REDACTION_DETECTORS = ["profanity", "pii", "toxicity"]  # llm opt-in
# PII is deliberately NOT a default masking category: name masking is aggressive on
# conversational transcripts (every "[NAME]" interrupts reading) and the primary ask
# is profanity/toxicity masking. PII spans are still DETECTED and cached (detectors
# above), so enabling the category later applies instantly at read time.
DEFAULT_REDACTION_CATEGORIES = ["profanity", "toxicity", "custom"]
# ORGANIZATION is INCLUDED, and the reason is measured (issue #499).
#
# It used to be excluded, on the grounds that spaCy over-tags acronyms as ORG and that
# "org names are rarely sensitive PII". The first half is true; the second missed that a
# PERSON'S NAME is sometimes what gets tagged ORG, and excluding the entity meant that
# name shipped in clear to a user who had explicitly asked for PII masking.
#
# MEASURED against the shipped detector (`en_core_web_sm`, the model this app configures
# — NOT the `en_core_web_lg` a bare AnalyzerEngine downloads):
#
#     "Blackwell will follow up"      -> ORGANIZATION @ 0.85   <- a surname. The leak.
#     "Acme Corporation", "Microsoft" -> ORGANIZATION @ 0.85   <- correct, now masked too
#     "SSN", "API", "CPU"             -> ORGANIZATION @ 0.85   <- noise, now masked too
#
# ⚠️ **A confidence threshold cannot separate those.** Presidio's own FAQ recommends
# tuning the acceptance threshold for exactly this problem, and it does not work here:
# `en_core_web_sm` returns a flat **0.85 for every NER hit**, real or noise. That is why
# `DEFAULT_REDACTION_PII_CONFIDENCE` is not the lever, and raising it only loses recall.
#
# The trade is therefore deliberate and one-directional: company names and acronyms get
# masked so that a misfiled person's name does not leak. That is the recall-over-precision
# choice the PII literature recommends (a missed name is a privacy incident; a masked
# company name is noise), and it only ever applies to users who opted into PII masking —
# `pii` is not in DEFAULT_REDACTION_CATEGORIES, so this is opt-in twice over.
#
# ⚠️ **This does NOT make name detection exhaustive, and the UI says so.** The same
# measurement found `en_core_web_sm` missing person names ENTIRELY — "Dax Okonkwo",
# "Rivera" and "Sterling" produced no span at all, not even a mislabelled one. Adding
# ORGANIZATION cannot recover those. The higher-recall path is the GLiNER detector
# (`REDACTION_PII_USE_GLINER`, default off), which is a PII-specific model rather than a
# general-purpose NER.
DEFAULT_REDACTION_PII_ENTITIES = list(REDACTION_PII_ENTITIES)
DEFAULT_REDACTION_STYLE = "label"
DEFAULT_REDACTION_TOXICITY_THRESHOLD = 0.5
DEFAULT_REDACTION_REDACT_BEFORE_LLM = True
DEFAULT_REDACTION_DEFAULT_EXPORT_REDACTED = True

# Substituted for a segment whose masking raised. Never fall back to the original
# text: under redact_before_llm the whole point is that unmaskable content must not
# reach a third-party provider. See services/redaction/llm_guard.py.
REDACTION_LLM_FAILSAFE_TEXT = "[redacted — masking unavailable]"
# How many times an LLM task may defer itself waiting for detection spans before
# failing. 10 × 60s covers a slow CPU scan; past that something is wrong, and a
# loud failure beats an unbounded retry loop.
REDACTION_LLM_MAX_DEFERRALS = 10

# PII detection confidence floor (Presidio/GLiNER scores below this are dropped).
DEFAULT_REDACTION_PII_CONFIDENCE = 0.4

# Redaction lifecycle statuses (MediaFile.redaction_status).
REDACTION_STATUS_PENDING = "pending"
REDACTION_STATUS_PROCESSING = "processing"
REDACTION_STATUS_DONE = "done"
REDACTION_STATUS_FAILED = "failed"
