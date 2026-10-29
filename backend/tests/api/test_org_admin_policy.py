"""``/api/org-admin/policy`` over HTTP: an organization's privacy policy and redaction floor.

The route changes what every member's recordings do (voiceprints, inferred gender, masked text),
so the cases that matter are the boundaries: who may call (an organization administrator, behind
a capability no superuser can bypass), what an administrator can reach (their own organization,
and nothing else: there is no organization parameter), what is persisted and refused, what the
audit trail may contain (field names, never a custom word), and that the stored floor actually
masks a real export.
"""

from __future__ import annotations

import uuid as uuid_pkg

import pytest
from fastapi import status

from app.auth.audit import AuditEventType
from app.auth.audit import audit_logger
from app.core import constants
from app.core.capabilities import COMMUNITY_CAPABILITIES
from app.core.capabilities import reset_capability_resolver
from app.core.capabilities import set_capability_resolver
from app.core.tenant_limits import RedactionFloor
from app.core.tenant_limits import reset_resolvers
from app.core.tenant_limits import set_redaction_floor_resolver
from app.models.media import MediaFile
from app.models.media import TranscriptSegment
from app.models.organization import Organization
from app.models.organization import OrganizationMembership
from app.models.organization_policy import OrganizationPolicy
from app.models.prompt import UserSetting
from tests.fixtures.privacy_switches import upsert_system_setting

POLICY = "/api/org-admin/policy"
SECRET_WORD = "Project-Nightingale"


def _capabilities(**overrides: bool):
    caps = {**COMMUNITY_CAPABILITIES, "organizations": True, "org.defaults": True, **overrides}
    set_capability_resolver(lambda _request: caps)


@pytest.fixture(autouse=True)
def _policy_on():
    _capabilities()
    reset_resolvers()
    yield
    reset_capability_resolver()
    reset_resolvers()


@pytest.fixture()
def events(monkeypatch) -> list[dict]:
    written: list[dict] = []
    monkeypatch.setattr(audit_logger, "log", lambda **kw: written.append(kw))
    return written


def _row(db, org: Organization) -> OrganizationPolicy | None:
    db.expire_all()
    row: OrganizationPolicy | None = db.get(OrganizationPolicy, org.id)
    return row


# --------------------------------------------------------------------------- #
# Who may call
# --------------------------------------------------------------------------- #


def test_every_route_is_a_404_while_the_capability_is_off(client, gov):
    _capabilities(**{"org.defaults": False})
    with gov.acting_as(gov.admin):
        assert client.get(POLICY).status_code == status.HTTP_404_NOT_FOUND
        assert (
            client.patch(POLICY, json={"gender_inference_enabled": False}).status_code
            == status.HTTP_404_NOT_FOUND
        )
        purge = client.post(
            f"{POLICY}/purge", json={"targets": ["voiceprints"], "confirm": "PURGE"}
        )
        assert purge.status_code == status.HTTP_404_NOT_FOUND
    assert _row(gov.db, gov.org) is None


def test_a_resolver_that_omits_the_key_hides_the_panel(client, gov):
    """The key is granted explicitly or not at all (an omitted key fails closed)."""
    caps = {k: v for k, v in COMMUNITY_CAPABILITIES.items() if k != "org.defaults"}
    caps["organizations"] = True
    set_capability_resolver(lambda _request: caps)
    with gov.acting_as(gov.admin):
        assert client.get(POLICY).status_code == status.HTTP_404_NOT_FOUND


def test_a_superuser_is_not_a_way_around_the_capability(client, gov, db_session, monkeypatch):
    """No platform bypass: with the key off platform staff get the same 404 as anyone.

    The bypass reads the caller through ``get_optional_current_user`` on its own session, which
    cannot see this test's uncommitted rows, so the lookup is stubbed to return the staff user.
    The control proves the stub is reached: a route that DOES allow the bypass lets the same
    caller through.
    """
    _capabilities(**{"org.defaults": False, "watch_sources": False})
    gov.admin.role, gov.admin.is_superuser = "super_admin", True
    db_session.commit()
    monkeypatch.setattr(
        "app.api.endpoints.auth.get_optional_current_user", lambda request, db: gov.admin
    )
    with gov.acting_as(gov.admin):
        assert client.get(POLICY).status_code == status.HTTP_404_NOT_FOUND
        assert client.get("/api/watch-sources").status_code == status.HTTP_200_OK, (
            "control: a bypass-enabled route admits platform staff through a disabled capability"
        )


