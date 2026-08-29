"""Language scope for RAG chat: what the retrieval stack can actually answer over.

**Multilingual is the goal, and this module measures the distance to it** — it does
not enforce English. WhisperX already transcribes 100+ languages; what this
describes is whether the *question-answering* path on top can do anything useful
with the result, on **this deployment's current configuration**.

⚠️ **This used to be a hardcoded ``frozenset({"en"})`` and that was wrong.** It made
a deployment that had deliberately selected a multilingual embedding model — and
was therefore genuinely serving Spanish — display "Spanish is unsupported" on every
turn. A constant cannot know what the operator configured, so support is now
**derived** from the model that actually produces the vectors.

Which stage is English-only, and what changes it:

* **Embeddings — the one that matters, and it is configurable.** The default
  ``all-MiniLM-L6-v2`` is declared ``"languages": ["en"]``. Measured on this repo's
  own harness (``scripts/verify-embedding-models.py``), cosine of a translation
  against its English original: **0.10 (es) / 0.01 (zh)** for the default versus
  **0.98 / 0.95** for ``paraphrase-multilingual-MiniLM-L12-v2`` — which is also
  384d, so adopting it is a re-embed and NOT an index recreation. Selecting it is
  what makes a deployment multilingual, and :func:`supported_rag_languages` reads
  that selection.
* **BM25** — the ``transcript_chunks`` analyzer is ``english_stop`` +
  ``english_snowball`` (``services/search/indexing_service.py:55``). Partially
  mitigated already: ``content.exact`` (standard analyzer) is queried alongside the
  stemmed leg, so non-English keyword matching degrades rather than vanishing.
* **Reranking** — ``ms-marco-MiniLM-L-6-v2`` is English MS MARCO. It is **skipped
  for non-English content** rather than left to reorder text it cannot read.
* **Prompting — FIXED (#453).** Both prompts are authored in English but now
  instruct in it: answer in the question's language, quote in the original and
  never translate a quotation, and never translate the rewritten query.
* **BM25 field preset — the MECHANISM is built, adoption is not (#506/ML1).**
  ``search.chunk_retrieval`` has a ``"no-stem"`` ``text_fields`` preset that drops
  the stemmed ``content`` leg (``english_snowball`` mis-stems non-English tokens)
  and queries only the unstemmed ``content.exact`` leg instead.
  :func:`resolve_text_field_preset_for_locale` below is the mapping from a
  per-turn locale hint to that preset. Nothing calls it yet — a caller that
  reaches a per-turn locale (the router/planner layer) opts in explicitly by
  passing its result as ``retrieve_chunks(..., text_fields=resolve_text_field_preset(...))``.
  Adopting it automatically is a **measured** decision (see
  ``docs-site/docs/developer-guide/rag-evaluation.md``), not a default flip —
  this module only supplies the mapping.

⚠️ **A multilingual embedding model does not make BM25 multilingual**, so the
warning is not simply switched off — it reports what is *actually* degraded rather
than claiming either total support or none.
"""

from __future__ import annotations

import logging
import uuid as uuid_pkg
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy.orm import Session

# Re-imported, NOT redefined (issue #545). This module used to carry its own copy, which
# disagreed with `redaction/config.py`'s on 13 of 21 inputs. The name stays here because
# `_classify`, `resolve_text_field_preset_for_locale` and `tests/test_chat_language_scope.py`
# reach it through this module; what must not exist twice is the implementation.
from app.utils.language import normalize_language

logger = logging.getLogger(__name__)

#: Fallback support set, used when the active embedding model is English-only or
#: cannot be resolved. **Never widen this by hand** — widening it here would assert
#: support for a language on a deployment still running English-only embeddings.
#: Multilingual support comes from selecting a multilingual model; see
#: :func:`supported_rag_languages`.
SUPPORTED_RAG_LANGUAGES: frozenset[str] = frozenset({"en"})

#: Sentinel returned by :func:`supported_rag_languages` when the active embedding
#: model is multilingual: the set is open, so no language is reported unsupported.
#: A concrete list is deliberately NOT hardcoded — the published cards disagree with
#: themselves (HF tags ``distiluse-base-multilingual-cased-v1`` "14 languages" while
#: sbert's docs say "15" and enumerate 13), and a table transcribed from a source
#: that cannot count is exactly the kind of unverifiable claim that rots.
ALL_LANGUAGES: None = None


