"""RAG chat is English-only, and a turn must say so when it isn't (task #37).

Transcription stays multilingual — nothing here touches ASR. What is English-only
is the *question-answering* path: an English BM25 analyzer, an English embedding
model, an English cross-encoder and English prompts. A Spanish recording in scope
is therefore near-invisible to retrieval, and before this the product answered
from whatever English material remained with no signal that anything had been left
out.

Every "the warning fires" test below is paired with a **control** that runs the
same code path over an all-English scope and asserts silence. Without the control
these tests would still pass if the notice were emitted unconditionally, which is
the failure mode the notice itself exists to avoid: a warning that is always on is
one nobody reads.
"""

from __future__ import annotations

import json
import uuid as uuid_pkg
from contextlib import contextmanager

import pytest

from app.models.media import MediaFile
from app.services.chat import language as lang
from app.services.chat import service as chat_service
from app.services.chat.redactor import MaskedChunk
from app.services.chat.settings import ChatSettings
from app.services.llm_stream import LLMStreamEvent
from app.services.search.chunk_retrieval import ChunkHit


def _make_file(db, user, *, language, title="Recording"):
    """A completed transcript with a given detected language (None = undetected)."""
    media = MediaFile(
        uuid=uuid_pkg.uuid4(),
        user_id=user.id,
        filename=f"{title}-{uuid_pkg.uuid4()}.mp4",
        title=title,
        storage_path=f"media/test/{uuid_pkg.uuid4()}.mp4",
        content_type="video/mp4",
        file_size=1000,
        status="completed",
        language=language,
    )
    db.add(media)
    db.commit()
    db.refresh(media)
    return media


# ---------------------------------------------------------------------------
# normalize_language — ASR providers do not agree on the shape of a code
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("en", "en"),
        ("EN", "en"),
        ("en-US", "en"),
        ("en_GB", "en"),
        ("  es  ", "es"),
        ("zh-Hans", "zh"),
        (None, None),
        ("", None),
        ("   ", None),
        ("unknown", None),
        ("und", None),
        ("auto", None),
        ("123", None),
    ],
)
def test_normalize_language_reduces_a_stored_value_to_a_bare_code(raw, expected):
    """Region subtags are dropped; placeholders become "unknown", not a language."""
    assert lang.normalize_language(raw) == expected


def test_english_regional_variants_are_supported_not_flagged():
    """en-US must not read as a language RAG cannot serve."""
    assert lang.normalize_language("en-US") in lang.SUPPORTED_RAG_LANGUAGES


# ---------------------------------------------------------------------------
# describe_context_languages — against real rows
# ---------------------------------------------------------------------------


def test_a_non_english_transcript_in_scope_is_reported_as_unsupported(db_session, normal_user):
    english = _make_file(db_session, normal_user, language="en")
    spanish = _make_file(db_session, normal_user, language="es")

    result = lang.describe_context_languages(
        db_session,
        scope_file_uuids=[str(english.uuid), str(spanish.uuid)],
    )

    assert result.has_unsupported is True
    assert result.unsupported == ("es",)
    assert result.supported == ("en",)
    assert result.unsupported_files == 1
    assert result.total_files == 2


def test_an_all_english_scope_reports_nothing_unsupported(db_session, normal_user):
    """CONTROL for the test above: same call, opposite outcome, driven only by data."""
    first = _make_file(db_session, normal_user, language="en")
    second = _make_file(db_session, normal_user, language="en-US")

    result = lang.describe_context_languages(
        db_session,
        scope_file_uuids=[str(first.uuid), str(second.uuid)],
    )

    assert result.has_unsupported is False
    assert result.unsupported == ()
    assert result.unsupported_files == 0
    assert result.total_files == 2


def test_an_undetected_language_is_neither_english_nor_unsupported(db_session, normal_user):
    """Unknown is its own bucket.

    Counting it as English would hide a real Spanish recording; counting it as
    non-English would fire the notice across every library recorded before
    language detection existed. It is reported as unknown and warns on nothing.
    """
    unknown = _make_file(db_session, normal_user, language=None)
    blank = _make_file(db_session, normal_user, language="")

    result = lang.describe_context_languages(
        db_session,
        scope_file_uuids=[str(unknown.uuid), str(blank.uuid)],
    )

    assert result.unknown_files == 2
    assert result.supported == ()
    assert result.unsupported == ()
    assert result.has_unsupported is False


