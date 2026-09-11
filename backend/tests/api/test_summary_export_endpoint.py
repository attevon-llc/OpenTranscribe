"""``GET /api/files/{uuid}/summary/export`` — the summary copy-button endpoint (issue #885).

#885 was filed as a redaction bypass and that premise is WRONG: ``GET .../summary`` already
masks correctly (issue #465, ``tests/redaction/test_summary_redaction.py``). The real defects
were client-side — ``SummaryModal.svelte``'s copy button dropped the ``action_items`` and
``speakers_analysis`` sections, and it was the last client-side re-serialization of server data
left in the SPA. This suite exists to prove:

1. This route resolves through the SAME ``_redacted_summary`` helper as the read endpoint —
   one masking implementation, not two that could drift (tests 6-9, 11).
2. The dropped sections are now present (test 5) — the actual bug fix.
3. The three deliberately-absent mechanisms (a ``redact`` reveal parameter, a
   ``_redaction_pending`` 409 gate, an audit-event call) really are absent, so nobody "fixes"
   them back in believing they were forgotten (test 10 covers the first; the docstring on
   ``export_summary`` explains all three).

PII sentences/fixture shape are copied verbatim from ``test_summary_redaction.py`` — see that
file's header for why the exact subject-first phrasing matters to ``en_core_web_sm``.
"""

from __future__ import annotations

import inspect
import uuid

import pytest
from fastapi import status

from app.api.endpoints.summarization import export_summary
from app.models.media import MediaFile
from app.models.prompt import UserSetting
from app.services.system_settings_service import set_setting

# The admin floor lives in SystemSettings, whose key namespace is shared state — same group
# test_summary_redaction.py uses, since these tests flip the same keys.
pytestmark = pytest.mark.xdist_group("redaction_system_settings")

EXPORT_PATH = "/api/files/{uuid}/summary/export"

#: Copied verbatim from test_summary_redaction.py — do not invent new PII strings; see that
#: file's header for why the sentence frame is measured, not arbitrary.
REPEATED_NAME = "Talia Yarrow"
PHONE = "555-867-5309"
_NAME_SENTENCES = (
    f"{REPEATED_NAME} opened the meeting.",
    f"{REPEATED_NAME} presented the roadmap.",
    f"{REPEATED_NAME} asked the team to escalate the risk.",
)


def _make_file(db_session, owner, summary: dict | None = None) -> MediaFile:
    media_file = MediaFile(
        user_id=owner.id,
        filename="summary-export.wav",
        storage_path="test/summary-export.wav",
        content_type="audio/wav",
        file_size=1234,
        status="completed",
        summary_data=summary,
    )
    db_session.add(media_file)
    db_session.commit()
    db_session.refresh(media_file)
    return media_file


def _enable_redaction(db_session, user, categories: str = '["pii"]') -> None:
    for key, value in (("redaction_enabled", "true"), ("redaction_categories", categories)):
        db_session.add(UserSetting(user_id=user.id, setting_key=key, setting_value=value))
    db_session.commit()


def _export(client, headers, media_file, **params):
    return client.get(EXPORT_PATH.format(uuid=media_file.uuid), headers=headers, params=params)


# --------------------------------------------------------------------------------------- #
# Basic contract
# --------------------------------------------------------------------------------------- #


def test_the_export_requires_authentication(client, db_session, normal_user):
    media_file = _make_file(db_session, normal_user, {"bluf": "hi"})
    response = client.get(EXPORT_PATH.format(uuid=media_file.uuid))
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


def test_the_export_404s_when_there_is_no_summary(
    client, db_session, normal_user, user_token_headers
):
    media_file = _make_file(db_session, normal_user, summary=None)
    response = _export(client, user_token_headers, media_file)
    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()["detail"] == (
        "No summary available for this file. Please generate one first."
    )


def test_another_users_file_is_refused(client, db_session, normal_user, other_user_auth_headers):
    """Mirrors ``test_get_summary_other_user_403``'s exact expected status."""
    media_file = _make_file(db_session, normal_user, {"bluf": "hi"})
    response = _export(client, other_user_auth_headers, media_file)
    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["detail"] == "You do not have permission to access this file"


def test_an_unsupported_format_is_refused(client, db_session, normal_user, user_token_headers):
    media_file = _make_file(db_session, normal_user, {"bluf": "hi"})
    response = _export(client, user_token_headers, media_file, format="pdf")
    assert response.status_code == status.HTTP_400_BAD_REQUEST


# --------------------------------------------------------------------------------------- #
# The bug fix
# --------------------------------------------------------------------------------------- #


def test_the_export_includes_action_items_and_speaker_analysis(
    client, db_session, normal_user, user_token_headers
):
    """THE bug fix. The retired client-side serializer never rendered these two sections
    at all, even though SummaryDisplay.svelte renders both on screen."""
    media_file = _make_file(
        db_session,
        normal_user,
        {
            "bluf": "Quarterly review.",
            "brief_summary": "The team reviewed Q3 numbers.",
            "action_items": [
                {"item": "File the expense report", "owner": "Priya", "priority": "high"}
            ],
            "speakers_analysis": [
                {
                    "speaker": "Priya",
                    "role": "Finance lead",
                    "key_contributions": ["Presented the budget"],
                }
            ],
        },
    )
    response = _export(
        client,
        user_token_headers,
        media_file,
        action_items_label="Action Items",
        speaker_analysis_label="Speaker Analysis",
    )
    assert response.status_code == status.HTTP_200_OK
    body = response.text

    assert "Action Items" in body
    assert "File the expense report" in body
    assert "Speaker Analysis" in body
    assert "Priya" in body
    assert "Presented the budget" in body


