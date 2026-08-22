"""``services/documents/chunking.py`` must thread the document's own language into
sentence splitting, never a hardcoded ``"en"`` (issue #448's guard, lane C0 task 7).

``_split_long_block`` used to call ``split_into_sentences(block.text)`` with no
``language`` argument at all, which defaults to ``"en"``. ``chunking_service.
_punkt_can_read`` only applies its script/terminator disqualifiers to a language it
does not recognise — "en" is always recognised — so the hardcoded default silently
defeated the #448 guard for every document regardless of its real script: a long
Devanagari or Thai block was handed to English punkt, which finds no ``.``/``!``/``?``
and returns the WHOLE block as one "sentence," so ``_split_long_block`` produces
exactly one oversized chunk instead of the several the block should split into.

Both tests below are against the REAL Devanagari-splitting behaviour (not a mock of
it), measured directly before writing the assertions:

    language="en" (the bug) -> 1 chunk (the whole 37-word block, unsplit)
    language="hi"           -> 5 chunks, split on danda (।) sentence boundaries
    language=None           -> 5 chunks, identical to "hi" — the guard's own
                                no-language path, which is what the fix takes when
                                a parser could not detect the document's language
                                rather than coercing it to English.
"""

from __future__ import annotations

import pytest

from app.services.documents.chunking import chunk_document
from app.services.documents.ir import IRBuilder


@pytest.fixture(autouse=True, scope="module")
def _ensure_punkt():
    """``_get_nltk_tokenizer`` latches ``_nltk_load_failed`` PERMANENTLY per process
    the first time it cannot load a punkt model (issue #449 — see
    ``chunking_service.py``'s own comment on why the latch is deliberate). That is a
    real hazard for this test module specifically: it asserts that ``language="en"``
    and ``language="hi"``/``None`` produce DIFFERENT splits, which is only true when
    English punkt actually loads — if an earlier test in this xdist worker already
    hit a punkt failure (a clean checkout with no cached ``nltk_data``, or a
    same-process race with another worker's download), every language would fall
    to the same regex fallback and the two arms would look identical for the wrong
    reason. Same fixture shape as ``test_chunking_scripts.py``, plus an explicit
    ``reset_sentence_splitter_state()`` — downloading the corpus does not by itself
    un-latch a failure some earlier test in this worker may have already recorded.
    """
    import nltk

    from app.services.search.chunking_service import reset_sentence_splitter_state

    nltk.download("punkt_tab", quiet=True)
    reset_sentence_splitter_state()
    yield
    reset_sentence_splitter_state()


#: Five short sentences separated by the Devanagari danda (।), a terminator English
#: punkt does not recognise. Devanagari is written WITH spaces (unlike Thai/CJK), so
#: `_split_long_block`'s own `len(block.text.split()) > target * 2` gate is reached
#: through ordinary whitespace counting — this fixture does not also need #448's
#: separate CJK/Thai word-counting fix to exercise the language-threading bug.
HINDI_PARAGRAPH = (
    "आज मौसम बहुत अच्छा है। "
    "हमने बगीचे में टहलने का फैसला किया। "
    "बच्चे बहुत खुश थे। "
    "शाम को हम सब साथ बैठकर चाय पी और बातें कीं। "
    "यह एक यादगार दिन था जिसे हम कभी नहीं भूलेंगे।"
)


def _document(language: str | None):
    builder = IRBuilder()
    builder.add(block_type="paragraph", text=HINDI_PARAGRAPH)
    return builder.build(parser="test", parser_version="0", language=language)


def test_split_long_block_threads_the_documents_language_not_a_hardcoded_en(monkeypatch):
    """Direct proof of the fix: the language argument reaching the sentence
    splitter is the document's own, never a literal ``"en"``.

    Spies on ``split_into_sentences`` at its real import location — the same
    function ``_split_long_block`` locally imports at call time — so this fails
    loudly if the hardcoded default ever comes back, independent of punkt's actual
    splitting behaviour (which the second test below covers).
    """
    calls: list[str | None] = []
    import app.services.search.chunking_service as chunking_service

    real_split = chunking_service.split_into_sentences

    def spy(text, language="en"):
        calls.append(language)
        return real_split(text, language=language)

    monkeypatch.setattr(chunking_service, "split_into_sentences", spy)

    document = _document(language="hi")
    chunk_document(document, target_words=10)

    assert calls, "split_into_sentences was never reached — the fixture no longer exercises it"
    assert all(lang == "hi" for lang in calls), (
        f"expected every call to carry the document's language 'hi', got {calls}"
    )


def test_a_long_devanagari_block_splits_on_its_own_sentence_boundaries(monkeypatch):
    """The behavioural half: a hardcoded 'en' produces ONE oversized chunk (English
    punkt finds no sentence boundary in Devanagari text); the document's real
    language — or ``None``, the guard's own no-language path — correctly splits on
    the danda terminators instead.
    """
    buggy = chunk_document(_document(language="en"), target_words=10)
    fixed = chunk_document(_document(language="hi"), target_words=10)
    unknown = chunk_document(_document(language=None), target_words=10)

    assert len(buggy) == 1, (
        "this pins the BUG's own behaviour as a control, not a desired outcome: "
        "English punkt swallows the whole Devanagari block as one 'sentence' "
        "when it is (wrongly) asked to read it"
    )
    assert len(fixed) > 1, "a real, detected language must reach the script guard and split"
    assert len(unknown) > 1, "an undetected language must take the no-language path, not 'en'"
    assert [c.text for c in fixed] == [c.text for c in unknown], (
        "None and the document's real non-Latin language must behave identically — "
        "both correctly bypass English punkt, only their names differ"
    )
    # Every chunk is still a verbatim slice of the source (the offset invariant
    # `test_document_chunking.py` pins generally) — checked here too because a
    # language-driven split path is exactly the kind of change that could silently
    # break slicing for a script this suite does not otherwise exercise.
    document = _document(language="hi")
    for chunk in fixed:
        assert document.text[chunk.char_start : chunk.char_end] == chunk.text
