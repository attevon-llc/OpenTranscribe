"""The bulk rail forwards the request's platform bypass to the per-file tag change.

``test_a_platform_admin_bypass_may_tag_any_users_file`` pins the bypass at the service
(``apply_tag_to_file``); nothing pinned that the route's tag handler passes ``ctx.bypass``
through, so a handler that dropped it survived the whole bulk/tag/tenancy suite.
"""

from __future__ import annotations

from fixtures.tag_world import make_file
from fixtures.tag_world import word

from app.models.tag import FileTag
from app.models.tag import Tag
from app.services.tag_service import normalize_tag_name

BULK = "/api/files/management/bulk-action"


def test_a_platform_admin_bulk_tags_another_users_file_through_the_route(
    client, db_session, admin_user, other_user, admin_token_headers
):
    theirs = make_file(db_session, other_user)
    name = word("AdminBulk")

    response = client.post(
        BULK,
        headers=admin_token_headers,
        json={"file_uuids": [str(theirs.uuid)], "action": "add_tag", "tag_name": name},
    )

    assert response.status_code == 200, response.text
    assert [row["outcome"] for row in response.json()] == ["added"]
    db_session.expire_all()
    tag = db_session.query(Tag).filter(Tag.normalized_name == normalize_tag_name(name)).one()
    attached = (
        db_session.query(FileTag)
        .filter(FileTag.media_file_id == theirs.id, FileTag.tag_id == tag.id)
        .count()
    )
    assert attached == 1


def test_a_failed_tag_result_reaches_the_row_field_for_field(
    client, db_session, normal_user, user_token_headers, monkeypatch
):
    """A refusal returned (not raised) by ``apply_tag_to_file`` keeps ``success`` False."""
    from app.services.tag_bulk import BulkTagOutcome
    from app.services.tag_bulk import BulkTagResult

    monkeypatch.setattr(
        "app.api.endpoints.files.bulk_actions.refresh_tagged_files", lambda *a, **k: []
    )
    monkeypatch.setattr(
        "app.api.endpoints.files.bulk_action_handlers.apply_tag_to_file",
        lambda *a, **k: BulkTagResult(BulkTagOutcome.FAILED, message="refused", error="E_REFUSED"),
    )
    own = make_file(db_session, normal_user)

    response = client.post(
        BULK,
        headers=user_token_headers,
        json={"file_uuids": [str(own.uuid)], "action": "add_tag", "tag_name": word("Refused")},
    )

    assert response.status_code == 200, response.text
    assert response.json() == [
        {
            "file_uuid": str(own.uuid),
            "success": False,
            "message": "refused",
            "error": "E_REFUSED",
            "outcome": "failed",
        }
    ]
