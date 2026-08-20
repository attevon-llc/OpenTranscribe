"""Tests for ``harness.answerers.RagAnswerer`` (#463).

No live stack needed for these: the constructor's hard-fail, ``describe()``'s shape, and
the "never routes rerank through ``apply_user_preferences``" contract are all checkable
without OpenSearch, Postgres, or an LLM. Driving a real ``answer_with_context`` call needs
the live stack and is explicitly out of scope for this file — see
``docs-site/docs/developer-guide/rag-evaluation.md``'s answer-quality section for the
smoke-test status.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

import pytest

from tests.eval.harness import answerers
from tests.eval.harness.answerers import RagAnswer
from tests.eval.harness.answerers import RagAnswerer


class TestConstructorHardFailsWithoutAProvider:
    def test_missing_base_url_raises(self) -> None:
        with pytest.raises(ValueError, match="base_url and model are both required"):
            RagAnswerer(
                client=None, index="x", user_id=1, session_factory=None, base_url="", model="m"
            )

    def test_missing_model_raises(self) -> None:
        with pytest.raises(ValueError, match="base_url and model are both required"):
            RagAnswerer(
                client=None,
                index="x",
                user_id=1,
                session_factory=None,
                base_url="http://localhost:5195/v1",
                model="",
            )

    def test_the_error_never_reads_as_a_declined_answer(self) -> None:
        """A caller must not be able to mistake 'never configured' for 'declined to
        answer' -- ValueError propagates, it is not swallowed into a None answer."""
        with pytest.raises(ValueError):
            RagAnswerer(
                client=None, index="x", user_id=1, session_factory=None, base_url="", model=""
            )

    def test_a_real_provider_constructs_successfully(self) -> None:
        answerer = RagAnswerer(
            client=object(),
            index="transcript_chunks",
            user_id=1,
            session_factory=lambda: None,
            base_url="http://localhost:5195/v1",
            model="gemma-4-e4b",
        )
        assert answerer.model == "gemma-4-e4b"


class TestDescribe:
    def test_records_provider_and_rerank_bypass(self) -> None:
        answerer = RagAnswerer(
            client=object(),
            index="transcript_chunks",
            user_id=1,
            session_factory=lambda: None,
            base_url="http://localhost:5195/v1",
            model="gemma-4-e4b",
            rerank_enabled=True,
        )
        description = answerer.describe()
        assert description["provider"] == {
            "base_url": "http://localhost:5195/v1",
            "model": "gemma-4-e4b",
        }
        assert description["rerank_enabled"] is True
        assert description["rerank_enabled_bypassed_apply_user_preferences"] is True
        assert description["llm_required"] is True

    def test_temperature_defaults_to_zero_for_reproducibility(self) -> None:
        answerer = RagAnswerer(
            client=object(),
            index="x",
            user_id=1,
            session_factory=lambda: None,
            base_url="http://localhost:5195/v1",
            model="gemma-4-e4b",
        )
        assert answerer.temperature == 0.0
        assert answerer.describe()["temperature"] == 0.0

    def test_rerank_enabled_false_is_recorded_exactly_as_passed(self) -> None:
        answerer = RagAnswerer(
            client=object(),
            index="x",
            user_id=1,
            session_factory=lambda: None,
            base_url="http://localhost:5195/v1",
            model="gemma-4-e4b",
            rerank_enabled=False,
        )
        assert answerer.rerank_enabled is False
        assert answerer.describe()["rerank_enabled"] is False


class TestNeverRoutesRerankThroughApplyUserPreferences:
    """The single most important correctness constraint this class documents: an
    A/B arm asking for `rerank_enabled=True` must reliably GET True, which
    `apply_user_preferences`'s one-way-narrowing AND cannot guarantee. This is a
    static guard over the SOURCE, not a mocked call — it proves the hazardous
    function is never even named in this module, which is stronger than proving
    one particular test path avoids calling it."""

    def test_apply_user_preferences_is_never_imported_or_called_in_this_module(self) -> None:
        """AST-based, not a raw string search: the docstring legitimately NAMES
        `apply_user_preferences` in prose to explain why it is avoided, and a
        naive `"apply_user_preferences" not in source` check would trip on its
        own documentation. This walks only Import/ImportFrom/Call nodes."""
        tree = ast.parse(inspect.getsource(answerers))
        imported_names: set[str] = set()
        called_names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported_names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    called_names.add(node.func.id)
                elif isinstance(node.func, ast.Attribute):
                    called_names.add(node.func.attr)
        assert "apply_user_preferences" not in imported_names, (
            "answerers.py must never IMPORT chat.settings.apply_user_preferences -- "
            "see RagAnswerer's class docstring for why"
        )
        assert "apply_user_preferences" not in called_names, (
            "answerers.py must never CALL apply_user_preferences -- "
            "see RagAnswerer's class docstring for why"
        )

    def test_rag_answerer_constructs_chatsettings_directly_in_its_own_ast(self) -> None:
        """A more specific, positive check alongside the negative one above:
        `answer_with_context`'s source constructs `ChatSettings(...)` itself
        rather than delegating to any settings-resolution helper."""
        method_source = textwrap.dedent(inspect.getsource(RagAnswerer.answer_with_context))
        tree = ast.parse(method_source)
        call_names = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "ChatSettings" in call_names


class TestRagAnswerDataclass:
    def test_carries_text_and_contexts_separately(self) -> None:
        answer = RagAnswer(
            text="the answer",
            contexts=["excerpt one", "excerpt two"],
            excerpt_ids=[1, 2],
            retrieved=5,
            reranked=2,
        )
        assert answer.text == "the answer"
        assert answer.contexts == ["excerpt one", "excerpt two"]


class TestAnswerIsATextOnlyWrapper:
    def test_answer_and_answer_with_context_are_both_present(self) -> None:
        """Structural check that the two-method split described in the class
        docstring actually exists (answer() for answer_text scoring,
        answer_with_context() for faithfulness's context requirement)."""
        assert hasattr(RagAnswerer, "answer")
        assert hasattr(RagAnswerer, "answer_with_context")
        assert RagAnswerer.answer is not RagAnswerer.answer_with_context