def test_the_community_default_hides_the_surface_from_everyone(client, gov):
    reset_capability_resolver()  # community: org.defaults is False
    with gov.acting_as(gov.admin):
        assert client.get(POLICY).status_code == status.HTTP_404_NOT_FOUND


def test_a_plain_member_is_refused_and_changes_nothing(client, gov):
    with gov.acting_as(gov.alice):
        assert client.get(POLICY).status_code == status.HTTP_403_FORBIDDEN
        response = client.patch(POLICY, json={"voiceprint_matching_enabled": False})
        assert response.status_code == status.HTTP_403_FORBIDDEN
        purge = client.post(
            f"{POLICY}/purge", json={"targets": ["voiceprints"], "confirm": "PURGE"}
        )
        assert purge.status_code == status.HTTP_403_FORBIDDEN
    assert _row(gov.db, gov.org) is None


def test_a_signed_in_user_with_no_organization_is_refused(client, gov):
    with gov.acting_as(gov.outsider, org=None):
        assert client.get(POLICY).status_code == status.HTTP_403_FORBIDDEN


def test_an_anonymous_caller_is_refused(client):
    assert client.get(POLICY).status_code == status.HTTP_401_UNAUTHORIZED
    assert (
        client.patch(POLICY, json={"gender_inference_enabled": False}).status_code
        == status.HTTP_401_UNAUTHORIZED
    )
    purge = client.post(f"{POLICY}/purge", json={"targets": ["voiceprints"], "confirm": "PURGE"})
    assert purge.status_code == status.HTTP_401_UNAUTHORIZED


def test_the_real_membership_chain_refuses_a_member_and_admits_an_admin(
    client, user_token_headers, normal_user, db_session, org_context
):
    """Over the real auth chain (token, lifecycle gates, require_org_admin), not overrides."""
    org = Organization(
        external_org_id=f"org_chain_{uuid_pkg.uuid4().hex[:8]}", name="c", is_active=True
    )
    db_session.add(org)
    db_session.commit()
    db_session.add(
        OrganizationMembership(organization_id=org.id, user_id=normal_user.id, role="org:member")
    )
    db_session.commit()

    org_context(org_id=org.id, org_role="org:member", only_for=normal_user.id)
    assert client.get(POLICY, headers=user_token_headers).status_code == status.HTTP_403_FORBIDDEN

    org_context(org_id=org.id, org_role="org:admin", only_for=normal_user.id)
    assert client.get(POLICY, headers=user_token_headers).status_code == status.HTTP_200_OK


# --------------------------------------------------------------------------- #
# Tenant isolation
# --------------------------------------------------------------------------- #


def test_an_administrator_of_another_organization_changes_only_their_own(client, gov, db_session):
    gov.join(gov.outsider, gov.other_org, role="org:admin")
    with gov.acting_as(gov.outsider, org=gov.other_org, role="org:admin"):
        response = client.patch(
            POLICY,
            json={
                "voiceprint_matching_enabled": False,
                "organization_id": gov.org.id,  # no such parameter: it must change nothing
                "redaction_floor": {"forced_custom_words": [SECRET_WORD]},
            },
        )
    assert response.status_code == status.HTTP_200_OK, response.text

    assert _row(db_session, gov.org) is None, "organization A was touched by B's administrator"
    theirs = _row(db_session, gov.other_org)
    assert theirs is not None and theirs.voiceprint_matching_enabled is False
    assert theirs.floor_forced_custom_words == [SECRET_WORD]

    with gov.acting_as(gov.admin):  # A's administrator still sees A's untouched defaults
        mine = client.get(POLICY).json()
    assert mine["voiceprint_matching_enabled"] is True
    assert mine["updated_at"] is None
    assert mine["redaction_floor"]["forced_custom_words"] == []


