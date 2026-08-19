"""Chat must answer in the user's language and must not translate the query (#453 stage 4).

Two prompts were English-only, and the failure compounds across a turn:

1. ``BASE_SYSTEM_RULES`` never told the model what language to answer in. A model
   given English instructions and English rules will usually answer a Spanish
   question in English.
2. ``_REWRITE_SYSTEM`` is English prose telling the model to rewrite a follow-up
   into a standalone query. A model following English instructions tends to produce
   an English query — and that query is then matched against **transcripts in their
   own language**, so it searches for words nobody said. The retrieval failure
   happens before the answer prompt is ever reached.

⚠️ **Quotes must NOT be translated even when the answer is.** A translated quotation
is no longer evidence of what was said — it is the model's paraphrase wearing quote
marks, and this product's whole claim is that a citation is checkable against the
recording.

These are prompt-content assertions, which is the honest scope: whether a given model
*obeys* the rule is a measurement question for the eval harness (#453 step 2), not
something a unit test can settle. What a unit test CAN pin is that the instruction is
present at all — and its absence was the entire defect.
"""

from __future__ import annotations

import pytest


def test_the_answer_rules_name_the_users_language() -> None:
    """Rule absent means the model defaults to the language of its instructions."""
    from app.services.chat.prompting import BASE_SYSTEM_RULES

    lowered = BASE_SYSTEM_RULES.lower()
    assert "same language" in lowered, (
        "BASE_SYSTEM_RULES does not tell the model which language to answer in, so a "
        "Spanish question gets an English answer from a model reading English rules"
    )


def test_quotes_are_exempted_from_translation() -> None:
    """The subtle half: answer in their language, quote in the original.

    Without this the rule above makes things worse — a model told to answer in
    Spanish will helpfully translate an English quotation, and the citation stops
    being evidence.
    """
    from app.services.chat.prompting import BASE_SYSTEM_RULES

    lowered = BASE_SYSTEM_RULES.lower()
    assert "do not translate" in lowered or "not translate the quotation" in lowered, (
        "nothing stops the model translating a quoted excerpt; a translated quote is a "
        "paraphrase wearing quote marks, and citations are meant to be checkable"
    )


def test_the_rules_stay_numbered_contiguously() -> None:
    """A duplicated or skipped number is how a rule gets silently dropped.

    The language rule was inserted mid-list, which renumbered everything after it.
    Models do follow numbered lists loosely, but a list with two rule 11s reads as
    an editing mistake and is a real signal that an edit went wrong.
    """
    import re

    from app.services.chat.prompting import BASE_SYSTEM_RULES

    numbers = [int(m) for m in re.findall(r"^(\d+)\. ", BASE_SYSTEM_RULES, re.MULTILINE)]

    assert numbers, "no numbered rules found — the prompt format changed"
    assert numbers == list(range(1, len(numbers) + 1)), (
        f"rule numbers are not contiguous from 1: {numbers}"
    )


def test_the_rewriter_is_told_not_to_translate() -> None:
    """The failure that happens BEFORE the answer prompt is reached.

    An English-instructed rewriter turns "¿qué dijo Ana sobre el presupuesto?" into
    an English query, which is then matched against Spanish transcripts. Retrieval
    returns nothing relevant and the answer prompt never gets a chance.
    """
    from app.services.chat.query_rewriter import _REWRITE_SYSTEM

    lowered = _REWRITE_SYSTEM.lower()
    assert "same language" in lowered, (
        "the rewriter does not preserve the question's language, so a non-English "
        "follow-up is rewritten into English and searched against transcripts that "
        "never contained those words"
    )
    assert "never translate" in lowered or "not translate" in lowered


@pytest.mark.parametrize(
    "constant_path",
    [
        "app.services.chat.prompting.BASE_SYSTEM_RULES",
        "app.services.chat.query_rewriter._REWRITE_SYSTEM",
    ],
)
def test_the_prompts_are_still_non_empty_prose(constant_path: str) -> None:
    """Guard the guard: the assertions above pass vacuously on an empty string.

    Every check here is a substring test, and ``"" in ""`` is False — but a prompt
    reduced to a stub would fail them loudly for the wrong reason, and a prompt
    rebuilt as a list would fail them silently. Pin the shape.
    """
    import importlib

    module_path, _, name = constant_path.rpartition(".")
    value = getattr(importlib.import_module(module_path), name)

    assert isinstance(value, str), f"{constant_path} is no longer a string: {type(value)}"
    assert len(value) > 200, f"{constant_path} is suspiciously short ({len(value)} chars)"