def supported_rag_languages(db: Session | None = None) -> frozenset[str] | None:
    """Languages this deployment's retrieval stack can serve.

    Derived from the **active embedding model**, because that is the stage the
    operator can actually change and the one whose failure is silent.

    Args:
        db: Session for reading the model selection. ``None`` skips the lookup and
            returns the English fallback.

    Returns:
        :data:`ALL_LANGUAGES` (``None``) when the active model is multilingual —
        meaning "do not report any language as unsupported" — otherwise
        :data:`SUPPORTED_RAG_LANGUAGES`.

        Fails **closed to English** on any error: an unreadable setting must not
        silence a warning that exists to make a silent failure visible.
    """
    if db is None:
        return SUPPORTED_RAG_LANGUAGES
    try:
        from app.core.constants import OPENSEARCH_EMBEDDING_MODELS
        from app.services.search.settings_service import get_search_embedding_model

        info = OPENSEARCH_EMBEDDING_MODELS.get(get_search_embedding_model()) or {}
        if info.get("language_type") == "multilingual":
            return ALL_LANGUAGES
    except Exception:  # noqa: BLE001 — a diagnostic must never fail a chat turn
        logger.exception("Could not resolve the active embedding model; assuming English-only")
    return SUPPORTED_RAG_LANGUAGES


#: SSE ``warning`` frame code and the ``msg_metadata`` flag, mirroring
#: ``context_dropped``: one code, set live on the frame and persisted on the row,
#: so the notice survives a reload instead of existing only for the stream.
WARNING_CODE = "unsupported_language"

#: ``msg_metadata`` key carrying the per-turn language diagnostics.
METADATA_KEY = "context_languages"

# A frame is a notice, not a report. Enough codes to be specific, few enough that
# a 500-file mixed scope does not put a paragraph of ISO codes on screen.
_MAX_REPORTED_LANGUAGES = 8


@dataclass(frozen=True)
class ContextLanguages:
    """Languages of the transcripts one chat turn could draw on.

    Three buckets, deliberately — ``unknown`` is its own outcome. A file whose
    language was never detected must not be counted as English (that would hide a
    real Spanish recording) nor as non-English (that would fire the warning across
    every library recorded before language detection existed). It is reported as
    what it is, in the metadata, and never on its own triggers the warning.
    """

    supported: tuple[str, ...] = ()
    unsupported: tuple[str, ...] = ()
    supported_files: int = 0
    unsupported_files: int = 0
    unknown_files: int = 0

    @property
    def total_files(self) -> int:
        return self.supported_files + self.unsupported_files + self.unknown_files

    @property
    def has_unsupported(self) -> bool:
        """Whether any in-scope transcript is in a language RAG cannot serve."""
        return bool(self.unsupported)

    def as_metadata(self) -> dict:
        """Diagnostics for ``msg_metadata`` — counts and codes, never text."""
        return {
            "supported": list(self.supported),
            "unsupported": list(self.unsupported),
            "files": self.total_files,
            "unsupported_files": self.unsupported_files,
            "unknown_files": self.unknown_files,
        }


def _classify(
    rows: Iterable[str | None], allowed: frozenset[str] | None = SUPPORTED_RAG_LANGUAGES
) -> ContextLanguages:
    """Bucket raw language values into supported / unsupported / unknown.

    Args:
        rows: Raw ``MediaFile.language`` values.
        allowed: The supported set, or :data:`ALL_LANGUAGES` (``None``) when the
            active embedding model is multilingual — then nothing is unsupported.
    """
    supported: set[str] = set()
    unsupported: set[str] = set()
    supported_files = unsupported_files = unknown_files = 0

    for raw in rows:
        code = normalize_language(raw)
        if code is None:
            unknown_files += 1
        elif allowed is None or code in allowed:
            supported.add(code)
            supported_files += 1
        else:
            unsupported.add(code)
            unsupported_files += 1

    return ContextLanguages(
        supported=tuple(sorted(supported)),
        unsupported=tuple(sorted(unsupported)),
        supported_files=supported_files,
        unsupported_files=unsupported_files,
        unknown_files=unknown_files,
    )


def _parse_uuids(values: Iterable[str]) -> list[uuid_pkg.UUID]:
    """Parse uuid strings, skipping anything unparseable rather than raising."""
    parsed: list[uuid_pkg.UUID] = []
    for value in values:
        try:
            parsed.append(uuid_pkg.UUID(str(value)))
        except (ValueError, AttributeError, TypeError):
            logger.debug("Chat language scope: skipping malformed uuid %r", value)
    return parsed


