"""Declarative registry for admin-tunable ``chat.*`` flags.

**Why this exists.** Before this module, adding one ``chat.*`` admin knob needed
SEVEN coordinated hand-edits spread across four files: a ``DEFAULT_CHAT_*``
constant in ``core/constants.py``; a ``SETTING_KEYS`` entry AND a ``DEFAULTS``
entry AND a dataclass field in ``services/chat/settings.py``; a field on BOTH
``ChatAdminSettings`` and ``ChatAdminSettingsUpdate`` in ``schemas/chat.py``;
and a ``_DESCRIPTIONS`` entry in ``api/endpoints/chat/admin_settings.py``. A
missing ``_DESCRIPTIONS`` entry was not caught anywhere — it raised
``KeyError`` inside the PUT handler, i.e. a 500 on save, the first time an
admin tried to change the new flag.

**What this module owns, and what it still does not.** ``CHAT_FLAG_REGISTRY``
is the single declarative table this repo has for the *description* and
*bounds* of each flag. ``api/endpoints/chat/admin_settings.py`` reads its
descriptions from here instead of a second hand-maintained dict, and
``services/chat/settings.SETTING_KEYS``/``DEFAULTS`` are now DERIVED from
this registry (a dict comprehension over ``CHAT_FLAG_REGISTRY``), not a
second hand-written copy — a field added only here is picked up by both
automatically. It still does **not** replace the ``ChatSettings`` dataclass
(also in ``services/chat/settings.py``) or ``schemas/chat.ChatAdminSettings``/
``ChatAdminSettingsUpdate``: a dataclass field and a Pydantic field each need
a real type annotation and a coded default, which cannot be synthesized from
a tuple of specs without dynamic class construction — those three remain
hand-declared, written to match this registry exactly, field for field and
key for key. ``tests/unit/test_chat_flag_registry.py`` is the completeness
test: it fails the moment this registry and those three disagree, on any of
field set, setting key, default, or bounds — which is the drift that used to
surface as a runtime 500 (or, for the dataclass, a ``TypeError`` in
``get_chat_settings()``) instead of a test failure. Adding a flag today is
four edits, down from the original seven: one ``ChatFlagSpec`` here, one
``DEFAULT_CHAT_*`` constant in ``core/constants.py``, one ``ChatSettings``
field, and one field on each of the two schemas in ``schemas/chat.py``.

**None of the 16 registered flags are experimental.** ``experimental=True`` is
plumbing for admin-tunable knobs that require a working LLM provider to have
any effect (a future planner/enrichment toggle is the motivating case) — the
admin UI groups those under a separate "Experimental (measurement-gated)"
subsection and disables them with an explanatory hint when no provider is
configured. No such flag exists yet; the field is here so the first one that
does needs one registry entry, not a new mechanism.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core import constants as C  # noqa: N812


@dataclass(frozen=True)
class ChatFlagSpec:
    """One admin-tunable ``chat.*`` flag, declared once.

    Attributes:
        field: The Python-side field name — matches
            ``services.chat.settings.SETTING_KEYS`` and
            ``schemas.chat.ChatAdminSettingsUpdate`` exactly.
        setting_key: The ``SystemSettings`` row key, e.g. ``"chat.rag.final_chunks"``.
        description: Shown in the admin UI and recorded on the
            ``SystemSettings`` row (``set_setting``'s ``description`` arg).
        value_type: ``int`` | ``bool`` | ``float`` — matches the coded default's
            type, which is also what ``services.chat.settings._coerce`` branches on.
        default: The coded default (sourced from ``core.constants``, never
            duplicated here as a literal).
        ge: Inclusive lower bound, or ``None`` for a bool.
        le: Inclusive upper bound, or ``None`` for a bool.
        experimental: Whether this flag belongs in the admin UI's
            "Experimental (measurement-gated)" subsection, disabled without a
            configured LLM provider. ``False`` for every flag registered today.
    """

    field: str
    setting_key: str
    description: str
    value_type: type
    default: int | bool | float
    ge: int | float | None = None
    le: int | float | None = None
    experimental: bool = False


# Field order matches `services/chat/settings.SETTING_KEYS` and
# `schemas/chat.ChatAdminSettingsUpdate` — kept in sync by
# `tests/unit/test_chat_flag_registry.py`, not by convention alone.
CHAT_FLAG_REGISTRY: tuple[ChatFlagSpec, ...] = (
    ChatFlagSpec(
        field="candidate_pool",
        setting_key="chat.rag.candidate_pool",
        description="Chunks retrieved before reranking",
        value_type=int,
        default=C.DEFAULT_CHAT_RAG_CANDIDATE_POOL,
        ge=1,
        le=500,
    ),
    ChatFlagSpec(
        field="final_chunks",
        setting_key="chat.rag.final_chunks",
        description="Chunks included in the prompt",
        value_type=int,
        default=C.DEFAULT_CHAT_RAG_FINAL_CHUNKS,
        ge=1,
        le=100,
    ),
    ChatFlagSpec(
        field="max_chunks_per_file",
        setting_key="chat.rag.max_chunks_per_file",
        description="Maximum chunks contributed by any one recording",
        value_type=int,
        default=C.DEFAULT_CHAT_RAG_MAX_CHUNKS_PER_FILE,
        ge=1,
        le=50,
    ),
    ChatFlagSpec(
        field="rerank_enabled",
        setting_key="chat.rag.rerank_enabled",
        description="Rerank retrieved chunks with a CPU cross-encoder",
        value_type=bool,
        default=C.DEFAULT_CHAT_RAG_RERANK_ENABLED,
    ),
    ChatFlagSpec(
        field="rerank_max_pairs",
        setting_key="chat.rag.rerank_max_pairs",
        description="Maximum (query, chunk) pairs scored per message",
        value_type=int,
        default=C.DEFAULT_CHAT_RAG_RERANK_MAX_PAIRS,
        ge=1,
        le=500,
    ),
    ChatFlagSpec(
        field="query_rewrite_enabled",
        setting_key="chat.rag.query_rewrite_enabled",
        description="Expand follow-up questions into standalone queries",
        value_type=bool,
        default=C.DEFAULT_CHAT_RAG_QUERY_REWRITE_ENABLED,
    ),
    ChatFlagSpec(
        field="cache_ttl_seconds",
        setting_key="chat.rag.cache_ttl_seconds",
        description="Retrieval cache lifetime (0 disables)",
        value_type=int,
        default=C.DEFAULT_CHAT_RAG_CACHE_TTL_SECONDS,
        ge=0,
        le=86400,
    ),
    ChatFlagSpec(
        field="semantic_cache_enabled",
        setting_key="chat.rag.semantic_cache_enabled",
        description="Reuse results for near-identical questions",
        value_type=bool,
        default=C.DEFAULT_CHAT_RAG_SEMANTIC_CACHE_ENABLED,
    ),
    ChatFlagSpec(
        field="semantic_cache_threshold",
        setting_key="chat.rag.semantic_cache_threshold",
        description="Cosine similarity required for a semantic cache hit",
        value_type=float,
        default=C.DEFAULT_CHAT_RAG_SEMANTIC_CACHE_THRESHOLD,
        ge=0.5,
        le=1.0,
    ),
    ChatFlagSpec(
        field="history_max_turns",
        setting_key="chat.history_max_turns",
        description="Prior exchanges (question + answer) replayed to the model",
        value_type=int,
        default=C.DEFAULT_CHAT_HISTORY_MAX_TURNS,
        ge=1,
        le=50,
    ),
    ChatFlagSpec(
        field="messages_per_hour",
        setting_key="chat.limits.messages_per_hour",
        description="Per-user hourly message ceiling",
        value_type=int,
        default=C.DEFAULT_CHAT_MESSAGES_PER_HOUR,
        ge=1,
        le=10000,
    ),
    ChatFlagSpec(
        field="max_concurrent_streams",
        setting_key="chat.limits.max_concurrent_streams",
        description="Per-user simultaneous streaming replies",
        value_type=int,
        default=C.DEFAULT_CHAT_MAX_CONCURRENT_STREAMS,
        ge=1,
        le=20,
    ),
    ChatFlagSpec(
        field="retention_days",
        setting_key="chat.retention_days",
        description="Delete conversations older than N days (0 keeps forever)",
        value_type=int,
        default=C.DEFAULT_CHAT_RETENTION_DAYS,
        ge=0,
        le=3650,
    ),
    ChatFlagSpec(
        field="speaker_facet_content_scope",
        setting_key="chat.aggregate.speaker_facet_content_scope",
        description="Score the speaker facet by spoken content instead of recording titles",
        value_type=bool,
        default=C.DEFAULT_CHAT_AGGREGATE_SPEAKER_FACET_CONTENT_SCOPE,
    ),
    ChatFlagSpec(
        field="speaker_stats_enabled",
        setting_key="chat.aggregate.speaker_stats_enabled",
        description="Answer 'who talked most' from exact per-speaker talk time",
        value_type=bool,
        default=C.DEFAULT_CHAT_AGGREGATE_SPEAKER_STATS_ENABLED,
    ),
    ChatFlagSpec(
        field="map_tier_summaries",
        setting_key="chat.rag.map_tier_summaries",
        description="Prefer each file's fresh LLM summary over its digest in the collection map",
        value_type=bool,
        default=C.DEFAULT_CHAT_MAP_TIER_SUMMARIES,
    ),
    ChatFlagSpec(
        field="speaker_resolver_enabled",
        setting_key="chat.speaker_resolver_enabled",
        description="Resolve a speaker named in the question text into a parallel retrieval leg",
        value_type=bool,
        default=C.DEFAULT_CHAT_SPEAKER_RESOLVER_ENABLED,
    ),
    ChatFlagSpec(
        field="map_tier_speaker_summaries",
        setting_key="chat.rag.map_tier_speaker_summaries",
        description="Prefer each file's fresh LLM speaker analysis over its digest in the per-speaker map",
        value_type=bool,
        default=C.DEFAULT_CHAT_MAP_TIER_SPEAKER_SUMMARIES,
    ),
    ChatFlagSpec(
        field="recurrence_enabled",
        setting_key="chat.recurrence_enabled",
        description="Detect items recurring across multiple recordings and surface a <recurrence> block",
        value_type=bool,
        default=C.DEFAULT_CHAT_RECURRENCE_ENABLED,
    ),
    ChatFlagSpec(
        field="planner_enabled",
        setting_key="chat.planner_enabled",
        description="Plan multi-part questions into parallel retrieval legs before answering",
        value_type=bool,
        default=C.DEFAULT_CHAT_PLANNER_ENABLED,
    ),
    ChatFlagSpec(
        field="planner_max_parallel_legs",
        setting_key="chat.planner.max_parallel_legs",
        description="Maximum retrieval legs run in parallel for a planned turn",
        value_type=int,
        default=C.DEFAULT_CHAT_PLANNER_MAX_PARALLEL_LEGS,
        ge=1,
        le=8,
    ),
    ChatFlagSpec(
        field="enrichment_enabled",
        setting_key="chat.enrichment_enabled",
        description="Reconcile merged multi-leg evidence into a <synthesis> block before answering",
        value_type=bool,
        default=C.DEFAULT_CHAT_ENRICHMENT_ENABLED,
    ),
)

#: ``field -> ChatFlagSpec``, for a single-lookup consumer.
BY_FIELD: dict[str, ChatFlagSpec] = {spec.field: spec for spec in CHAT_FLAG_REGISTRY}

#: ``field -> description`` — what ``api/endpoints/chat/admin_settings.py``
#: passes to ``set_setting``. The single source now; no second hand-written dict.
DESCRIPTIONS: dict[str, str] = {spec.field: spec.description for spec in CHAT_FLAG_REGISTRY}

#: ``field -> setting_key`` — for parity-checking against
#: ``services.chat.settings.SETTING_KEYS`` (they must be byte-identical).
SETTING_KEYS: dict[str, str] = {spec.field: spec.setting_key for spec in CHAT_FLAG_REGISTRY}

#: Flags belonging in the admin UI's "Experimental (measurement-gated)"
#: subsection. Empty today — see the module docstring.
EXPERIMENTAL_FIELDS: tuple[str, ...] = tuple(
    spec.field for spec in CHAT_FLAG_REGISTRY if spec.experimental
)


def find_missing_pieces(
    *,
    setting_keys: dict[str, str] | None = None,
    schema_fields: set[str] | None = None,
    dataclass_fields: set[str] | None = None,
) -> dict[str, list[str]]:
    """Report registry entries missing a derived piece, keyed by field.

    Used by the completeness test (and safe to call from a startup check): a
    field present in the registry but absent from ``setting_keys``,
    ``schema_fields`` or ``dataclass_fields`` — or present there with no
    registry entry — is exactly the class of drift that used to reach
    production as a missing ``_DESCRIPTIONS`` key and a 500 on save.

    Args:
        setting_keys: ``services.chat.settings.SETTING_KEYS``, or ``None`` to
            skip that comparison.
        schema_fields: ``schemas.chat.ChatAdminSettingsUpdate.model_fields``
            keys, or ``None`` to skip that comparison.
        dataclass_fields: ``{f.name for f in dataclasses.fields(ChatSettings)}``,
            or ``None`` to skip that comparison. ``SETTING_KEYS``/``DEFAULTS``
            in ``services.chat.settings`` are now DERIVED from this registry
            (a field added there is automatic), but the ``ChatSettings``
            dataclass itself still needs an explicit field declaration — a
            real type annotation and a coded default cannot be synthesized
            without dynamic class construction, which this repo does not do
            for a hand-read, hand-typed settings object. This comparison is
            what closes that remaining gap: nothing previously checked the
            dataclass's field set against the registry at all, so a field
            added to one and forgotten in the other surfaced only as a
            runtime ``TypeError`` in ``get_chat_settings()``, the first time
            that code path actually ran. The caller is expected to exclude
            ``max_output_tokens`` first — it is deliberately NOT a registered
            flag (resolved from tenant limits, never admin- or user-set
            directly), so it is not registry-comparable at all.

    Returns:
        ``{field: [problem, ...]}`` for every field with at least one problem.
        Empty when the registry and its callers fully agree.
    """
    registry_fields = set(BY_FIELD)
    problems: dict[str, list[str]] = {}

    def _note(field: str, problem: str) -> None:
        problems.setdefault(field, []).append(problem)

    if setting_keys is not None:
        for field in registry_fields - set(setting_keys):
            _note(field, "missing from SETTING_KEYS")
        for field in set(setting_keys) - registry_fields:
            _note(field, "missing from CHAT_FLAG_REGISTRY (found in SETTING_KEYS)")
        for field in registry_fields & set(setting_keys):
            if setting_keys[field] != BY_FIELD[field].setting_key:
                _note(field, "setting_key mismatch against SETTING_KEYS")

    if schema_fields is not None:
        for field in registry_fields - schema_fields:
            _note(field, "missing from ChatAdminSettingsUpdate")
        for field in schema_fields - registry_fields:
            _note(field, "missing from CHAT_FLAG_REGISTRY (found in ChatAdminSettingsUpdate)")

    if dataclass_fields is not None:
        for field in registry_fields - dataclass_fields:
            _note(field, "missing from the ChatSettings dataclass")
        for field in dataclass_fields - registry_fields:
            _note(field, "missing from CHAT_FLAG_REGISTRY (found on the ChatSettings dataclass)")

    return problems
