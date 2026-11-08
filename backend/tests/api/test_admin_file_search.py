"""The administrators' file pickers over HTTP: ``GET /api/{org-admin,admin}/holds/files/search``.

The picker replaced pasting file IDs, so it has to reach every file the hold routes accept, and
nothing more. An organization administrator places holds on any of the organization's files
(every member's, in the trash or taken down too) and on no other tenant's; an instance
administrator on any tenant's. The gallery list cannot serve either in multi-tenant mode (it
shows an administrator their own and shared files only), which is why these routes exist.
"""

from __future__ import annotations

import uuid as uuid_pkg
from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest
from fastapi import status

from app.core.capabilities import COMMUNITY_CAPABILITIES
from app.core.capabilities import reset_capability_resolver
from app.core.capabilities import set_capability_resolver

ORG_SEARCH = "/api/org-admin/holds/files/search"
PLATFORM_SEARCH = "/api/admin/holds/files/search"


@pytest.fixture()
def holds_on():
    set_capability_resolver(
        lambda _r: {**COMMUNITY_CAPABILITIES, "organizations": True, "org.legal_hold": True}
    )
    yield
    reset_capability_resolver()


@pytest.fixture(autouse=True)
def _no_storage(monkeypatch):
    monkeypatch.setattr("app.services.minio_service.set_object_legal_hold", lambda *a, **k: True)


@pytest.fixture()
def operator(gov):
    user = gov.user("operator")
    user.role = "admin"
    gov.db.commit()
    return user


def _trashed() -> dict:
    """The columns of a file in the trash (``ck_media_file_trash_state`` wants all three)."""
    now = datetime.now(UTC)
    return {"deleted_at": now, "purge_after": now + timedelta(days=30), "trash_origin": "user"}


@pytest.fixture()
def word() -> str:
    """A title fragment no other row in the database carries."""
    return f"deposition{uuid_pkg.uuid4().hex[:8]}"


def _uuids(response) -> set[str]:
    assert response.status_code == status.HTTP_200_OK, response.text[:300]
    return {f["uuid"] for f in response.json()["files"]}


def test_an_org_admin_finds_every_members_file_in_the_organization(client, gov, holds_on, word):
    """Including another member's, one in the trash and a taken-down one: a hold reaches all."""
    alices = gov.file(gov.alice, title=f"{word} alice")
    bobs_trashed = gov.file(gov.bob, title=f"{word} bob", **_trashed())
    taken_down = gov.file(gov.bob, title=f"{word} takedown", is_quarantined=True)

    with gov.acting_as(gov.admin):
        response = client.get(ORG_SEARCH, params={"q": word})

    assert _uuids(response) == {str(alices.uuid), str(bobs_trashed.uuid), str(taken_down.uuid)}
    by_uuid = {f["uuid"]: f for f in response.json()["files"]}
    assert by_uuid[str(bobs_trashed.uuid)]["availability"] == "trashed"
    assert by_uuid[str(taken_down.uuid)]["availability"] == "quarantined"
    assert by_uuid[str(alices.uuid)]["availability"] == "available"
    assert by_uuid[str(alices.uuid)]["organization_name"] is None  # its own organization
    assert response.json()["total"] == 3


def test_an_org_admin_cannot_find_another_organizations_or_a_personal_file(
    client, gov, holds_on, word
):
    mine = gov.file(gov.alice, title=f"{word} ours")
    gov.file(gov.outsider, org=gov.other_org, title=f"{word} theirs")
    gov.file(gov.alice, org=None, title=f"{word} personal")

    with gov.acting_as(gov.admin):
        response = client.get(ORG_SEARCH, params={"q": word})

    assert _uuids(response) == {str(mine.uuid)}
    assert response.json()["total"] == 1


def test_a_platform_admin_finds_any_tenants_file_with_its_organization(client, gov, operator, word):
    ours = gov.file(gov.alice, title=f"{word} a")
    theirs = gov.file(gov.outsider, org=gov.other_org, title=f"{word} b")
    personal = gov.file(gov.bob, org=None, title=f"{word} c", **_trashed())

    with gov.acting_as(operator, org=None):
        response = client.get(PLATFORM_SEARCH, params={"q": word})

    assert _uuids(response) == {str(ours.uuid), str(theirs.uuid), str(personal.uuid)}
    orgs = {f["uuid"]: f["organization_name"] for f in response.json()["files"]}
    assert orgs == {
        str(ours.uuid): gov.org.name,
        str(theirs.uuid): gov.other_org.name,
        str(personal.uuid): None,
    }


def test_matches_the_file_name_too_and_treats_like_wildcards_literally(client, gov, operator, word):
    named = gov.file(gov.alice, filename=f"{word}_100%.mp3")
    gov.file(gov.alice, filename=f"{word}_1000.mp3")

    with gov.acting_as(operator, org=None):
        response = client.get(PLATFORM_SEARCH, params={"q": f"{word}_100%"})

    assert _uuids(response) == {str(named.uuid)}


def test_a_held_file_says_who_holds_it_but_never_why_or_which_hold(client, gov, operator, word):
    f = gov.file(gov.alice, title=f"{word} held")
    with gov.acting_as(operator, org=None):
        placed = client.post(
            "/api/admin/holds",
            json={"scope": "file", "file_uuids": [str(f.uuid)], "reason": "Preservation 2026-1"},
        )
        assert placed.status_code == status.HTTP_201_CREATED, placed.text[:300]
        response = client.get(PLATFORM_SEARCH, params={"q": word})

    (hit,) = response.json()["files"]
    assert hit["hold"] == {"held": True, "held_by": ["platform"], "hold_uuids": []}
    assert "Preservation 2026-1" not in response.text


def test_a_regular_user_gets_403_from_both(client, gov, holds_on, word):
    gov.file(gov.alice, title=f"{word} x")
    with gov.acting_as(gov.alice):
        org_answer = client.get(ORG_SEARCH, params={"q": word})
        platform_answer = client.get(PLATFORM_SEARCH, params={"q": word})
    assert org_answer.status_code == status.HTTP_403_FORBIDDEN
    assert platform_answer.status_code == status.HTTP_403_FORBIDDEN
    assert word not in org_answer.text + platform_answer.text


def test_an_org_admin_is_not_an_instance_admin(client, gov, word):
    gov.file(gov.outsider, org=gov.other_org, title=f"{word} x")
    with gov.acting_as(gov.admin):
        answer = client.get(PLATFORM_SEARCH, params={"q": word})
    assert answer.status_code == status.HTTP_403_FORBIDDEN


def test_pages_are_capped_and_a_blank_query_returns_nothing(client, gov, operator, word):
    for i in range(3):
        gov.file(gov.alice, title=f"{word} {i}")
    with gov.acting_as(operator, org=None):
        too_big = client.get(PLATFORM_SEARCH, params={"q": word, "limit": 51})
        page = client.get(PLATFORM_SEARCH, params={"q": word, "limit": 2})
        blank = client.get(PLATFORM_SEARCH, params={"q": "   "})

    assert too_big.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert len(page.json()["files"]) == 2
    assert page.json()["total"] == 3
    assert blank.json() == {"files": [], "total": 0}
