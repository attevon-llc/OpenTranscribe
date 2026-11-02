"""Personal-workspace and instance-wide purges: a person's own data only, and the instance switch and super-admin rules.

Fixtures (in-memory OpenSearch, recorded broker, in-process task runner) live in
``tests/api/privacy_purge/conftest.py``; seeding helpers in ``tests/fixtures/privacy_purge.py``.
"""

from __future__ import annotations

import pytest
from fastapi import status

from app.core import constants
from app.models.prompt import UserSetting
from app.models.speaker import Speaker
from app.models.speaker import SpeakerMatch
from tests.fixtures.privacy_purge import ADMIN_PURGE
from tests.fixtures.privacy_purge import BODY
from tests.fixtures.privacy_purge import ME_PURGE
from tests.fixtures.privacy_purge import ORG_PURGE
from tests.fixtures.privacy_purge import assert_purged
from tests.fixtures.privacy_purge import assert_untouched
from tests.fixtures.privacy_purge import purge_via_api
from tests.fixtures.privacy_purge import seed
from tests.fixtures.privacy_purge import turn_off

# --------------------------------------------------------------------------- #
# The person's own purge
# --------------------------------------------------------------------------- #


def test_a_person_purges_only_their_own_data_in_the_active_scope(
    client, gov, opensearch, broker, in_process_task, reindexed
):
    mine = seed(gov, opensearch, gov.alice, gov.org, "alice-org")
    mine_personal = seed(gov, opensearch, gov.alice, None, "alice-personal")
    bobs = seed(gov, opensearch, gov.bob, gov.org, "bob-org")
    gov.db.add(
        UserSetting(
            user_id=gov.alice.id,
            setting_key=constants.PRIVACY_USER_VOICEPRINT_MATCHING_KEY,
            setting_value="false",
        )
    )
    gov.db.add(
        UserSetting(
            user_id=gov.alice.id,
            setting_key=constants.PRIVACY_USER_GENDER_DETECTION_KEY,
            setting_value="false",
        )
    )
    gov.db.commit()

    response = purge_via_api(client, gov, gov.alice, ME_PURGE)
    assert response.status_code == status.HTTP_202_ACCEPTED, response.text
    in_process_task(broker[0])

    assert_purged(gov.db, mine, opensearch)
    assert_untouched(gov.db, bobs, opensearch)
    assert_untouched(gov.db, mine_personal, opensearch)


def _switch_off_for(db, user) -> None:
    for key in (
        constants.PRIVACY_USER_VOICEPRINT_MATCHING_KEY,
        constants.PRIVACY_USER_GENDER_DETECTION_KEY,
    ):
        db.add(UserSetting(user_id=user.id, setting_key=key, setting_value="false"))
    db.commit()


def test_the_personal_workspace_purge_leaves_the_same_persons_organization_data(
    client, gov, opensearch, broker, in_process_task, reindexed
):
    mine_personal = seed(gov, opensearch, gov.alice, None, "alice-personal")
    mine_org = seed(gov, opensearch, gov.alice, gov.org, "alice-org")
    others_personal = seed(gov, opensearch, gov.bob, None, "bob-personal")
    _switch_off_for(gov.db, gov.alice)

    response = purge_via_api(client, gov, gov.alice, ME_PURGE, org=None)
    assert response.status_code == status.HTTP_202_ACCEPTED, response.text
    result = in_process_task(broker[0])

    assert_purged(gov.db, mine_personal, opensearch)
    assert_untouched(gov.db, mine_org, opensearch)
    assert_untouched(gov.db, others_personal, opensearch)
    # Only the personal workspace's rows were counted: 2 speakers carry inferred gender, 1 match,
    # 2 voiceprint suggestions (one on a person-verified speaker).
    postgres = result["counts"]["postgres"]
    assert postgres["gender_speakers_cleared"] == 2
    assert postgres["speaker_matches_deleted"] == 1
    assert postgres["suggestions_cleared"] == 2


