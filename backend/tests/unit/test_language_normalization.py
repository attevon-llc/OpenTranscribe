"""One parser for a language value, applied at the boundary that writes the column.

Two implementations of ``normalize_language`` used to exist — ``services/chat/language.py``
and ``services/redaction/config.py`` — both taking ``MediaFile.language`` and **disagreeing
on 13 of 21 inputs** (issue #545). The chat one answered ``None`` for anything it could not
identify; the redaction one answered ``"en"`` for the sentinels and passed everything else
through untouched. Each call site happened to import the one its domain wanted, which is why
this was a latent hazard rather than a live defect — and why unifying them is what makes the
redaction fail-open in ``tests/redaction/test_language_fail_closed.py`` fixable at all.

What is pinned here:

1. **There is exactly ONE module-level ``def normalize_language`` under ``app/``.** The two
   old modules keep the *name* as a re-import, so ``chat/language.py``'s intra-module callers
   and ``tests/test_chat_language_scope.py`` are untouched — but a second *definition* is the
   thing that can drift, and that is what the test counts.
2. **``None`` means "we could not identify this", never ``"en"``.** ``default`` is the
   explicit opt-in for a caller that genuinely wants a fallback; nothing in the app passes one.
3. **The unification is not a rewrite.** Every input the two old implementations already
   agreed on still normalizes to the same value, with the deliberate improvements enumerated
   in ``_DELIBERATE_CHANGES`` — each one a value they agreed on that was *not a language*.
4. **The ASR boundary normalizes, so the column only ever holds a code or NULL.** An
   ``ASRResult`` is the provider's output type and ``update_media_file_transcription_status``
   is the only place that assigns ``media_file.language``; both normalize, so the local
   (WhisperX) path is covered as well as the cloud ones.
"""

from __future__ import annotations

import ast
import uuid as uuid_pkg
from pathlib import Path

import pytest

import app as app_pkg
from app.services.asr.types import ASRResult
from app.tasks.transcription.storage import update_media_file_transcription_status
from app.utils.language import normalize_language

# ---------------------------------------------------------------------------
# 1. Exactly one definition
# ---------------------------------------------------------------------------

# ``app`` is a namespace package (no ``__init__.py``), so ``__file__`` is None.
APP_ROOT = Path(next(iter(app_pkg.__path__))).resolve()
CANONICAL = APP_ROOT / "utils" / "language.py"


def _module_level_definitions(name: str) -> list[str]:
    """Every ``app/`` module carrying a module-level ``def <name>``, as repo-relative paths."""
    found: list[str] = []
    for path in sorted(APP_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:  # module level only — a nested helper is not a second copy
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name:
                found.append(str(path.relative_to(APP_ROOT.parent)))
    return found


def test_exactly_one_module_level_definition_of_normalize_language() -> None:
    """Two implementations of one concept is the defect; one name in three places is not."""
    definitions = _module_level_definitions("normalize_language")

    assert definitions == [str(CANONICAL.relative_to(APP_ROOT.parent))], definitions


def test_the_two_old_modules_still_export_the_name() -> None:
    """CONTROL. Deleting the names would be a different change — every caller would move.

    ``chat/language.py`` uses it twice intra-module and ``tests/test_chat_language_scope.py``
    calls it on that module; ``redaction/service.py`` imports it from ``redaction/config.py``.
    Both keep the name as a re-import, which is why the count above is 1 and not 3.
    """
    from app.services.chat import language as chat_language
    from app.services.redaction import config as redaction_config

    assert chat_language.normalize_language is normalize_language
    assert redaction_config.normalize_language is normalize_language


# ---------------------------------------------------------------------------
# 2. Unknown is None, never English
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [None, "", "   ", "auto", "und", "unknown", "none", "null", "nan", "undefined", "123", "e1"],
)
def test_a_value_that_names_no_language_is_none(raw) -> None:
    """The redaction copy answered ``"en"`` for half of these and echoed the rest verbatim.

    Both are fail-open: ``"en"`` runs the English detectors over text of unknown language and
    reports full coverage, and an echoed value reads downstream as "a language with no
    detector", which is a *legitimate* skip and therefore reported as no gap at all.
    """
    assert normalize_language(raw) is None