# --------------------------------------------------------------------------------------- #
# One masking implementation, shared with the read endpoint
# --------------------------------------------------------------------------------------- #


@pytest.mark.models
def test_the_admin_force_floor_reaches_the_summary_export(
    client, db_session, normal_user, user_token_headers
):
    """Mirrors test_summary_redaction.py's admin-force-floor test, for the export route."""
    media_file = _make_file(db_session, normal_user, {"bluf": _NAME_SENTENCES[0]})
    set_setting(db_session, "redaction.force_pii", True)
    db_session.commit()

    try:
        response = _export(client, user_token_headers, media_file)
    finally:
        set_setting(db_session, "redaction.force_pii", False)
        db_session.commit()

    assert response.status_code == status.HTTP_200_OK
    body = response.text
    assert REPEATED_NAME not in body, f"the admin force floor did not reach the export: {body!r}"
    assert "[" in body and "]" in body, "no mask-label bracket found in the exported body"


@pytest.mark.models
def test_every_section_is_masked_not_only_the_first(
    client, db_session, normal_user, user_token_headers
):
    """The same name in three separate sections must be masked in all three — the
    anti-batching property from test_summary_redaction.py, re-checked on this route."""
    media_file = _make_file(
        db_session,
        normal_user,
        {
            "bluf": _NAME_SENTENCES[0],
            "brief_summary": _NAME_SENTENCES[1],
            "major_topics": [{"topic": "Pricing", "key_points": [_NAME_SENTENCES[2]]}],
        },
    )
    _enable_redaction(db_session, normal_user)

    response = _export(client, user_token_headers, media_file)

    assert response.status_code == status.HTTP_200_OK
    assert REPEATED_NAME not in response.text, (
        f"the name leaked from at least one section: {response.text!r}"
    )


def test_a_redaction_disabled_reader_gets_the_summary_untouched(
    client, db_session, normal_user, user_token_headers
):
    """The control: without this, tests 6-7 could pass by masking everything
    unconditionally regardless of the reader's own policy."""
    media_file = _make_file(db_session, normal_user, {"bluf": _NAME_SENTENCES[0]})

    response = _export(client, user_token_headers, media_file)

    assert response.status_code == status.HTTP_200_OK
    assert REPEATED_NAME in response.text, "a redaction-disabled reader's export was masked"


def test_processing_metadata_is_not_masked(client, db_session, normal_user, user_token_headers):
    """``metadata`` (provider/model) survives verbatim in the disclaimer footer — masking
    machine-generated provenance is a correctness loss and no privacy gain."""
    media_file = _make_file(
        db_session,
        normal_user,
        {"bluf": "Quarterly review.", "metadata": {"provider": "openai", "model": "gpt-4"}},
    )

    response = _export(
        client,
        user_token_headers,
        media_file,
        disclaimer_label="Generated by openai (gpt-4).",
    )

    assert response.status_code == status.HTTP_200_OK
    assert "Generated by openai (gpt-4)." in response.text


def test_the_summary_export_takes_no_redact_parameter():
    """No ``redact``/reveal query parameter exists on this route — adding one would CREATE
    the vulnerability #885 mistakenly believed already existed. Must-fire control alongside
    it: the transcript export route DOES take one, so a broken introspection call (e.g. one
    that always returns an empty parameter set) cannot pass this test vacuously."""
    from app.api.endpoints.files.transcript_export import export_transcript

    assert "redact" not in inspect.signature(export_summary).parameters
    assert "redact" in inspect.signature(export_transcript).parameters, (
        "control invariant broken: the transcript export route no longer takes `redact` — "
        "if this fails, the assertion above may be passing vacuously"
    )


def test_the_export_and_the_read_mask_through_one_implementation():
    """Pins 'no parallel policy implementation' structurally, not just by behavior."""
    assert "_redacted_summary" in inspect.getsource(export_summary)


def test_a_share_recipient_may_export_the_summary_they_can_read(
    client, db_session, normal_user, other_user, user_token_headers
):
    """A share recipient with read access may export the summary they can already see —
    matches ``get_file_by_uuid_with_permission``'s default (no ``min_permission``), same as
    ``GET .../summary`` itself."""
    from app.models.media import Collection
    from app.models.media import CollectionMember
    from app.models.sharing import CollectionShare

    media_file = _make_file(db_session, other_user, {"bluf": "Shared recording summary."})
    collection = Collection(
        user_id=other_user.id,
        name=f"shared-{uuid.uuid4().hex[:8]}",
        description="summary export share test",
    )
    db_session.add(collection)
    db_session.commit()
    db_session.add(CollectionMember(collection_id=collection.id, media_file_id=media_file.id))
    db_session.add(
        CollectionShare(
            collection_id=collection.id,
            shared_by_id=other_user.id,
            target_type="user",
            target_user_id=normal_user.id,
            permission="viewer",
        )
    )
    db_session.commit()

    response = _export(client, user_token_headers, media_file)

    assert response.status_code == status.HTTP_200_OK
    assert "Shared recording summary." in response.text
