"""The Q&A panel prompt must be selectable, and must survive `.format()` (#136).

A user contributed a prompt for recurring panels that answer audience questions. It is
seeded as a **system default** so anyone can pick it, rather than living in an issue.

⚠️ **The doubled braces in its JSON skeleton are load-bearing, not a typo.**
`llm_service` applies prompts with

    prompt_template.format(transcript=..., speaker_data=...)

at three call sites, so a single ``{`` opens a format placeholder. ``{transcript}`` and
``{speaker_data}`` are the intended substitutions; every LITERAL brace in the output
schema must be doubled or summarization dies with ``KeyError`` on whatever word follows
the brace — at request time, against a real provider, for every file using the prompt.

Copying the prompt out of the issue verbatim is exactly how that regresses, which is why
the test below actually formats it rather than eyeballing the text.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from typing import cast

import pytest

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


def test_the_prompt_survives_the_substitution_llm_service_performs() -> None:
    """The defect this guards: a single literal brace turns into a format placeholder."""
    from app.core.default_prompts import QA_PANEL_PROMPT

    rendered = QA_PANEL_PROMPT.format(
        transcript="SPEAKER_00: What is the roadmap?",
        speaker_data=json.dumps({"SPEAKER_00": "Ada Vance"}),
    )

    assert "SPEAKER_00: What is the roadmap?" in rendered
    assert "Ada Vance" in rendered
    # The literal skeleton must survive as single braces, i.e. it was doubled in source.
    assert '"Questions": [' in rendered
    assert '"timestamp_range"' in rendered
    # And no placeholder may be left unsubstituted.
    assert "{transcript}" not in rendered
    assert "{speaker_data}" not in rendered


def test_every_literal_brace_in_the_source_is_doubled() -> None:
    """Directly assert the property, so the failure message names the cause.

    ``.format()`` raising is the symptom; this is the diagnosis. Without it, someone
    seeing a KeyError has to work out *why* from a stack trace inside stdlib.
    """
    from app.core.default_prompts import QA_PANEL_PROMPT

    placeholders = {"{transcript}", "{speaker_data}"}
    stripped = QA_PANEL_PROMPT
    for placeholder in placeholders:
        stripped = stripped.replace(placeholder, "")
    # After removing the two real placeholders, every remaining brace must be part of a
    # doubled pair — any lone brace would be interpreted as a new placeholder.
    singles = stripped.replace("{{", "").replace("}}", "")
    assert "{" not in singles and "}" not in singles, (
        "a literal brace in QA_PANEL_PROMPT is not doubled, so str.format() will read it "
        "as a placeholder and summarization will fail with KeyError at request time"
    )


def test_the_prompt_is_seeded_as_a_selectable_system_default() -> None:
    """It has to reach the database, not just the module."""
    from app.core.default_prompts import QA_PANEL_DESCRIPTION
    from app.core.default_prompts import QA_PANEL_NAME
    from app.core.default_prompts import QA_PANEL_PROMPT
    from app.initial_data import _ensure_system_prompts

    seeded: list[dict] = []

    class _Query:
        def filter(self, *a, **kw):
            return self

        def first(self):
            return None  # nothing exists yet, so every prompt is created

    class _Session:
        def query(self, *a, **kw):
            return _Query()

        def add(self, obj):
            seeded.append(
                {
                    "name": obj.name,
                    "description": obj.description,
                    "prompt_text": obj.prompt_text,
                    "content_type": obj.content_type,
                    "is_system_default": obj.is_system_default,
                }
            )

        def flush(self):
            return None

        def commit(self):
            return None

        def rollback(self):
            return None

    # `cast` because `_Session` is a deliberate stand-in, not a Session subclass:
    # this test is about WHICH prompts get seeded, and reaching a real database to
    # answer that would make it a DB test for no gain. The cast states what the double
    # stands for; it does not widen a genuine type mismatch.
    _ensure_system_prompts(cast("Session", _Session()))

    by_type = {row["content_type"]: row for row in seeded}
    assert "qa_panel" in by_type, f"the Q&A panel prompt is not seeded: {sorted(by_type)}"
    row = by_type["qa_panel"]
    assert row["name"] == QA_PANEL_NAME
    assert row["description"] == QA_PANEL_DESCRIPTION
    assert row["prompt_text"] == QA_PANEL_PROMPT
    assert row["is_system_default"] is True

    # The pre-existing defaults must still be seeded beside it.
    assert "general" in by_type
    assert "speaker_identification" in by_type


def test_its_content_type_is_accepted_by_the_api() -> None:
    """A seeded prompt whose content_type the API rejects is unreachable.

    ``/prompts/by-content-type`` validates against an allowlist and 400s otherwise, so
    seeding without adding the type would put the prompt in the database where nothing
    could list it.
    """
    source = (
        __import__("pathlib")
        .Path(__import__("app.api.endpoints.prompts", fromlist=["prompts"]).__file__)
        .read_text(encoding="utf-8")
    )

    assert '"qa_panel"' in source, (
        "qa_panel is seeded but not in the endpoint's valid_types allowlist, so listing "
        "prompts for that content type returns 400 and the prompt is unreachable"
    )
    # speaker_identification is deliberately NOT user-selectable — it drives an internal
    # pipeline step. Assert the distinction so it is a decision, not an oversight.
    assert '"speaker_identification"' not in source.split("valid_types")[1][:200]


def test_the_three_content_type_allowlists_do_not_contradict_each_other() -> None:
    """A content type is validated in THREE places and nothing compared them.

    Adding `qa_panel` to the seed and the endpoint left the two Pydantic validators
    rejecting it, so `GET /prompts` 500'd with a ValidationError on a row the
    application itself had seeded — 8 tests went red on a prompt nobody could create
    through the API.

    The three lists were **already** inconsistent before that: the validators accept
    `speaker_identification` and the endpoint does not. That asymmetry is deliberate
    (it is an internal pipeline prompt, not a summarization style a user picks), so
    this asserts the *seeded* types are accepted everywhere rather than demanding the
    three be identical.
    """
    import pathlib as _pathlib
    import re

    from app.initial_data import _ensure_system_prompts  # noqa: F401 - import guard

    root = _pathlib.Path(__file__).resolve().parents[2] / "app"
    schema_src = (root / "schemas" / "prompt.py").read_text(encoding="utf-8")
    endpoint_src = (root / "api" / "endpoints" / "prompts.py").read_text(encoding="utf-8")
    seed_src = (root.parent / "app" / "initial_data.py").read_text(encoding="utf-8")

    seeded = set(re.findall(r'"content_type":\s*"([a-z_]+)"', seed_src))
    assert seeded, "could not parse seeded content types from initial_data.py"

    validator_blocks = schema_src.count("content_type must be one of")
    assert validator_blocks == 2, (
        f"expected 2 validator allowlists in schemas/prompt.py, found {validator_blocks} — "
        "if one was added or removed, this guard needs updating with it"
    )

    # Every seeded type must pass BOTH validators, or the API 500s reading its own data.
    for content_type in sorted(seeded):
        assert schema_src.count(f'"{content_type}"') >= 2, (
            f"'{content_type}' is seeded as a system prompt but is missing from one of the "
            "two content_type validators in schemas/prompt.py — GET /prompts will raise "
            "ValidationError on a row the app seeded itself"
        )

    # And every USER-SELECTABLE seeded type must also pass the endpoint filter.
    internal_only = {"speaker_identification"}
    for content_type in sorted(seeded - internal_only):
        assert f'"{content_type}"' in endpoint_src, (
            f"'{content_type}' is seeded and user-selectable but not in the endpoint's "
            "valid_types, so listing prompts of that type returns 400"
        )


@pytest.mark.parametrize("locale", ["en", "de", "es", "fr", "ja", "pt", "ru", "zh"])
def test_the_new_content_type_has_a_label_in_every_locale(locale: str) -> None:
    """A missing key renders the raw key string in the UI for that language."""
    import pathlib

    repo_root = pathlib.Path(__file__).resolve().parents[3]
    path = repo_root / "frontend" / "src" / "lib" / "i18n" / "locales" / f"{locale}.json"
    data = json.loads(path.read_text(encoding="utf-8"))

    label = data.get("prompts.contentTypeQaPanel")
    assert label, f"{locale}.json has no prompts.contentTypeQaPanel — the UI shows the raw key"
    assert label.strip() == label and label != "prompts.contentTypeQaPanel"