# --------------------------------------------------------------------------- #
# Reading and writing
# --------------------------------------------------------------------------- #


def test_the_defaults_inherit_everything(client, gov):
    with gov.acting_as(gov.admin):
        body = client.get(POLICY).json()
    assert body["voiceprint_matching_enabled"] is True
    assert body["gender_inference_enabled"] is True
    assert body["instance"] == {
        "voiceprint_matching_enabled": True,
        "gender_inference_enabled": True,
    }
    assert body["effective"]["voiceprint_matching"] == {"enabled": True, "locked_by": None}
    assert body["redaction_floor"] == {
        "forced_categories": [],
        "forced_pii_entities": [],
        "forced_custom_words": [],
        "force_toxicity_threshold": None,
        "force_export_redacted": False,
        "force_redact_before_llm": False,
    }
    assert body["updated_at"] is None


def test_a_patch_persists_and_is_partial(client, gov, db_session):
    with gov.acting_as(gov.admin):
        first = client.patch(POLICY, json={"voiceprint_matching_enabled": False})
        second = client.patch(
            POLICY,
            json={
                "redaction_floor": {
                    "forced_categories": ["pii", "toxicity"],
                    "forced_pii_entities": ["EMAIL", "PHONE"],
                    "forced_custom_words": ["alpha", "beta"],
                    "force_toxicity_threshold": 0.25,
                    "force_export_redacted": True,
                }
            },
        )
        third = client.patch(POLICY, json={"redaction_floor": {"force_toxicity_threshold": None}})
        final = client.get(POLICY).json()
    assert first.status_code == second.status_code == third.status_code == status.HTTP_200_OK

    row = _row(db_session, gov.org)
    assert row is not None
    assert row.voiceprint_matching_enabled is False
    assert row.gender_inference_enabled is True, "an unsent field must be left alone"
    assert row.floor_forced_categories == ["pii", "toxicity"]
    assert row.floor_forced_pii_entities == ["EMAIL", "PHONE"]
    assert row.floor_forced_custom_words == ["alpha", "beta"]
    assert row.floor_toxicity_threshold is None, "an explicit null clears the threshold"
    assert row.floor_export_redacted is True
    assert row.updated_by_user_id == gov.admin.id

    assert final["voiceprint_matching_enabled"] is False
    assert final["effective"]["voiceprint_matching"] == {
        "enabled": False,
        "locked_by": "organization",
    }
    assert final["effective"]["gender_inference"]["enabled"] is True
    assert final["redaction_floor"]["forced_custom_words"] == ["alpha", "beta"]
    assert final["updated_at"] is not None


def test_a_switch_the_operator_turned_off_cannot_be_turned_on_here(client, gov, db_session):
    upsert_system_setting(db_session, constants.PRIVACY_VOICEPRINT_MATCHING_KEY, "false")
    db_session.commit()
    with gov.acting_as(gov.admin):
        patched = client.patch(POLICY, json={"voiceprint_matching_enabled": True})
        body = client.get(POLICY).json()
    assert patched.status_code == status.HTTP_200_OK
    assert body["voiceprint_matching_enabled"] is True, "the organization's own choice is stored"
    assert body["instance"]["voiceprint_matching_enabled"] is False
    assert body["effective"]["voiceprint_matching"] == {"enabled": False, "locked_by": "instance"}


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"redaction_floor": {}},
        {"voiceprint_matching_enabled": "no"},
        {"redaction_floor": {"forced_categories": ["custom"]}},
        {"redaction_floor": {"forced_pii_entities": ["NOT_AN_ENTITY"]}},
        {"redaction_floor": {"forced_custom_words": ["x" * 101]}},
        {"redaction_floor": {"forced_custom_words": [f"w{i}" for i in range(201)]}},
        {"redaction_floor": {"force_toxicity_threshold": 1.01}},
        {"redaction_floor": {"forced_categories": None}},
    ],
)
def test_invalid_input_is_a_422_and_stores_nothing(client, gov, payload):
    with gov.acting_as(gov.admin):
        response = client.patch(POLICY, json=payload)
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT, response.text
    assert _row(gov.db, gov.org) is None