def test_a_caller_that_wants_a_fallback_must_ask_for_one() -> None:
    """``default`` is keyword-only and opt-in; no caller in the app passes it."""
    assert normalize_language(None) is None
    assert normalize_language(None, default="en") == "en"
    assert normalize_language("xyzzy", default="en") == "en"
    assert normalize_language("fr", default="en") == "fr", "a real code ignores the fallback"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("en", "en"),
        ("EN", "en"),
        ("en-US", "en"),
        ("en_GB", "en"),
        ("en ", "en"),
        (" en", "en"),
        ("  es  ", "es"),
        ("zh-Hans", "zh"),
        ("eng", "en"),
        ("English", "en"),
        ("fra", "fr"),
        ("fre", "fr"),
        ("deu", "de"),
        ("ger", "de"),
        ("Spanish", "es"),
        ("haitian creole", "ht"),
    ],
)
def test_the_shapes_real_providers_emit_reduce_to_a_bare_code(raw, expected) -> None:
    assert normalize_language(raw) == expected


def test_every_alias_resolves_to_a_language_the_app_knows() -> None:
    """A typo in the alias table would silently drop that spelling, not fail loudly.

    ``_build_vocabulary`` merges the alias map over the app's own code table, so an alias
    pointing at a code that is not in ``WHISPER_LANGUAGES`` would make ``normalize_language``
    return a value no other reader recognises.
    """
    from app.core import constants as C  # noqa: N812
    from app.utils.language import _ALIAS_CODES

    unknown = {
        alias: code for alias, code in _ALIAS_CODES.items() if code not in C.WHISPER_LANGUAGES
    }

    assert _ALIAS_CODES, "the table is the point of the test"
    assert unknown == {}
    assert "auto" not in set(_ALIAS_CODES.values()), "the not-detected sentinel is not a language"


# ---------------------------------------------------------------------------
# 3. Control: unification did not quietly change the agreeing cases
# ---------------------------------------------------------------------------


def _old_chat_normalize(raw: str | None) -> str | None:
    """``services/chat/language.py:126`` as it stood before the unification."""
    unknown = frozenset({"", "und", "undefined", "unknown", "none", "null", "auto", "nan"})
    if not raw:
        return None
    code = raw.strip().lower().replace("_", "-").split("-", 1)[0]
    if code in unknown or not code.isalpha():
        return None
    return code


def _old_redaction_normalize(language: str | None) -> str:
    """``services/redaction/config.py:512`` as it stood before the unification."""
    if not language or language.lower() in ("auto", "und", "unknown"):
        return "en"
    return language.lower().split("-")[0].split("_")[0]


_CORPUS: tuple[str | None, ...] = (
    "en", "EN", "en-US", "en_GB", "en ", " en", "eng", "English", "es", "  es  ",
    "zh-Hans", "fr", "fra", "de", "deu", "ger", "zz", "xyzzy", "e1", "123",
    "auto", "und", "unknown", "none", "null", "nan", "undefined", "", "   ", None,
)  # fmt: skip

#: The inputs the two old implementations agreed on where the agreed value was **not a
#: language**, and which therefore change deliberately. Every other agreeing input is
#: preserved byte-for-byte by the assertion below.
#:
#: * ``eng`` / ``English`` / ``fra`` / ``deu`` / ``ger`` — both echoed a string that is a real
#:   language written in a shape neither could read. Mapping them is the point of the parser.
#: * ``zz`` / ``xyzzy`` — both echoed a string that names no language at all. Echoing it is the
#:   fail-open B5 closes: downstream it reads as "a recognised language that happens to have no
#:   PII detector", which is a *legitimate* skip and so is subtracted from the coverage report.
_DELIBERATE_CHANGES: dict[str | None, str | None] = {
    "eng": "en",
    "English": "en",
    "fra": "fr",
    "deu": "de",
    "ger": "de",
    "zz": None,
    "xyzzy": None,
}


