"""AI summaries must obey the reader's redaction policy (issue #465).

The defect this pins: ``GET /api/files/{uuid}/summary`` returned
``media_file.summary_data`` verbatim. A user whose policy masks PII saw a masked
transcript beside an **unmasked abstractive summary of that same transcript**,
and the admin ``redaction.force_*`` floor never reached the surface at all.

Cached spans cannot help here — they address ``transcript_segment.text`` offsets
that exist nowhere in LLM-authored prose — so every test below drives **live
detection**, which is what the shipped path does.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.models.media import MediaFile
from app.models.prompt import UserSetting
from app.services.redaction.config import resolve_effective_config
from app.services.system_settings_service import set_setting

# The admin floor lives in SystemSettings, whose key namespace is shared state.
pytestmark = pytest.mark.xdist_group("redaction_system_settings")

SUMMARY_PATH = "/api/files/{uuid}/summary"

#: One name, in three separate sections. This is the shape that breaks a batched
#: implementation: ``en_core_web_sm`` reports each distinct PERSON once per
#: *document*, so joining these into one ``analyze()`` call yields one span and
#: leaks the name from every section after the first — while the page still shows
#: a ``[NAME]`` label somewhere and looks masked.
REPEATED_NAME = "Talia Yarrow"
PHONE = "555-867-5309"

#: ⚠️ **The sentence frame matters, and these are measured, not invented.**
#: ``en_core_web_sm`` is context-sensitive, and the default policy excludes
#: ``ORGANIZATION`` (``DEFAULT_REDACTION_PII_ENTITIES``). Measured on the shipped
#: detector:
#:
#:   "Call Talia Yarrow at 555-867-5309."  -> ORGANIZATION('Call Talia Yarrow')  [NOT masked]
#:   "Escalate to Talia Yarrow."           -> LOCATION('Talia')                  [partial]
#:   "Talia Yarrow owns the pricing model."-> LOCATION('Talia')                  [partial]
#:   "Talia Yarrow opened the meeting."    -> NAME('Talia Yarrow')               [masked]
#:
#: So a subject-first frame is used throughout. If one of these starts failing
#: after a spaCy/Presidio bump, re-measure before "fixing" the masker — the
#: model's labelling changed, not the code under test. (That NER can classify a
#: person as ORGANIZATION and thereby skip the default policy is a real, app-wide
#: property, not something this surface introduced.)
_NAME_SENTENCES = (
    f"{REPEATED_NAME} opened the meeting.",
    f"{REPEATED_NAME} presented the roadmap.",
    f"{REPEATED_NAME} asked the team to escalate the risk.",
)
_PHONE_SENTENCE = f"The number on file is {PHONE}."


def _make_file(db_session, user, summary: dict | None = None) -> MediaFile:
    media_file = MediaFile(
        user_id=user.id,
        filename="summary-redaction.wav",
        storage_path="test/summary-redaction.wav",
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


def _get_summary(client, headers, media_file) -> dict[str, Any]:
    response = client.get(SUMMARY_PATH.format(uuid=media_file.uuid), headers=headers)
    assert response.status_code == 200, response.text
    summary: dict[str, Any] = response.json()["summary_data"]
    return summary


# --------------------------------------------------------------------------- #
# The control: a redaction-disabled deployment must be byte-identical           #
# --------------------------------------------------------------------------- #


def test_a_redaction_disabled_reader_gets_the_summary_untouched(
    client, db_session, normal_user, user_token_headers
):
    """Without this control every assertion below would also pass with masking
    hardcoded on, and a deployment that never enabled redaction would silently
    start losing text from its summaries."""
    stored = {
        "bluf": _NAME_SENTENCES[0],
        "major_topics": [{"topic": "Budget", "key_points": [_PHONE_SENTENCE]}],
    }
    media_file = _make_file(db_session, normal_user, stored)

    returned = _get_summary(client, user_token_headers, media_file)

    assert returned == stored, "a redaction-disabled reader must get byte-identical JSON"


# --------------------------------------------------------------------------- #
# The defect                                                                    #
# --------------------------------------------------------------------------- #


@pytest.mark.models
def test_pii_in_the_bluf_is_masked(client, db_session, normal_user, user_token_headers):
    """The headline defect: the BLUF restated a phone number the transcript masked."""
    media_file = _make_file(
        db_session,
        normal_user,
        {"bluf": _NAME_SENTENCES[0], "brief_summary": _PHONE_SENTENCE},
    )
    _enable_redaction(db_session, normal_user)

    returned = _get_summary(client, user_token_headers, media_file)

    assert REPEATED_NAME not in returned["bluf"], f"the name survived masking: {returned['bluf']!r}"
    assert PHONE not in returned["brief_summary"], (
        f"the phone number survived masking: {returned['brief_summary']!r}"
    )


@pytest.mark.models
def test_every_section_is_masked_not_only_the_first(
    client, db_session, normal_user, user_token_headers
):
    """⚠️ The anti-batching test. Do not "optimise" detection into one call.

    spaCy reports a PERSON once per document, so a batched implementation masks
    the first occurrence and leaks the rest — while the response still contains
    a ``[NAME]`` label, so it looks masked. Measured on the snippet path when it
    was tried there: 31 of 32 leaked.
    """
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

    returned = _get_summary(client, user_token_headers, media_file)

    blob = json.dumps(returned)
    assert REPEATED_NAME not in blob, (
        f"the name leaked from at least one section — detection was batched: {blob!r}"
    )


@pytest.mark.models
def test_nested_lists_and_dicts_are_masked(client, db_session, normal_user, user_token_headers):
    """``summary_data`` is free-form JSON, so the walk must not name known fields."""
    media_file = _make_file(
        db_session,
        normal_user,
        {"custom_prompt_output": {"risks": [{"detail": _NAME_SENTENCES[2]}]}},
    )
    _enable_redaction(db_session, normal_user)

    returned = _get_summary(client, user_token_headers, media_file)

    leaf = returned["custom_prompt_output"]["risks"][0]["detail"]
    assert REPEATED_NAME not in leaf, f"a custom-prompt field was never walked: {leaf!r}"


def test_the_container_shape_survives_masking(client, db_session, normal_user, user_token_headers):
    """Masking must rewrite leaves, never restructure the document."""
    stored = {
        "bluf": "Damn, that slipped.",
        "counts": {"topics": 3, "ratio": 0.5, "flagged": True, "missing": None},
        "major_topics": [{"topic": "Budget", "key_points": ["one", "two"]}],
    }
    media_file = _make_file(db_session, normal_user, stored)
    _enable_redaction(db_session, normal_user, categories='["profanity"]')

    returned = _get_summary(client, user_token_headers, media_file)

    assert returned["counts"] == stored["counts"], "non-string leaves must pass through unchanged"
    assert len(returned["major_topics"][0]["key_points"]) == 2
    assert "Damn" not in returned["bluf"], "profanity was not masked"


def test_processing_metadata_is_not_masked(client, db_session, normal_user, user_token_headers):
    """``metadata`` is machine-generated provenance, not model prose.

    Masking it is no privacy gain and a real correctness loss — a DATE_TIME entity
    would rewrite ``created_at`` into a label and the UI would report a summary
    generated at ``[DATE]``.
    """
    metadata = {"provider": "openai", "model": "gpt-4", "created_at": "2026-08-18T12:00:00Z"}
    media_file = _make_file(
        db_session, normal_user, {"bluf": "Damn, that slipped.", "metadata": metadata}
    )
    _enable_redaction(db_session, normal_user, categories='["profanity"]')

    returned = _get_summary(client, user_token_headers, media_file)

    assert returned["metadata"] == metadata
    assert "Damn" not in returned["bluf"], "the control half of this test did not mask"


# --------------------------------------------------------------------------- #
# Policy resolution: the floor, the reader, and the cheap path                  #
# --------------------------------------------------------------------------- #


def test_the_admin_force_floor_reaches_the_summary(
    client, db_session, normal_user, user_token_headers
):
    """A reader who disabled redaction still gets the deployment's forced floor."""
    media_file = _make_file(db_session, normal_user, {"bluf": "Damn, that slipped."})
    set_setting(db_session, "redaction.force_profanity", True)
    db_session.commit()

    try:
        bluf = _get_summary(client, user_token_headers, media_file)["bluf"]
    finally:
        set_setting(db_session, "redaction.force_profanity", False)
        db_session.commit()

    assert "Damn" not in bluf, f"the admin force floor did not reach the summary: {bluf!r}"