def test_the_floor_is_hidden_and_unwritable_while_redaction_policy_is_off(client, gov):
    _capabilities(**{"redaction.policy": False})
    with gov.acting_as(gov.admin):
        assert client.get(POLICY).json()["redaction_floor"] is None
        floor = client.patch(POLICY, json={"redaction_floor": {"force_export_redacted": True}})
        assert floor.status_code == status.HTTP_404_NOT_FOUND
        # the switches are not part of that capability
        ok = client.patch(POLICY, json={"gender_inference_enabled": False})
    assert ok.status_code == status.HTTP_200_OK
    assert ok.json()["redaction_floor"] is None
    row = _row(gov.db, gov.org)
    assert row is not None and row.gender_inference_enabled is False
    assert row.floor_export_redacted is False


# --------------------------------------------------------------------------- #
# Audit
# --------------------------------------------------------------------------- #


def test_the_audit_event_names_fields_and_never_a_value(client, gov, events):
    with gov.acting_as(gov.admin):
        response = client.patch(
            POLICY,
            json={
                "gender_inference_enabled": False,
                "redaction_floor": {
                    "forced_custom_words": [SECRET_WORD],
                    "forced_pii_entities": ["EMAIL"],
                },
            },
        )
    assert response.status_code == status.HTTP_200_OK
    changes = [e for e in events if e["event_type"] == AuditEventType.ORG_POLICY_CHANGE]
    assert len(changes) == 1
    event = changes[0]
    assert event["organization_id"] == gov.org.id
    assert event["user_id"] == gov.admin.id
    assert event["details"] == {
        "fields": [
            "gender_inference_enabled",
            "redaction_floor.forced_custom_words",
            "redaction_floor.forced_pii_entities",
        ]
    }
    assert SECRET_WORD not in repr(event)


def test_a_refused_patch_writes_no_policy_event(client, gov, events):
    with gov.acting_as(gov.alice):
        client.patch(POLICY, json={"gender_inference_enabled": False})
    assert not [e for e in events if e["event_type"] == AuditEventType.ORG_POLICY_CHANGE]


# --------------------------------------------------------------------------- #
# The floor really masks
# --------------------------------------------------------------------------- #

FORCED_WORD = "acquisition"
CLEAN_TEXT = "the quarterly numbers look fine"
SENSITIVE_TEXT = f"we should discuss the {FORCED_WORD} before the board meets"
PII_WORD = "Smithers"
PII_TEXT = f"please call {PII_WORD} tomorrow"


def _org_file_with_a_cached_word_span(db, owner, org) -> MediaFile:
    file_uuid = str(uuid_pkg.uuid4())
    media_file = MediaFile(
        uuid=file_uuid,
        filename="floor.wav",
        title="floor",
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=4096,
        status="completed",
        is_public=False,
        user_id=owner.id,
        organization_id=org.id,
        redaction_status=constants.REDACTION_STATUS_DONE,
    )
    db.add(media_file)
    db.commit()
    db.refresh(media_file)
    start = SENSITIVE_TEXT.find(FORCED_WORD)
    db.add(
        TranscriptSegment(
            media_file_id=media_file.id, start_time=0.0, end_time=1.5, text=CLEAN_TEXT
        )
    )
    db.add(
        TranscriptSegment(
            media_file_id=media_file.id,
            start_time=1.5,
            end_time=3.0,
            text=SENSITIVE_TEXT,
            redactions=[
                {
                    "char_start": start,
                    "char_end": start + len(FORCED_WORD),
                    "category": "custom",
                    "entity_type": "CUSTOM",
                    "detector": "wordlist",
                    "confidence": 1.0,
                }
            ],
        )
    )
    pii_start = PII_TEXT.find(PII_WORD)
    db.add(
        TranscriptSegment(
            media_file_id=media_file.id,
            start_time=3.0,
            end_time=4.5,
            text=PII_TEXT,
            redactions=[
                {
                    "char_start": pii_start,
                    "char_end": pii_start + len(PII_WORD),
                    "category": "pii",
                    "entity_type": "NAME",
                    "detector": "presidio",
                    "confidence": 0.99,
                }
            ],
        )
    )
    db.commit()
    return media_file