def test_unknown_files_are_counted_alongside_a_real_unsupported_language(db_session, normal_user):
    """The count travels with the warning so "we don't know" is stated, not implied."""
    spanish = _make_file(db_session, normal_user, language="es")
    unknown = _make_file(db_session, normal_user, language=None)

    result = lang.describe_context_languages(
        db_session,
        scope_file_uuids=[str(spanish.uuid), str(unknown.uuid)],
    )

    assert result.unsupported == ("es",)
    assert result.unknown_files == 1
    assert result.as_metadata()["unknown_files"] == 1


def test_an_all_accessible_scope_is_judged_only_on_what_was_retrieved(db_session, normal_user):
    """``scope_file_uuids is None`` must not enumerate the library.

    A foreign recording somewhere in a library the user never selected is not a
    reason to warn on every turn; one the answer is actually drawn from is.
    """
    ignored_spanish = _make_file(db_session, normal_user, language="es")
    grounded_english = _make_file(db_session, normal_user, language="en")

    silent = lang.describe_context_languages(
        db_session,
        scope_file_uuids=None,
        grounded_file_uuids=[str(grounded_english.uuid)],
    )
    assert silent.has_unsupported is False
    assert silent.total_files == 1

    # Same wide scope, but the excerpts now come from the Spanish recording.
    fires = lang.describe_context_languages(
        db_session,
        scope_file_uuids=None,
        grounded_file_uuids=[str(ignored_spanish.uuid)],
    )
    assert fires.has_unsupported is True
    assert fires.unsupported == ("es",)


def test_a_scoped_file_that_retrieval_never_surfaced_still_warns(db_session, normal_user):
    """The case that matters most: the Spanish file was selected and found nothing.

    Waiting for a non-English file to appear in the excerpts would mean never
    warning about it, because retrieval is exactly what fails on it.
    """
    english = _make_file(db_session, normal_user, language="en")
    spanish = _make_file(db_session, normal_user, language="es")

    result = lang.describe_context_languages(
        db_session,
        scope_file_uuids=[str(english.uuid), str(spanish.uuid)],
        grounded_file_uuids=[str(english.uuid)],
    )

    assert result.has_unsupported is True
    assert result.unsupported == ("es",)


def test_an_empty_scope_selection_describes_nothing(db_session):
    """``[]`` matches nothing, so there is no context to have a language."""
    result = lang.describe_context_languages(db_session, scope_file_uuids=[])

    assert result.total_files == 0
    assert result.has_unsupported is False


def test_malformed_uuids_are_skipped_rather_than_raising(db_session, normal_user):
    spanish = _make_file(db_session, normal_user, language="es")

    result = lang.describe_context_languages(
        db_session,
        scope_file_uuids=["not-a-uuid", str(spanish.uuid)],
    )

    assert result.unsupported == ("es",)
    assert result.total_files == 1


# ---------------------------------------------------------------------------
# warning_payload — the frame is built from the SAME dict that is persisted
# ---------------------------------------------------------------------------


def test_warning_payload_is_built_from_persisted_metadata():
    metadata = {
        lang.METADATA_KEY: {
            "supported": ["en"],
            "unsupported": ["es", "fr"],
            "files": 5,
            "unsupported_files": 2,
            "unknown_files": 1,
        }
    }

    payload = lang.warning_payload(metadata)

    assert payload == {
        "code": "unsupported_language",
        "languages": ["es", "fr"],
        "files": 2,
        "unknown_files": 1,
        "supported": ["en"],
    }


@pytest.mark.parametrize(
    "metadata",
    [
        None,
        {},
        {lang.METADATA_KEY: {"supported": ["en"], "unsupported": [], "unknown_files": 3}},
    ],
)
def test_warning_payload_is_none_without_an_unsupported_language(metadata):
    """CONTROL: nothing unsupported — including an all-unknown scope — stays silent."""
    assert lang.warning_payload(metadata) is None


