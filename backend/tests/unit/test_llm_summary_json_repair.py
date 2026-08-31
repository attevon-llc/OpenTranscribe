"""``LLMService._parse_summary_response`` / ``_summarize_section`` -- recovering
malformed JSON from an AI-summary response.

**Real bug found live** on a 3-hour transcript ("Joe Rogan Experience #2219",
vllm/gemma-4-e4b): the same file, same model, summarized twice in a row --
run 1 succeeded, run 2 failed with ``Initial JSON parse failed: Expecting ':'
delimiter: line 83 column 19 (char 6413)``, followed by ``JSON repair also
failed``, surfacing a raw ``{"error": "JSON parsing failed", ...}`` card to the
user instead of a summary.

``_repair_truncated_json`` (the existing fallback) only closes brackets/strings
left open at the very END of the response -- it exists for a response cut off
at the token limit (``finish_reason="length"``). It cannot fix a MID-document
syntax break, which is a different failure shape: ``json.loads`` fails well
past the actual defect, deep inside an otherwise well-formed document, because
there is no open bracket to close.

The real transcript was checked directly (fetched via the live demo's API,
960 segments) and contains exactly three literal ``"`` characters -- all from
an ASR-transcribed aside about Lincoln being ``6'6"`` tall (a feet/inches
mark), inside a Lincoln Bedroom anecdote. Re-running that exact snippet
through the real prompt against the real model (``otfresh-demo``'s vLLM)
showed gemma-4-e4b sometimes renders it correctly escaped (``6'6\\"``) and
plausibly, on other samples, does not -- an unescaped ``"`` inside a JSON
string value desyncs the parser's in-string tracking for everything that
follows, which matches the reported error shape (a delimiter error deep into
the document, not an end-of-string error) far better than truncation would:
every live reproduction had ``finish_reason="stop"`` (natural completion),
never ``"length"``.

Fixed by adding ``json_repair`` (issue: no repair library was a dependency
yet; `requirements.txt` now pins ``json-repair==0.63.4``) as a second-tier
repair, tried when the bracket-closer returns ``None``, in both
``_parse_summary_response`` (the final/combined summary) and
``_summarize_section`` (a section summary, so an unrecovered section does not
get stitched into the combine-step prompt as a "failed to parse" placeholder).

``TestMidDocumentUnescapedQuote`` is red against the pre-fix code (git HEAD
before this change) via a `git archive` copy -- see
``test_would_have_failed_against_the_pre_fix_repair_alone``.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from app.services.llm_service import LLMConfig
from app.services.llm_service import LLMProvider
from app.services.llm_service import LLMResponse
from app.services.llm_service import LLMService

# The exact malformation class found live: a JSON string value containing a
# literal, unescaped double quote from a feet/inches measurement mentioned in
# the source transcript ("He was 6'6\" tall..."), which desyncs the parser's
# in-string tracking so the *next* member fails with "Expecting ':' delimiter"
# rather than an end-of-string error -- reproducing the reported shape.
MID_DOCUMENT_UNESCAPED_QUOTE_JSON = (
    "{\n"
    '  "bluf": "Discussion touches on presidential history, including a fun '
    'aside that Lincoln was 6\'6" tall.",\n'
    '  "brief_summary": "A wide-ranging conversation covering the Lincoln '
    'Bedroom and its history.",\n'
    '  "major_topics": [],\n'
    '  "action_items": [],\n'
    '  "key_decisions": [],\n'
    '  "follow_up_items": [],\n'
    '  "overall_sentiment": "neutral",\n'
    '  "content_type_detected": "podcast"\n'
    "}"
)

TRAILING_TRUNCATION_JSON = (
    "{\n"
    '  "bluf": "Budget overrun addressed by deferring feature releases.",\n'
    '  "brief_summary": "Team resolved a Q4 budget shortfall.",\n'
    '  "major_topics": [{"topic": "Budget review", "summary": "Engineering '
    'overspent", "key_points": ["Over by $50K'
)

UNRECOVERABLE_GARBAGE = "Sorry, I cannot help with that request."


def _service(max_tokens: int = 60000) -> LLMService:
    return LLMService(
        LLMConfig(
            provider=LLMProvider.VLLM,
            model="test-model",
            base_url="http://llm.test/v1",
            max_tokens=max_tokens,
        )
    )


class TestMidDocumentUnescapedQuote:
    """The bug: an unescaped quote breaks parsing well past where it occurs."""

    def test_the_fixture_actually_reproduces_the_reported_error_shape(self):
        """Sanity check: this is a mid-document break, not a truncation.

        `json.loads` must fail on a delimiter/structural error *before*
        end-of-string, matching "Expecting ':' delimiter: line 83 column 19
        (char 6413)" from the live incident -- not `json.JSONDecodeError`'s
        end-of-file variants ("Unterminated string starting at", "Expecting
        value" at the final character), which is what a truncated response
        produces instead.
        """
        with pytest.raises(json.JSONDecodeError) as exc_info:
            json.loads(MID_DOCUMENT_UNESCAPED_QUOTE_JSON)

        err = exc_info.value
        assert err.pos < len(MID_DOCUMENT_UNESCAPED_QUOTE_JSON) - 5, (
            "fixture must fail well before end-of-string to reproduce a "
            "mid-document break, not a truncation-shaped error"
        )

    def test_would_have_failed_against_the_pre_fix_repair_alone(self):
        """Red check: the bracket-closing repair alone cannot recover this.

        Runs the actual pre-fix `_repair_truncated_json` from git HEAD (before
        this change) against the fixture, in an isolated `git archive` copy --
        never by editing the tracked file in place. It must return `None`,
        proving the bug was real and this test is not vacuous.
        """
        repo_root = Path(__file__).resolve().parents[3]

        with tempfile.TemporaryDirectory(prefix="otredcheck_llm_json_repair_") as archive_dir_str:
            archive_dir = Path(archive_dir_str)

            tar_proc = subprocess.run(
                ["git", "archive", "HEAD", "--", "backend/app/services/llm_service.py"],
                cwd=repo_root,
                capture_output=True,
                check=True,
            )
            subprocess.run(
                ["tar", "-x", "-C", str(archive_dir)],
                input=tar_proc.stdout,
                check=True,
            )

            pre_fix_module_path = archive_dir / "backend" / "app" / "services" / "llm_service.py"
            assert pre_fix_module_path.exists()

            # Load the pre-fix module under a private name so it never shadows the
            # real `app.services.llm_service` import used by the rest of this file.
            import importlib.util

            spec = importlib.util.spec_from_file_location(
                "otredcheck_pre_fix_llm_service", pre_fix_module_path
            )
            assert spec is not None and spec.loader is not None
            pre_fix_module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = pre_fix_module
            try:
                spec.loader.exec_module(pre_fix_module)

                pre_fix_service = pre_fix_module.LLMService(
                    pre_fix_module.LLMConfig(
                        provider=pre_fix_module.LLMProvider.VLLM,
                        model="test-model",
                        base_url="http://llm.test/v1",
                        max_tokens=60000,
                    )
                )
                result = pre_fix_service._repair_truncated_json(MID_DOCUMENT_UNESCAPED_QUOTE_JSON)
            finally:
                del sys.modules[spec.name]

        assert result is None, (
            "the pre-fix bracket-closing repair recovered this fixture -- "
            "the fixture no longer reproduces the bug, or the repo's "
            "llm_service.py changed shape enough to invalidate this check"
        )

    def test_parse_summary_response_recovers_it_via_json_repair(self):
        """Green: the new json_repair fallback recovers the full structure."""
        service = _service()
        response = LLMResponse(
            content=MID_DOCUMENT_UNESCAPED_QUOTE_JSON,
            usage_tokens=123,
            finish_reason="stop",
        )

        result = service._parse_summary_response(response, transcript_length=205684)

        assert "error" not in result
        assert result["overall_sentiment"] == "neutral"
        assert result["content_type_detected"] == "podcast"
        assert "6'6" in result["bluf"]
        assert result["metadata"]["json_repaired"] is True

    def test_summarize_section_recovers_it_via_json_repair(self, monkeypatch):
        """The section-summary path gets the same recovery, not a placeholder."""
        service = _service()
        monkeypatch.setattr(
            service,
            "chat_completion",
            lambda *a, **k: LLMResponse(
                content=MID_DOCUMENT_UNESCAPED_QUOTE_JSON,
                usage_tokens=42,
                finish_reason="stop",
            ),
        )

        result = service._summarize_section(
            chunk="irrelevant, chat_completion is stubbed",
            section_num=1,
            total_sections=2,
            speaker_data=None,
            prompt_template="{transcript}{speaker_data}",
        )

        assert "6'6" in result["bluf"]
        assert result.get("key_points") != ["Section 1: Failed to parse structured summary"]


class TestTrailingTruncationStillHandledFirst:
    """Control: the original bracket-closing repair path is untouched."""

    def test_a_genuinely_truncated_response_is_still_repaired_by_the_bracket_closer(
        self,
    ):
        service = _service()
        response = LLMResponse(
            content=TRAILING_TRUNCATION_JSON,
            usage_tokens=99,
            finish_reason="length",
        )

        result = service._parse_summary_response(response, transcript_length=1000)

        assert "error" not in result
        assert result["bluf"].startswith("Budget overrun")
        assert result["metadata"]["json_repaired"] is True


class TestUnrecoverableContentStillFailsGracefully:
    """`json_repair` never raises, but garbage in must not become garbage out."""

    def test_non_json_content_still_returns_the_error_structure(self):
        service = _service()
        response = LLMResponse(
            content=UNRECOVERABLE_GARBAGE,
            usage_tokens=10,
            finish_reason="stop",
        )

        result = service._parse_summary_response(response, transcript_length=50)

        assert result["error"] == "JSON parsing failed"
        assert result["raw_response_preview"] == UNRECOVERABLE_GARBAGE