def test_a_floor_saved_through_the_api_masks_a_members_export(
    client, user_token_headers, normal_user, db_session, org_context
):
    """The whole chain: an org admin saves a word, the same user's export masks it.

    ``normal_user`` switched redaction off and is the organization's administrator here, so the
    saved floor is the ONLY thing that can change the outcome; the control reads the export
    before the floor is saved.
    """
    from app import models

    org = Organization(
        external_org_id=f"org_e2e_{uuid_pkg.uuid4().hex[:8]}", name="e2e", is_active=True
    )
    db_session.add(org)
    db_session.commit()
    db_session.add(
        OrganizationMembership(organization_id=org.id, user_id=normal_user.id, role="org:admin")
    )
    db_session.add(
        models.UserSetting(
            user_id=normal_user.id, setting_key="redaction_enabled", setting_value="false"
        )
    )
    db_session.commit()
    media_file = _org_file_with_a_cached_word_span(db_session, normal_user, org)
    org_context(org_id=org.id, org_role="org:admin", only_for=normal_user.id)

    export = f"/api/files/{media_file.uuid}/subtitles"
    before = client.get(export, headers=user_token_headers, params={"subtitle_format": "srt"})
    assert before.status_code == status.HTTP_200_OK
    assert FORCED_WORD in before.text, "control: with no floor the word ships raw"

    saved = client.patch(
        POLICY,
        headers=user_token_headers,
        json={"redaction_floor": {"forced_custom_words": [FORCED_WORD]}},
    )
    assert saved.status_code == status.HTTP_200_OK, saved.text

    after = client.get(export, headers=user_token_headers, params={"subtitle_format": "srt"})
    assert after.status_code == status.HTTP_200_OK
    assert CLEAN_TEXT in after.text
    assert FORCED_WORD not in after.text, "the stored organization floor did not reach the export"


def test_a_stored_floor_and_a_registered_resolver_both_mask(
    client, user_token_headers, normal_user, db_session, org_context
):
    """Each half masks something the other cannot: drop either and a word ships raw."""
    org = Organization(
        external_org_id=f"org_both_{uuid_pkg.uuid4().hex[:8]}", name="both", is_active=True
    )
    db_session.add(org)
    db_session.commit()
    db_session.add(
        OrganizationMembership(organization_id=org.id, user_id=normal_user.id, role="org:member")
    )
    # The user masks nothing of their own, not even custom words (the default category).
    db_session.add(
        UserSetting(
            user_id=normal_user.id, setting_key="redaction_categories", setting_value='["toxicity"]'
        )
    )
    db_session.commit()
    media_file = _org_file_with_a_cached_word_span(db_session, normal_user, org)
    db_session.add(
        OrganizationPolicy(organization_id=org.id, floor_forced_custom_words=[FORCED_WORD])
    )
    db_session.commit()
    set_redaction_floor_resolver(
        lambda db, org_id: (
            RedactionFloor(forced_categories=frozenset({"pii"})) if org_id == org.id else None
        )
    )
    org_context(org_id=org.id, org_role="org:member", only_for=normal_user.id)

    response = client.get(
        f"/api/files/{media_file.uuid}/subtitles",
        headers=user_token_headers,
        params={"subtitle_format": "srt"},
    )
    assert response.status_code == status.HTTP_200_OK
    assert FORCED_WORD not in response.text, "the stored organization floor was lost"
    assert PII_WORD not in response.text, "the registered resolver's floor was lost"
    assert CLEAN_TEXT in response.text


def test_platform_staff_are_not_an_organizations_administrators(client, super_admin_token_headers):
    """Over a real token: with the key on, a super_admin with no organization role is refused."""
    assert client.get(POLICY, headers=super_admin_token_headers).status_code == 403