# ---------------------------------------------------------------------------
# _prepare_context wiring — the real service function, real DB rows
# ---------------------------------------------------------------------------


def _run_prepare_context(monkeypatch, db, *, file_uuids, chunk_file_uuids):
    """Drive the real ``_prepare_context`` with retrieval and masking stubbed."""
    from app.services.chat.retrieval import RetrievalResult

    chunks = [
        ChunkHit(file_uuid=uuid, file_id=i, chunk_index=i, content=f"said something {i}")
        for i, uuid in enumerate(chunk_file_uuids, start=1)
    ]
    monkeypatch.setattr(
        chat_service,
        "retrieve_context",
        lambda **_kwargs: RetrievalResult(chunks=chunks, retrieved=len(chunks)),
    )
    monkeypatch.setattr(
        chat_service,
        "mask_chunks",
        lambda _db, hits, _user_id: [MaskedChunk(source=h, content=h.content) for h in hits],
    )

    # `_prepare_context` takes no `db` and opens its OWN session per phase, so a
    # caller cannot reintroduce a session held across OpenSearch or an LLM round
    # trip (services/chat/CLAUDE.md). That means it would open a SECOND connection
    # here and see none of the rows this test seeded — the fixture's transaction is
    # never committed. Point the phase factory at the fixture session so the phases
    # read the rows under test; the lifetime property itself is covered by
    # tests/unit/test_chat_session_phases.py.
    @contextmanager
    def _fixture_session():
        yield db

    monkeypatch.setattr("app.db.session_utils.session_scope", _fixture_session)

    # It returns FOUR values — (masked, meta, counted, overview) — since the
    # router gained the counted and overview tiers. Only the first two matter
    # here; unpacking all four keeps this helper honest about the contract
    # rather than silently swallowing a future fifth.
    masked, meta, _counted, _overview = chat_service._prepare_context(
        user_id=1,
        organization_id=None,
        question="What did they decide?",
        history=[],
        settings=ChatSettings(),
        file_uuids=file_uuids,
        speakers=None,
        search_mode="hybrid",
        llm=None,
        rewrite_enabled=False,
    )
    return masked, meta


def test_prepare_context_flags_a_non_english_scope(monkeypatch, db_session, normal_user):
    english = _make_file(db_session, normal_user, language="en")
    spanish = _make_file(db_session, normal_user, language="es")

    _masked, meta = _run_prepare_context(
        monkeypatch,
        db_session,
        file_uuids=[str(english.uuid), str(spanish.uuid)],
        chunk_file_uuids=[str(english.uuid)],
    )

    assert meta["unsupported_language"] is True
    assert meta[lang.METADATA_KEY]["unsupported"] == ["es"]
    assert meta[lang.METADATA_KEY]["files"] == 2


def test_prepare_context_leaves_an_english_scope_unflagged(monkeypatch, db_session, normal_user):
    """CONTROL: same function, same stubs, only the recordings' language differs."""
    first = _make_file(db_session, normal_user, language="en")
    second = _make_file(db_session, normal_user, language="en")

    _masked, meta = _run_prepare_context(
        monkeypatch,
        db_session,
        file_uuids=[str(first.uuid), str(second.uuid)],
        chunk_file_uuids=[str(first.uuid)],
    )

    assert "unsupported_language" not in meta
    assert meta[lang.METADATA_KEY]["unsupported"] == []
    # The diagnostics are still recorded, so "all English" is a stated fact.
    assert meta[lang.METADATA_KEY]["supported"] == ["en"]


# ---------------------------------------------------------------------------
# The SSE frame — the real stream_reply generator
# ---------------------------------------------------------------------------


class _FakeProvider:
    value = "custom"


class _FakeConfig:
    provider = _FakeProvider()
    model = "test-model"