def describe_context_languages(
    db: Session,
    *,
    scope_file_uuids: list[str] | None,
    grounded_file_uuids: Iterable[str] = (),
) -> ContextLanguages:
    """Describe the languages one turn's context is drawn from.

    Two sources, unioned, for two different failure modes:

    * **The resolved scope** — files the user explicitly selected. A Spanish file
      here may never be retrieved *at all* for an English question, so waiting for
      it to appear in the excerpts would mean never warning about the case that
      matters most.
    * **The files the excerpts came from** — the only signal available when the
      scope is ``None``.

    ``scope_file_uuids is None`` means "every accessible transcript" and is
    deliberately **not** enumerated: firing on any one foreign recording anywhere
    in a library would put the warning on nearly every turn, and a warning that is
    always on is one nobody reads.

    Both inputs are already authorized — the scope by ``context_resolver`` and the
    excerpts by the retriever's ``accessible_user_ids`` filter — so no permission
    filter is reapplied here; this reads a column on rows the caller has been
    granted already.

    Args:
        db: Database session.
        scope_file_uuids: The resolved scope, or ``None`` for "all accessible".
        grounded_file_uuids: File uuids the retrieved excerpts came from.

    Returns:
        A :class:`ContextLanguages`. Empty (and therefore silent) when nothing is
        in scope or the lookup fails — a diagnostic must never break a chat turn,
        and a warning invented from a failed read would be worse than none.
    """
    from app.models.media import MediaFile

    wanted: set[str] = set(grounded_file_uuids)
    if scope_file_uuids is not None:
        wanted |= set(scope_file_uuids)
    if not wanted:
        return ContextLanguages()

    parsed = _parse_uuids(wanted)
    if not parsed:
        return ContextLanguages()

    try:
        rows = db.query(MediaFile.language).filter(MediaFile.uuid.in_(parsed)).all()
    except Exception:  # noqa: BLE001 — never fail a chat turn on a diagnostic read
        logger.exception("Chat language scope lookup failed; skipping the language notice")
        return ContextLanguages()

    return _classify((row[0] for row in rows), supported_rag_languages(db))


def resolve_text_field_preset_for_locale(locale: str | None) -> str:
    """Map a per-turn locale hint to a #506 ``text_fields`` preset name.

    This is the adoption path for the no-stemmed-leg arm, not the decision to
    use it: it only returns a preset **name** (from
    ``search.chunk_retrieval.TEXT_FIELD_PRESETS``); a caller must still resolve
    that name to a field list via
    ``search.chunk_retrieval.resolve_text_field_preset`` and pass it into
    ``retrieve_chunks``/``retrieve_digests`` as ``text_fields``. Nothing in this
    module calls it automatically — see the module docstring's "BM25 field
    preset" entry.

    Args:
        locale: A BCP-47 or ISO 639-1 locale hint (``"es"``, ``"es-MX"``,
            ``"en-US"``), or ``None``/blank when no hint is available.

    Returns:
        ``TEXT_FIELD_PRESET_NO_STEM`` for a recognisable non-English locale,
        else ``TEXT_FIELD_PRESET_DEFAULT`` — including when the locale is
        missing or unparseable, so an absent hint behaves exactly as today
        (the stemmed leg stays in the query).
    """
    from app.services.search.chunk_retrieval import TEXT_FIELD_PRESET_DEFAULT
    from app.services.search.chunk_retrieval import TEXT_FIELD_PRESET_NO_STEM

    code = normalize_language(locale)
    if code is not None and code != "en":
        return TEXT_FIELD_PRESET_NO_STEM
    return TEXT_FIELD_PRESET_DEFAULT


def warning_payload(metadata: dict | None) -> dict | None:
    """Build the ``warning`` frame payload from a turn's language metadata.

    Reads the same dict that is persisted on the message, so the streamed notice
    and the reloaded one can never describe different things.

    Args:
        metadata: A turn's ``msg_metadata``, or None.

    Returns:
        The SSE payload, or ``None`` when no unsupported language is in scope.
    """
    languages = (metadata or {}).get(METADATA_KEY) or {}
    unsupported = list(languages.get("unsupported") or [])
    if not unsupported:
        return None
    return {
        "code": WARNING_CODE,
        "languages": unsupported[:_MAX_REPORTED_LANGUAGES],
        "files": int(languages.get("unsupported_files") or 0),
        "unknown_files": int(languages.get("unknown_files") or 0),
        # Reachable only when `unsupported` is non-empty, which `_classify` can only
        # produce from a concrete allowed set — and the only concrete set is this
        # one. A multilingual model yields ALL_LANGUAGES and no payload at all.
        "supported": sorted(SUPPORTED_RAG_LANGUAGES),
    }