def test_every_input_the_two_old_implementations_agreed_on_is_preserved_or_listed() -> None:
    """CONTROL. Unifying two parsers must not silently re-answer the cases they agreed on."""
    agreed: dict[str | None, str | None] = {
        raw: _old_chat_normalize(raw)
        for raw in _CORPUS
        if _old_chat_normalize(raw) == _old_redaction_normalize(raw)
    }
    assert set(agreed) == {
        "en", "EN", "en-US", "en_GB", "eng", "English", "es",
        "zh-Hans", "fr", "fra", "de", "deu", "ger", "zz", "xyzzy",
    }, sorted(map(str, agreed))  # fmt: skip
    assert set(_DELIBERATE_CHANGES) <= set(agreed), (
        "an entry here that the old pair never agreed on is a stale exemption"
    )

    expected: dict[str | None, str | None] = {**agreed, **_DELIBERATE_CHANGES}
    assert {raw: normalize_language(raw) for raw in agreed} == expected


# ---------------------------------------------------------------------------
# 4. The ASR → column boundary
# ---------------------------------------------------------------------------


def _seed_file(db_session, user, *, language: str | None = None):
    from app.core.enums import FileStatus
    from app.models.media import MediaFile

    media = MediaFile(
        uuid=uuid_pkg.uuid4(),
        user_id=user.id,
        filename=f"lang-{uuid_pkg.uuid4().hex[:8]}.wav",
        storage_path=f"language-test/{uuid_pkg.uuid4().hex}",
        file_size=1,
        content_type="audio/wav",
        language=language,
        status=FileStatus.PROCESSING,
    )
    db_session.add(media)
    db_session.flush()
    return media


_SEGMENTS = [{"start": 0.0, "end": 1.0, "text": "hello"}]


def _store(db_session, media, provider_language: str | None) -> str | None:
    """Provider output → the column, through the two real functions on that path.

    ``ASRResult`` is what every cloud provider returns (``cloud_asr.py`` copies its
    ``.language`` straight into the result dict) and
    ``update_media_file_transcription_status`` is the ONE place ``media_file.language`` is
    assigned, for the local path as well.
    """
    result = ASRResult(segments=[], language=provider_language)
    update_media_file_transcription_status(
        db_session, media.id, _SEGMENTS, language=result.language
    )
    db_session.refresh(media)
    stored: str | None = media.language  # the ORM attribute is untyped; this is the column
    return stored


@pytest.mark.parametrize("provider_language", ["english", "English", "eng", "en ", "en-US"])
def test_an_asr_provider_language_reaches_the_column_normalized(
    db_session, normal_user, provider_language
) -> None:
    """A cloud provider's spelling must not become the value every redaction reader keys on."""
    media = _seed_file(db_session, normal_user)

    assert _store(db_session, media, provider_language) == "en"


def test_a_recognised_non_english_code_is_left_alone(db_session, normal_user) -> None:
    """CONTROL. Normalizing is not "make everything English"."""
    media = _seed_file(db_session, normal_user)

    assert _store(db_session, media, "fr") == "fr"


def test_an_unidentifiable_provider_language_is_stored_as_null(db_session, normal_user) -> None:
    """CONTROL, and the one that matters most.

    ``"en"`` here would run the English PII detector over text of unknown language and record
    full coverage — a NEW fail-open, against a contract that says PII support is English-only
    by design. NULL is what every downstream reader treats as "undetermined".
    """
    media = _seed_file(db_session, normal_user)

    assert _store(db_session, media, "Klingon") is None


def test_the_result_object_itself_carries_the_normalized_value(db_session, normal_user) -> None:
    """The normalization is at the provider boundary, not only at the DB write.

    ``diarization_merge.py`` rebuilds an ``ASRResult`` from another one's ``.language``, and
    ``cloud_asr.py`` reads it into the result dict — both see the normalized value.
    """
    assert ASRResult(segments=[], language="eng").language == "en"
    assert ASRResult(segments=[], language="Klingon").language is None
    assert ASRResult(segments=[], language="fr").language == "fr", "control"


def test_a_local_whisperx_code_still_reaches_the_column(db_session, normal_user) -> None:
    """CONTROL for the default deployment: the local path passes a bare code and is unaffected."""
    media = _seed_file(db_session, normal_user)
    update_media_file_transcription_status(db_session, media.id, _SEGMENTS, language="es")
    db_session.refresh(media)

    assert media.language == "es"