class _FakeLLM:
    def __init__(self) -> None:
        self.config = _FakeConfig()
        self.user_context_window = 32_000
        self.response_tokens = 4000

    def chat_completion_stream(self, messages, cancel_event=None, **_kwargs):
        yield LLMStreamEvent(type="delta", text="An answer citing [1].")
        yield LLMStreamEvent(type="done", finish_reason="stop")

    def estimate_tokens(self, text: str) -> int:
        return len(text) // 4


@contextmanager
def _null_session():
    yield None


async def _stream_warnings(monkeypatch, *, meta, use_context=True):
    """Run one real turn with ``_prepare_context`` stubbed; return warning frames."""
    chunk = ChunkHit(
        file_uuid="11111111-1111-1111-1111-111111111111",
        file_id=1,
        chunk_index=0,
        content="short and relevant",
        title="Recording",
    )
    chunks = [MaskedChunk(source=chunk, content=chunk.content)]

    monkeypatch.setattr("app.db.session_utils.session_scope", _null_session)
    # Four values: (masked, meta, counted, overview). The last two are the router's
    # structured blocks and are None for this turn — but the arity has to match, or
    # the unpack raises inside the generator and the warning frame under test is
    # never reached, which reads as "no warning emitted" rather than as an error.
    monkeypatch.setattr(
        chat_service, "_prepare_context", lambda *_a, **_kw: (list(chunks), dict(meta), None, None)
    )
    monkeypatch.setattr(chat_service.limits, "is_cancelled", lambda _uuid: False)

    async def _fake_finalize(**_kwargs):
        return None

    monkeypatch.setattr(chat_service, "_finalize_turn", _fake_finalize)

    warnings: list[dict] = []
    generator = chat_service.ChatService.stream_reply(
        conversation_id=1,
        conversation_uuid="conv-uuid",
        user_id=1,
        organization_id=None,
        question="What did the team decide?",
        history=[],
        file_uuids=None,
        speakers=[],
        settings=ChatSettings(),
        use_context=use_context,
        system_prompt="SYS",
        search_mode="hybrid",
        temperature=None,
        max_tokens=None,
        top_p=None,
        llm=_FakeLLM(),
        assistant_message_uuid="00000000-0000-0000-0000-0000000000aa",
        user_message_uuid="00000000-0000-0000-0000-0000000000bb",
        is_first_exchange=True,
    )
    async for raw in generator:
        if raw.startswith(":"):
            continue
        name = raw.split("event: ", 1)[1].split("\n", 1)[0]
        if name == "warning":
            warnings.append(json.loads(raw.split("data: ", 1)[1].strip()))
    return warnings


_SPANISH_META = {
    lang.METADATA_KEY: {
        "supported": ["en"],
        "unsupported": ["es"],
        "files": 2,
        "unsupported_files": 1,
        "unknown_files": 0,
    },
    "unsupported_language": True,
}

_ENGLISH_META = {
    lang.METADATA_KEY: {
        "supported": ["en"],
        "unsupported": [],
        "files": 2,
        "unsupported_files": 0,
        "unknown_files": 0,
    },
}


@pytest.mark.asyncio
async def test_a_non_english_scope_emits_a_warning_frame_and_still_answers(monkeypatch):
    """Answering with a notice, not refusing.

    A mixed library is the normal case; refusing every question because one
    recording is Spanish would be worse than useless.
    """
    warnings = await _stream_warnings(monkeypatch, meta=_SPANISH_META)

    assert warnings == [
        {
            "code": "unsupported_language",
            "languages": ["es"],
            "files": 1,
            "unknown_files": 0,
            "supported": ["en"],
        }
    ]


@pytest.mark.asyncio
async def test_an_english_scope_emits_no_warning_frame(monkeypatch):
    """CONTROL: identical turn, identical stubs — only the metadata differs."""
    warnings = await _stream_warnings(monkeypatch, meta=_ENGLISH_META)

    assert warnings == []


@pytest.mark.asyncio
async def test_no_context_mode_never_emits_a_language_warning(monkeypatch):
    """Pure-LLM chat consults no transcripts, so it has no language to report."""
    warnings = await _stream_warnings(monkeypatch, meta=_SPANISH_META, use_context=False)

    assert warnings == []
