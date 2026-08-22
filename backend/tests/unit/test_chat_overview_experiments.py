"""#532 synthesis-gap EXPERIMENT arms — delete this file with the flags it tests.

The measured defect (issue #532): retrieval OFFERS 99% of a multi-file scope, the
answer cites 75%, and the worst observed turn cited one excerpt for every claim
while holding an overview of all four recordings. Three one-variable arms:

* **(a) citable overview** — the overview's listed recordings carry citation ids
  above the excerpt id space, so base rule 12 ("answer from the overview") stops
  fighting rule 2 ("cite what you use").
* **(b) attached rule** — the anti-narrowing rule rides ON the overview block,
  the same per-block treatment rules 10/11 got, instead of living thirteen rules
  away in the base prompt.
* **(c) position** — the overview moves AFTER the excerpts (the input-order /
  primacy arm from the multi-document-summarization literature).

Every "arm on" test here has an "arm off" control asserting today's shipped
behaviour, because the arms must move exactly one variable each.
"""

from __future__ import annotations

import pytest

from app.services.chat.citations import build_overview_citations
from app.services.chat.mapreduce import build_overview
from app.services.chat.mapreduce.file_summaries import FileSummary
from app.services.chat.prompting import _OVERVIEW_ATTACHED_RULE
from app.services.chat.prompting import build_messages
from app.services.chat.prompting import build_system_prompt
from app.services.chat.redactor import MaskedChunk
from app.services.search.chunk_retrieval import ChunkHit

pytestmark = pytest.mark.unit


def _chunk(content: str, *, index: int = 0) -> MaskedChunk:
    return MaskedChunk(
        source=ChunkHit(
            file_uuid="11111111-1111-1111-1111-111111111111",
            file_id=1,
            chunk_index=index,
            content=content,
            title="Standup",
            speaker="Dana",
            start_time=75.0,
            end_time=105.0,
        ),
        content=content,
    )


def _summary(n: int, **kwargs) -> FileSummary:
    defaults = {
        "file_uuid": f"uuid-{n}",
        "title": f"Weekly sync {n}",
        "recorded_at": f"2025-03-{n % 27 + 1:02d}",
        "digest": f"We discussed item {n}.",
    }
    return FileSummary(**{**defaults, **kwargs})


def _messages(**kwargs) -> str:
    """Assemble one turn and return the final user message's content."""
    messages, _ = build_messages(
        system_prompt=build_system_prompt(use_context=True),
        chunks=kwargs.pop("chunks", [_chunk("Alice said the budget is fine.")]),
        history=[],
        question="what happened?",
        context_window=60000,
        response_tokens=1000,
        overview_block="<overview>\nrecordings: 2\n</overview>\n",
        **kwargs,
    )
    return messages[-1]["content"]


# ------------------------------------------------------------- arm (c) position


class TestOverviewPosition:
    def test_control_overview_precedes_the_excerpts(self):
        content = _messages()
        assert content.index("<overview>") < content.index("<excerpt")

    def test_arm_moves_the_overview_after_the_excerpts(self):
        content = _messages(overview_after_excerpts=True)
        assert content.index("<overview>") > content.index("<excerpt")
        # Still before the question — evidence, not an afterthought.
        assert content.index("<overview>") < content.rindex("what happened?")

    def test_arm_with_no_excerpts_still_carries_the_overview(self):
        content = _messages(overview_after_excerpts=True, chunks=[])
        assert "<overview>" in content


# ------------------------------------------------------- arm (b) attached rule


class TestOverviewAttachedRule:
    def test_control_carries_no_attached_rule(self):
        assert _OVERVIEW_ATTACHED_RULE not in _messages()

    def test_arm_attaches_the_rule_directly_after_the_block(self):
        content = _messages(overview_block_rule=True)
        overview_end = content.index("</overview>")
        rule_at = content.index(_OVERVIEW_ATTACHED_RULE.strip())
        assert rule_at > overview_end
        # Adjacent to the block, not floating at the end of the prompt.
        assert rule_at - overview_end < len("</overview>") + 4

    def test_no_overview_means_no_rule(self):
        """A rule pointing at a block that is not there would be worse than no
        rule — the exact defect #536 records for the recurrence vocabulary."""
        messages, _ = build_messages(
            system_prompt=build_system_prompt(use_context=True),
            chunks=[_chunk("text")],
            history=[],
            question="q?",
            context_window=60000,
            response_tokens=1000,
            overview_block="",
            overview_block_rule=True,
        )
        assert _OVERVIEW_ATTACHED_RULE not in messages[-1]["content"]


# ----------------------------------------------------- arm (a) citable overview


class TestCitableOverview:
    def test_control_renders_no_ids_and_no_cited_entries(self):
        overview = build_overview("q", [_summary(1), _summary(2)])
        assert overview.cited_entries == ()
        assert "[" not in overview.block.replace("[PERSON", "")  # no id markers

    def test_arm_numbers_entries_above_the_given_start(self):
        overview = build_overview("q", [_summary(1), _summary(2)], citation_start=40)
        assert overview.cited_entries == ((41, "uuid-1"), (42, "uuid-2"))
        assert "[41]" in overview.block
        assert "[42]" in overview.block

    def test_arm_forces_the_deterministic_reducer_even_when_llm_offered(self):
        """An LLM condensation could drop or renumber an entry, detaching an id
        from its file — ids demand the code path."""

        class _ExplodingLLM:
            def __getattr__(self, name):  # pragma: no cover - the assertion is no call
                raise AssertionError("the LLM reducer must not run for a citable overview")

        overview = build_overview(
            "q", [_summary(1)], llm=_ExplodingLLM(), use_llm=True, citation_start=10
        )
        assert overview.reducer == "code"
        assert overview.cited_entries == ((11, "uuid-1"),)

    def test_payloads_carry_digest_kind_uuid_and_masked_snippet(self):
        summaries = [_summary(1), _summary(2)]
        overview = build_overview("q", summaries, citation_start=12)
        payloads = build_overview_citations(overview.cited_entries, summaries)
        assert [p["id"] for p in payloads] == [13, 14]
        assert all(p["kind"] == "digest" for p in payloads)
        assert payloads[0]["file_uuid"] == "uuid-1"
        assert payloads[0]["snippet"] == "We discussed item 1."
        # An overview entry cites the whole (masked) digest, not one indexed
        # section — both locators are deliberately absent.
        assert payloads[0]["chunk_index"] is None
        assert payloads[0]["digest_section"] is None

    def test_an_entry_whose_summary_vanished_is_skipped_not_cited_empty(self):
        summaries = [_summary(1)]
        payloads = build_overview_citations(((5, "uuid-1"), (6, "uuid-gone")), summaries)
        assert [p["id"] for p in payloads] == [5]