# One store at a time: what each of the four stores holds for a seeded scope, as "kept?" Each
# reader returns True while the scope's rows / documents are still there.
_STORES = {
    "inferred_gender": lambda db, s, os_: (
        db.get(Speaker, s.guess).predicted_gender is not None
        and db.get(Speaker, s.other).predicted_gender is not None
    ),
    "voice_suggestions": lambda db, s, os_: db.get(Speaker, s.guess).suggestion_source is not None,
    "speaker_matches": lambda db, s, os_: (
        db.query(SpeakerMatch).filter(SpeakerMatch.speaker1_id.in_([s.guess, s.other])).count() == 1
    ),
    "opensearch_voiceprints": lambda db, s, os_: s.doc_ids <= os_.ids(),
}


@pytest.mark.parametrize("store", sorted(_STORES))
@pytest.mark.parametrize("purged_scope", ["organization", "personal"])
def test_each_purge_touches_only_its_own_tenants_rows_in_each_store(
    client, gov, opensearch, broker, in_process_task, reindexed, store, purged_scope
):
    """Two tenants hold the SAME person's data side by side; a purge of one leaves the other.

    The organization purge and the person's personal-workspace purge are the two that could
    reach across: both are run against the identical pair, and each store is read separately so
    a failure names the store that was crossed.
    """
    in_org = seed(gov, opensearch, gov.alice, gov.org, "alice-org")
    other_org_member = seed(gov, opensearch, gov.bob, gov.org, "bob-org")
    personal = seed(gov, opensearch, gov.alice, None, "alice-personal")
    elsewhere = seed(gov, opensearch, gov.outsider, gov.other_org, "outsider-org")
    if purged_scope == "organization":
        turn_off(gov.db, gov.org)
        response = purge_via_api(client, gov, gov.admin, ORG_PURGE)
        gone, kept = [in_org, other_org_member], [personal, elsewhere]
    else:
        _switch_off_for(gov.db, gov.alice)
        response = purge_via_api(client, gov, gov.alice, ME_PURGE, org=None)
        gone, kept = [personal], [in_org, other_org_member, elsewhere]
    assert response.status_code == status.HTTP_202_ACCEPTED, response.text
    gov.db.expire_all()
    for seeded in gone + kept:
        assert _STORES[store](gov.db, seeded, opensearch), f"{seeded.tag}: seeded wrongly"

    in_process_task(broker[0])

    gov.db.expire_all()
    assert not any(_STORES[store](gov.db, seeded, opensearch) for seeded in gone)
    assert all(_STORES[store](gov.db, seeded, opensearch) for seeded in kept)


def test_a_person_may_purge_once_their_organization_turned_it_off_without_changing_their_switch(
    client, gov, opensearch, broker
):
    seed(gov, opensearch, gov.alice, gov.org, "alice-org")
    turn_off(gov.db, gov.org)
    # alice's own switch is untouched (on), and the organization now locks it
    with gov.acting_as(gov.alice):
        assert (
            client.put("/api/privacy/me", json={"voiceprint_matching_enabled": False}).status_code
            == status.HTTP_409_CONFLICT
        )
    response = purge_via_api(client, gov, gov.alice, ME_PURGE)
    assert response.status_code == status.HTTP_202_ACCEPTED, response.text


def test_a_person_cannot_purge_while_everything_is_on(client, gov, opensearch, broker):
    mine = seed(gov, opensearch, gov.alice, gov.org, "alice-org")
    response = purge_via_api(client, gov, gov.alice, ME_PURGE)
    assert response.status_code == status.HTTP_409_CONFLICT
    assert response.json()["detail"]["code"] == "policy_still_enabled"
    assert not broker
    assert_untouched(gov.db, mine, opensearch)


# --------------------------------------------------------------------------- #
# Instance purge
# --------------------------------------------------------------------------- #


def _instance_off(client, headers) -> None:
    response = client.put(
        "/api/admin/privacy-policy",
        headers=headers,
        json={"voiceprint_matching_enabled": False, "gender_inference_enabled": False},
    )
    assert response.status_code == status.HTTP_200_OK, response.text


