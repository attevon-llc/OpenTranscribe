"""Behavioural check for base rule 11 ("answer in the question's language") — #453/ML1.

``test_chat_answer_language.py`` asserts the RULE TEXT is in the prompt and says outright
that whether a model *obeys* it "is a measurement question for the eval harness (#453 step
2), not something a unit test can settle." This file is that measurement, run directly
against a local model rather than through the eval harness — the harness needs the
research-licensed ``trec_eval`` dependency and a settled OpenSearch index (see
``backend/tests/CLAUDE.md``'s "``tests/eval/`` — the RAG evaluation harness" section); this
needs neither. One real, temperature-0 chat completion is enough to show the rule has an
effect, which is the gap being closed here, not a full per-language sweep (that belongs to
the eval harness, per that file's own docstring).

Skips (never fails) when nothing answers on ``localhost:$LLM_TEST_PORT`` (default 5195) —
the identical TCP-probe-and-skip pattern ``tests/eval/test_eval_faithfulness_judge.py`` uses for
the same class of dependency, so CI (which has no such server) reports these as skipped
rather than red, and a dev machine with ``--with-llm-test`` (or the project's local vLLM at
:5195) exercises them for real.

The language check below is a **coarse heuristic** (Spanish vs. English function-word
counts), not a language classifier: pulling in a language-detection dependency for one
behavioural smoke test is a worse trade than a slightly noisy heuristic, and temperature 0
plus a deliberately unambiguous Spanish question leaves little room for a false negative —
gemma-4-e4b either answers with a preponderance of Spanish function words or it doesn't.
"""

from __future__ import annotations

import os
import re
import socket

import pytest

from app.core.config import settings as app_settings
from app.services.chat.prompting import build_messages
from app.services.chat.prompting import build_system_prompt
from app.services.chat.redactor import MaskedChunk
from app.services.llm_service import LLMConfig
from app.services.llm_service import LLMProvider
from app.services.llm_service import LLMService
from app.services.search.chunk_retrieval import ChunkHit

#: Same variable `tests/eval/test_eval_faithfulness_judge.py` reads, so this follows a
#: `--fresh ... --port-offset N` LLM-test stack instead of always asking about whichever
#: stack happens to own the base port.
_LLM_TEST_PORT = os.environ.get("LLM_TEST_PORT", "5195")
_LLM_TEST_BASE_URL = f"http://localhost:{_LLM_TEST_PORT}/v1"


def _vllm_reachable() -> bool:
    try:
        with socket.create_connection(("localhost", int(_LLM_TEST_PORT)), timeout=0.3):
            return True
    except OSError:
        return False


pytestmark = pytest.mark.skipif(
    not _vllm_reachable(),
    reason=f"needs a reachable OpenAI-compatible server on localhost:{_LLM_TEST_PORT}",
)

#: Spanish function words: high frequency, essentially absent from an English answer of any
#: length. Word-boundary + case-insensitive matched. Deliberately a LONG list — a short one
#: (tried first: ~20 entries) produced a false negative on a real, correctly-Spanish answer
#: that happened to be terse ('Jordan dijo textualmente: "..."') and used none of the 20. A
#: coarse heuristic's failure mode is exactly this: not "the model got it wrong" but "the
#: detector didn't recognise it", and the fix is a bigger dictionary, not a looser assertion.
_SPANISH_MARKERS = (
    "el",
    "la",
    "los",
    "las",
    "que",
    "de",
    "en",
    "es",
    "por",
    "para",
    "con",
    "una",
    "un",
    "del",
    "según",
    "está",
    "esta",
    "esto",
    "dice",
    "dijo",
    "dijeron",
    "indicó",
    "indica",
    "señaló",
    "afirmó",
    "comentó",
    "explicó",
    "mencionó",
    "textualmente",
    "sobre",
    "fue",
    "fueron",
    "fue aprobado",
    "aprobado",
    "aprobó",
    "y",
    "fue de",
    "fue el",
    "próximo",
    "trimestre",
    "presupuesto",
    "fue el presupuesto",
)
#: Spanish-specific characters absent from English by construction — a second, orthogonal
#: signal from the same text, not just more words. Any of these appearing is unambiguous.
_SPANISH_CHARS = re.compile(r"[ñáéíóúÑÁÉÍÓÚ¿¡]")

_ENGLISH_MARKERS = (
    "the",
    "is",
    "and",
    "of",
    "to",
    "in",
    "that",
    "was",
    "for",
    "with",
    "according",
    "budget",
    "quarter",
)


def _marker_count(text: str, markers: tuple[str, ...]) -> int:
    lowered = text.lower()
    return sum(len(re.findall(rf"\b{re.escape(marker)}\b", lowered)) for marker in markers)


def _has_spanish_signal(text: str) -> bool:
    """True if the text carries ANY Spanish signal: a marker word or a Spanish-only
    character. Two independent signals rather than one list, because a single word
    list can always miss a real Spanish sentence that happens not to use any of its
    entries (see the list's own docstring above)."""
    return _marker_count(text, _SPANISH_MARKERS) > 0 or bool(_SPANISH_CHARS.search(text))


