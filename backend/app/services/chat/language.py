"""Language scope for RAG chat: what the retrieval stack can actually answer over.

**Transcription is multilingual; RAG and chat are not.** WhisperX transcribes 100+
languages and nothing here changes that — this module only describes what the
*question-answering* path can do with the result.

Every stage between a transcript and an answer is tuned for English:

* the ``transcript_chunks`` BM25 analyzer is ``english_stop`` + ``english_snowball``
  (``services/search/indexing_service.py:55``), so a Spanish query stems as if it
  were English and matches almost nothing;
* the default embedding model is ``all-MiniLM-L6-v2``, declared
  ``"languages": ["en"]`` in ``core/constants.py:OPENSEARCH_EMBEDDING_MODELS``;
* the cross-encoder reranker is ``ms-marco-MiniLM-L-6-v2``, an English MS MARCO
  model (``core/constants.py:CHAT_RERANKER_MODEL``);
* the base system rules and the query rewriter prompt are written in English.

The failure that produces is **silent**: a non-English transcript is not retrieved
for an English query, so the model answers confidently from whatever English
material remains, and nothing on screen says a recording was effectively invisible.
This module turns that into a visible, non-fatal warning.

**Not admin-tunable, on purpose.** An operator *can* select a multilingual
embedding model, but that fixes one of the four stages above; a knob that let them
declare "Spanish is supported" would be dishonest about the other three. When
multilingual RAG lands, :data:`SUPPORTED_RAG_LANGUAGES` widens in code alongside
the pipeline that earns it.
"""

from __future__ import annotations

import logging
import uuid as uuid_pkg
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

#: Languages the retrieval + prompting stack is actually tuned for. See the module
#: docstring for why this is code and not a ``SystemSettings`` row.
SUPPORTED_RAG_LANGUAGES: frozenset[str] = frozenset({"en"})

#: SSE ``warning`` frame code and the ``msg_metadata`` flag, mirroring
#: ``context_dropped``: one code, set live on the frame and persisted on the row,
#: so the notice survives a reload instead of existing only for the stream.
WARNING_CODE = "unsupported_language"

#: ``msg_metadata`` key carrying the per-turn language diagnostics.
METADATA_KEY = "context_languages"

# Values a detector writes when it declined to commit to a language. Treated as
# UNKNOWN, which is neither "English" nor "not English" — see ContextLanguages.
_UNKNOWN_TOKENS = frozenset({"", "und", "undefined", "unknown", "none", "null", "auto", "nan"})

# A frame is a notice, not a report. Enough codes to be specific, few enough that
# a 500-file mixed scope does not put a paragraph of ISO codes on screen.
_MAX_REPORTED_LANGUAGES = 8


def normalize_language(raw: str | None) -> str | None:
    """Reduce a stored language value to a bare ISO 639-1 code.

    ``MediaFile.language`` is written by whichever ASR provider ran, and they do
    not agree on shape: ``en``, ``EN``, ``en-US`` and ``en_GB`` all occur. The
    region subtag is irrelevant here — English is English — so it is dropped.

    Args:
        raw: The stored value, possibly ``None``, blank, or a placeholder.

    Returns:
        A lowercase primary subtag, or ``None`` when the language is unknown.
    """
    if not raw:
        return None
    code = raw.strip().lower().replace("_", "-").split("-", 1)[0]
    if code in _UNKNOWN_TOKENS or not code.isalpha():
        return None
    return code


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


def _classify(rows: Iterable[str | None]) -> ContextLanguages:
    """Bucket raw language values into supported / unsupported / unknown."""
    supported: set[str] = set()
    unsupported: set[str] = set()
    supported_files = unsupported_files = unknown_files = 0

    for raw in rows:
        code = normalize_language(raw)
        if code is None:
            unknown_files += 1
        elif code in SUPPORTED_RAG_LANGUAGES:
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

    return _classify(row[0] for row in rows)


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
        "supported": sorted(SUPPORTED_RAG_LANGUAGES),
    }