def test_a_profanity_only_policy_never_builds_the_pii_analyzer(
    client, db_session, normal_user, user_token_headers, monkeypatch
):
    """Presidio costs 0.8-2.1 s to construct; a profanity-only reader must not pay it.

    Asserting the analyzer is never *constructed* rather than that the output is
    unmasked: the latter passes just as well if PII detection ran and found
    nothing, which is the expensive outcome this gate exists to prevent.
    """
    from app.services.redaction.detectors import pii_presidio

    calls: list[str] = []

    def _explode(*args, **kwargs):
        calls.append("pii")
        raise AssertionError("the PII analyzer was constructed for a profanity-only policy")

    # `_cached` is the one place the AnalyzerEngine is built or returned, so any
    # PII detection at all goes through it.
    monkeypatch.setattr(pii_presidio, "_cached", _explode)

    media_file = _make_file(db_session, normal_user, {"bluf": f"Damn. {_PHONE_SENTENCE}"})
    _enable_redaction(db_session, normal_user, categories='["profanity"]')

    bluf = _get_summary(client, user_token_headers, media_file)["bluf"]

    assert not calls, "the PII analyzer was constructed"
    assert "Damn" not in bluf, "profanity masking did not run"
    assert PHONE in bluf, "PII was masked under a profanity-only policy"


@pytest.mark.models
def test_a_share_recipients_own_policy_governs_not_the_owners(
    client, db_session, normal_user, other_user, user_token_headers
):
    """The subject is the REQUESTING user — the read-surface rule from #85.

    The owner has redaction off; the reader has it on. ``llm_guard`` resolves the
    owner because egress is the owner's data leaving; a read surface is not that,
    and the two differ deliberately.
    """
    import uuid as uuid_pkg

    from app.models.media import Collection
    from app.models.media import CollectionMember
    from app.models.sharing import CollectionShare

    # Sharing is collection-scoped, not per-file, so reach the reader the way the
    # product actually does: other_user's collection, shared to normal_user.
    media_file = _make_file(db_session, other_user, {"bluf": _NAME_SENTENCES[0]})
    collection = Collection(
        user_id=other_user.id,
        name=f"shared-{uuid_pkg.uuid4().hex[:8]}",
        description="summary redaction subject test",
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
    _enable_redaction(db_session, normal_user)

    owner_cfg = resolve_effective_config(db_session, other_user.id)
    assert not owner_cfg.enabled, "fixture precondition: the owner must have redaction off"

    bluf = _get_summary(client, user_token_headers, media_file)["bluf"]

    assert REPEATED_NAME not in bluf, (
        f"the owner's policy governed a read by a share recipient: {bluf!r}"
    )