def _vllm_config() -> LLMConfig:
    return LLMConfig(
        provider=LLMProvider.VLLM,
        model="gemma-4-e4b",
        base_url=_LLM_TEST_BASE_URL,
        api_key=None,
        temperature=0.0,
        max_tokens=8192,
    )


def _english_excerpt_chunk() -> MaskedChunk:
    """One English transcript excerpt — the answer's language has to come from the
    RULE, not from copying whatever language the source material happens to be in."""
    hit = ChunkHit(
        file_uuid="11111111-1111-1111-1111-111111111111",
        file_id=1,
        chunk_index=0,
        content="",
        title="Quarterly Budget Review",
        speaker="Jordan",
        start_time=0.0,
        end_time=12.0,
        language="en",
    )
    content = (
        "Jordan: The marketing budget for next quarter is forty thousand dollars, "
        "approved by finance on Tuesday."
    )
    return MaskedChunk(source=hit, content=content, was_masked=False)


class TestBaseRule11AnswersInTheQuestionsLanguage:
    """Real generation against gemma-4-e4b, temperature 0.

    Deliberately not parametrized over languages: one clean discriminating case (a
    Spanish question over an English excerpt, with nothing Spanish anywhere in the
    prompt except base rule 11 itself) is what settles whether the rule has any
    effect at all on a real model. A broader per-language sweep is the eval
    harness's job (#453 step 2), not this smoke test's.
    """

    def test_a_spanish_question_is_answered_in_spanish(self, monkeypatch) -> None:
        # `localhost` is a private/loopback target; the SSRF guard on the LLM outbound
        # path refuses it unless the operator has opted in (issue #444/A0.1). This is
        # the same opt-in the LLM settings "Test connection" tests use.
        monkeypatch.setattr(app_settings, "LLM_ALLOW_PRIVATE_ENDPOINTS", True)

        system_prompt = build_system_prompt(use_context=True)
        assert "same language" in system_prompt.lower(), (
            "guard: base rule 11 is not present in the prompt this test exercises — "
            "see test_chat_answer_language.py's test_the_answer_rules_name_the_users_language"
        )

        messages, excerpt_ids = build_messages(
            system_prompt=system_prompt,
            chunks=[_english_excerpt_chunk()],
            history=[],
            question="¿Cuál es el presupuesto de marketing para el próximo trimestre?",
            context_window=8192,
            response_tokens=512,
        )
        assert excerpt_ids, "guard: the excerpt did not reach the prompt at all"

        service = LLMService(_vllm_config())
        response = service.chat_completion(messages)

        spanish_hits = _marker_count(response.content, _SPANISH_MARKERS)
        english_hits = _marker_count(response.content, _ENGLISH_MARKERS)

        assert spanish_hits > english_hits, (
            "base rule 11 did not hold for gemma-4-e4b: the Spanish question was not "
            f"answered in Spanish (spanish_hits={spanish_hits}, english_hits={english_hits}) "
            f"in: {response.content!r}"
        )

    def test_a_quote_is_reproduced_verbatim_not_translated(self, monkeypatch) -> None:
        """The sibling half of rule 11: answer in the question's language, but a
        quoted excerpt stays in its ORIGINAL language. Measured, not asserted from
        prompt text — a translated quote is no longer evidence of what was said.
        """
        monkeypatch.setattr(app_settings, "LLM_ALLOW_PRIVATE_ENDPOINTS", True)

        system_prompt = build_system_prompt(use_context=True)
        assert "do not translate" in system_prompt.lower() or (
            "not translate the quotation" in system_prompt.lower()
        ), "guard: the no-translate-quotes clause is not in the prompt this test exercises"

        messages, excerpt_ids = build_messages(
            system_prompt=system_prompt,
            chunks=[_english_excerpt_chunk()],
            history=[],
            question=(
                "Cita textualmente lo que dijo Jordan sobre el presupuesto, y responde en español."
            ),
            context_window=8192,
            response_tokens=512,
        )
        assert excerpt_ids, "guard: the excerpt did not reach the prompt at all"

        service = LLMService(_vllm_config())
        response = service.chat_completion(messages)

        # The dollar figure is distinctive enough not to appear by coincidence, and a
        # translation would render it as "cuarenta mil dólares" instead. Matched with a
        # little tolerance for surface form ("forty thousand dollars" vs "$40,000") —
        # vLLM's continuous batching can very rarely perturb wording even at
        # temperature 0 (a live-model property, not a flaky assertion), so the check
        # is on the untranslated NUMBER surviving, not one exact phrasing of it.
        quote_preserved = re.search(
            r"forty\s+thousand|\$\s?40,?000|40,000\s*dollars", response.content, re.IGNORECASE
        )
        assert quote_preserved, (
            "the quoted excerpt was translated (or dropped) rather than reproduced "
            f"verbatim: {response.content!r}"
        )
        # And the surrounding prose is still Spanish — rule 11 and the no-translate
        # clause both hold in the SAME answer, which is the case that broke before
        # #453 (see the module docstring on `chat/prompting.py`'s quote exemption).
        # Even a terse answer that is little more than "Jordan dijo textualmente:"
        # carries a Spanish signal (a marker word or an accented/Spanish-only
        # character) — `_has_spanish_signal` is the two-signal check for that.
        assert _has_spanish_signal(response.content), (
            f"no Spanish surrounding prose at all: {response.content!r}"
        )