def test_an_instance_purge_of_the_personal_scope_leaves_every_tenant(
    client, gov, super_admin_token_headers, opensearch, broker, in_process_task, reindexed
):
    personal = seed(gov, opensearch, gov.alice, None, "personal")
    personal2 = seed(gov, opensearch, gov.outsider, None, "personal2")
    tenant = seed(gov, opensearch, gov.alice, gov.org, "orgA")
    _instance_off(client, super_admin_token_headers)

    response = client.post(
        ADMIN_PURGE, headers=super_admin_token_headers, json={**BODY, "scope": "personal"}
    )
    assert response.status_code == status.HTTP_202_ACCEPTED, response.text
    in_process_task(broker[0])

    assert_purged(gov.db, personal, opensearch)
    assert_purged(gov.db, personal2, opensearch)
    assert_untouched(gov.db, tenant, opensearch)


def test_scope_all_is_refused_in_a_multi_tenant_deployment(
    client, gov, super_admin_token_headers, opensearch, broker, monkeypatch
):
    from app.services import platform_access

    tenant = seed(gov, opensearch, gov.alice, gov.org, "orgA")
    _instance_off(client, super_admin_token_headers)
    body = {**BODY, "scope": "all"}

    monkeypatch.setattr(platform_access.settings, "TENANCY_MODE", "multi")
    refused = client.post(ADMIN_PURGE, headers=super_admin_token_headers, json=body)
    assert refused.status_code == status.HTTP_403_FORBIDDEN
    assert refused.json()["detail"] == {"code": "purge_scope_not_allowed"}
    assert not broker
    assert_untouched(gov.db, tenant, opensearch)

    monkeypatch.setattr(platform_access.settings, "TENANCY_MODE", "single")
    allowed = client.post(ADMIN_PURGE, headers=super_admin_token_headers, json=body)
    assert allowed.status_code == status.HTTP_202_ACCEPTED, "control: single-tenant mode allows it"


def test_scope_all_deletes_everything_when_allowed(
    client,
    gov,
    super_admin_token_headers,
    opensearch,
    broker,
    in_process_task,
    reindexed,
    monkeypatch,
):
    from app.services import platform_access

    monkeypatch.setattr(platform_access.settings, "TENANCY_MODE", "single")
    everything = [
        seed(gov, opensearch, gov.alice, None, "personal"),
        seed(gov, opensearch, gov.alice, gov.org, "orgA"),
        seed(gov, opensearch, gov.outsider, gov.other_org, "orgB"),
    ]
    _instance_off(client, super_admin_token_headers)
    client.post(ADMIN_PURGE, headers=super_admin_token_headers, json={**BODY, "scope": "all"})
    in_process_task(broker[0])
    for seeded in everything:
        assert_purged(gov.db, seeded, opensearch)


def test_an_instance_purge_is_refused_while_the_instance_switch_is_on(
    client, gov, super_admin_token_headers, opensearch, broker
):
    personal = seed(gov, opensearch, gov.alice, None, "personal")
    response = client.post(
        ADMIN_PURGE, headers=super_admin_token_headers, json={**BODY, "scope": "personal"}
    )
    assert response.status_code == status.HTTP_409_CONFLICT
    assert not broker
    assert_untouched(gov.db, personal, opensearch)


def test_only_a_super_admin_can_start_an_instance_purge(
    client, user_token_headers, admin_token_headers, opensearch, broker
):
    body = {**BODY, "scope": "personal"}
    anonymous = client.post(ADMIN_PURGE, json=body)
    assert anonymous.status_code == status.HTTP_403_FORBIDDEN
    assert (
        client.post(ADMIN_PURGE, headers=user_token_headers, json=body).status_code
        == status.HTTP_403_FORBIDDEN
    )
    assert (
        client.post(ADMIN_PURGE, headers=admin_token_headers, json=body).status_code
        == status.HTTP_403_FORBIDDEN
    )
    assert not broker
