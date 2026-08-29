"""Comment endpoint tests.

**What was missing.** The file held five happy paths plus two 401s: no 403, no
404, no 422, and no cross-user request at all — on a router that authorizes the
*same resource* three different ways:

===========  ==========================================================
``GET``      ``_check_file_access(..., organization_id=ctx.org_id)`` — the
             tenant gate is applied
``PUT``      ``require_resource_owner(...)`` — author-or-admin, **no
             RequestContext, so no tenant gate**
``DELETE``   three inline branches (admin / author / file owner) — no
             ``PermissionService``, **no RequestContext**
===========  ==========================================================

The consequence is asserted at the bottom of this file: for a comment on a file
outside the caller's active tenant scope, the **read 403s and the mutation
succeeds**. Those two tests are ``xfail(strict=True)`` — they describe the
intended behaviour, so whoever adds the missing ``ctx`` to ``update_comment`` /
``delete_comment`` will be told to drop the marker rather than having to
rediscover the asymmetry.

The rest of the suite is now DB-backed rather than upload-backed: the S3 gate
used to skip the **whole module**, so on a machine without MinIO nothing here ran
at all. Only the tests that genuinely upload a file keep that skip.
"""

from __future__ import annotations

import os
import uuid as uuid_pkg

import pytest

from app.models.media import Comment
from app.models.media import MediaFile
from app.models.organization import Organization
from app.models.organization import OrganizationMembership

requires_s3 = pytest.mark.skipif(
    os.environ.get("SKIP_S3", "True").lower() == "true",
    reason="S3/MinIO storage is disabled in this environment",
)

COMMENTS_PATH = "/api/comments"


def _make_file(
    db_session, user, *, org_id: int | None = None, quarantined: bool = False
) -> MediaFile:
    file_uuid = uuid_pkg.uuid4()
    row = MediaFile(
        uuid=file_uuid,
        filename=f"{file_uuid}.wav",
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=1024,
        user_id=user.id,
        organization_id=org_id,
        status="completed",
        is_quarantined=quarantined,
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)
    return row


def _make_comment(db_session, media_file, author, text: str = "a remark") -> Comment:
    row = Comment(
        uuid=uuid_pkg.uuid4(),
        media_file_id=media_file.id,
        user_id=author.id,
        text=text,
        timestamp=12.5,
    )
    db_session.add(row)
    db_session.commit()
    db_session.refresh(row)  # created_at is a server default
    return row


@pytest.fixture
def owned_file(db_session, normal_user) -> MediaFile:
    return _make_file(db_session, normal_user)


@pytest.fixture
def own_comment(db_session, owned_file, normal_user) -> Comment:
    return _make_comment(db_session, owned_file, normal_user)


# --------------------------------------------------------------------------- #
# Cross-user authorization                                                     #
# --------------------------------------------------------------------------- #
class TestCrossUserAccessIsRefused:
    """``other_user`` holds no ownership and no share on the file, so every verb
    must refuse. None of these had a test, on a router where the read path and
    the two write paths compute permission independently."""

    def test_a_stranger_cannot_read_a_comment(self, client, own_comment, other_user_auth_headers):
        response = client.get(
            f"{COMMENTS_PATH}/{own_comment.uuid}", headers=other_user_auth_headers
        )
        assert response.status_code == 403, response.text

    def test_a_stranger_cannot_edit_a_comment(
        self, client, own_comment, other_user_auth_headers, db_session
    ):
        response = client.put(
            f"{COMMENTS_PATH}/{own_comment.uuid}",
            headers=other_user_auth_headers,
            json={"text": "hijacked"},
        )
        assert response.status_code == 403, response.text
        db_session.refresh(own_comment)
        assert own_comment.text == "a remark"

    def test_a_stranger_cannot_delete_a_comment(
        self, client, own_comment, other_user_auth_headers, db_session
    ):
        comment_id = own_comment.id
        response = client.delete(
            f"{COMMENTS_PATH}/{own_comment.uuid}", headers=other_user_auth_headers
        )
        assert response.status_code == 403, response.text
        assert db_session.query(Comment).filter(Comment.id == comment_id).first() is not None

    def test_a_stranger_cannot_list_comments_on_the_file(
        self, client, owned_file, own_comment, other_user_auth_headers
    ):
        response = client.get(
            COMMENTS_PATH,
            params={"media_file_id": str(owned_file.uuid)},
            headers=other_user_auth_headers,
        )
        assert response.status_code == 403, response.text

    def test_a_stranger_cannot_list_via_the_nested_route(
        self, client, owned_file, other_user_auth_headers
    ):
        """The nested pair lives under the comments prefix
        (``/api/comments/files/{uuid}/comments``), not under ``/api/files`` — it is
        a second, independently-written access check on the same resource."""
        response = client.get(
            f"{COMMENTS_PATH}/files/{owned_file.uuid}/comments", headers=other_user_auth_headers
        )
        assert response.status_code == 403, response.text

    def test_a_stranger_cannot_comment_on_the_file(
        self, client, owned_file, other_user_auth_headers
    ):
        response = client.post(
            COMMENTS_PATH,
            headers=other_user_auth_headers,
            json={"media_file_id": str(owned_file.uuid), "text": "uninvited", "timestamp": 1.0},
        )
        assert response.status_code == 403, response.text

    def test_a_stranger_cannot_comment_via_the_nested_route(
        self, client, owned_file, other_user_auth_headers
    ):
        response = client.post(
            f"{COMMENTS_PATH}/files/{owned_file.uuid}/comments",
            headers=other_user_auth_headers,
            json={"text": "uninvited", "timestamp": 1.0},
        )
        assert response.status_code == 403, response.text


class TestPermittedMutations:
    """The control side: the same code paths must still let the right people
    through, or the 403s above would also pass with everything broken."""

    def test_the_author_can_edit_their_own_comment(self, client, own_comment, user_token_headers):
        response = client.put(
            f"{COMMENTS_PATH}/{own_comment.uuid}",
            headers=user_token_headers,
            json={"text": "revised", "timestamp": 20.0},
        )
        assert response.status_code == 200, response.text
        assert response.json()["text"] == "revised"

    def test_the_author_can_delete_their_own_comment(self, client, own_comment, user_token_headers):
        response = client.delete(f"{COMMENTS_PATH}/{own_comment.uuid}", headers=user_token_headers)
        assert response.status_code == 204, response.text

    def test_the_file_owner_can_delete_someone_elses_comment(
        self, client, owned_file, other_user, user_token_headers, db_session
    ):
        """The third inline branch in ``delete_comment`` — moderation of your own
        file's thread. It had no test, so the branch could be deleted silently."""
        guest_comment = _make_comment(db_session, owned_file, other_user, text="from a guest")
        response = client.delete(
            f"{COMMENTS_PATH}/{guest_comment.uuid}", headers=user_token_headers
        )
        assert response.status_code == 204, response.text

    def test_a_platform_admin_can_delete_any_comment(
        self, client, own_comment, admin_token_headers
    ):
        response = client.delete(f"{COMMENTS_PATH}/{own_comment.uuid}", headers=admin_token_headers)
        assert response.status_code == 204, response.text

    def test_a_platform_admin_can_read_any_comment(self, client, own_comment, admin_token_headers):
        """``get_comment`` skips the permission check entirely for an admin."""
        response = client.get(f"{COMMENTS_PATH}/{own_comment.uuid}", headers=admin_token_headers)
        assert response.status_code == 200, response.text


# --------------------------------------------------------------------------- #
# Not-found and validation                                                     #
# --------------------------------------------------------------------------- #
class TestNotFoundAndValidation:
    def test_an_unknown_comment_uuid_is_404_on_read(self, client, user_token_headers):
        response = client.get(f"{COMMENTS_PATH}/{uuid_pkg.uuid4()}", headers=user_token_headers)
        assert response.status_code == 404, response.text

    def test_an_unknown_comment_uuid_is_404_on_edit(self, client, user_token_headers):
        response = client.put(
            f"{COMMENTS_PATH}/{uuid_pkg.uuid4()}",
            headers=user_token_headers,
            json={"text": "nothing to edit"},
        )
        assert response.status_code == 404, response.text

    def test_an_unknown_comment_uuid_is_404_on_delete(self, client, user_token_headers):
        response = client.delete(f"{COMMENTS_PATH}/{uuid_pkg.uuid4()}", headers=user_token_headers)
        assert response.status_code == 404, response.text

    def test_a_malformed_comment_uuid_is_400(self, client, user_token_headers):
        response = client.get(f"{COMMENTS_PATH}/not-a-uuid", headers=user_token_headers)
        assert response.status_code == 400, response.text

    def test_listing_without_a_file_reference_is_422(self, client, user_token_headers):
        """The handler raises this itself — both query names are optional so
        FastAPI cannot."""
        response = client.get(COMMENTS_PATH, headers=user_token_headers)
        assert response.status_code == 422, response.text

    def test_commenting_without_text_is_422(self, client, owned_file, user_token_headers):
        response = client.post(
            COMMENTS_PATH,
            headers=user_token_headers,
            json={"media_file_id": str(owned_file.uuid), "timestamp": 1.0},
        )
        assert response.status_code == 422, response.text

    def test_commenting_on_an_unknown_file_is_404(self, client, user_token_headers):
        response = client.post(
            COMMENTS_PATH,
            headers=user_token_headers,
            json={"media_file_id": str(uuid_pkg.uuid4()), "text": "into the void"},
        )
        assert response.status_code == 404, response.text

    def test_a_quarantined_file_hides_its_comments_from_the_owner(
        self, client, db_session, normal_user, user_token_headers
    ):
        """Takedown parity: 404, not 403, so a taken-down file is
        indistinguishable from a missing one — even for its owner."""
        hidden = _make_file(db_session, normal_user, quarantined=True)
        response = client.get(
            COMMENTS_PATH,
            params={"media_file_id": str(hidden.uuid)},
            headers=user_token_headers,
        )
        assert response.status_code == 404, response.text

    def test_the_legacy_query_name_still_resolves_the_file(
        self, client, owned_file, user_token_headers
    ):
        """``media_file_uuid`` is the older spelling the handler still accepts;
        nothing asserted that the second branch works."""
        response = client.get(
            COMMENTS_PATH,
            params={"media_file_uuid": str(owned_file.uuid)},
            headers=user_token_headers,
        )
        assert response.status_code == 200, response.text

    def test_listing_is_401_unauthenticated(self, client):
        response = client.get(COMMENTS_PATH)
        assert response.status_code == 401, response.text

    def test_creating_is_401_unauthenticated(self, client):
        response = client.post(
            COMMENTS_PATH,
            json={"media_file_id": str(uuid_pkg.uuid4()), "text": "should fail"},
        )
        assert response.status_code == 401, response.text


# --------------------------------------------------------------------------- #
# Quarantine parity for the comment-UUID routes (adversarial-review follow-up) #
# --------------------------------------------------------------------------- #
class TestQuarantineHidesCommentByUuid:
    """``test_a_quarantined_file_hides_its_comments_from_the_owner`` above
    covers the file-scoped listing route (``_check_file_access``, which
    resolves through ``get_file_by_uuid_with_permission`` and already 404s a
    quarantined file). ``GET``/``PUT``/``DELETE /{comment_uuid}`` reach the
    SAME comments through a completely different gate
    (``_assert_comment_file_in_scope``), which used only
    ``PermissionService.get_file_permission`` — no quarantine awareness at
    all. So a comment on the caller's own quarantined file stayed fully
    readable, editable and deletable by UUID even though the file itself, and
    its comment LIST, both 404 — A2's leak class, reaching even the file's
    own owner."""

    def test_a_quarantined_files_comment_is_404_on_read_by_uuid(
        self, client, db_session, normal_user, user_token_headers
    ):
        hidden = _make_file(db_session, normal_user, quarantined=True)
        comment = _make_comment(db_session, hidden, normal_user)
        response = client.get(f"{COMMENTS_PATH}/{comment.uuid}", headers=user_token_headers)
        assert response.status_code == 404, response.text

    def test_a_quarantined_files_comment_cannot_be_edited_by_uuid(
        self, client, db_session, normal_user, user_token_headers
    ):
        hidden = _make_file(db_session, normal_user, quarantined=True)
        comment = _make_comment(db_session, hidden, normal_user)
        response = client.put(
            f"{COMMENTS_PATH}/{comment.uuid}",
            headers=user_token_headers,
            json={"text": "should not apply"},
        )
        assert response.status_code == 404, response.text
        db_session.refresh(comment)
        assert comment.text == "a remark"

    def test_a_quarantined_files_comment_cannot_be_deleted_by_uuid(
        self, client, db_session, normal_user, user_token_headers
    ):
        hidden = _make_file(db_session, normal_user, quarantined=True)
        comment = _make_comment(db_session, hidden, normal_user)
        response = client.delete(f"{COMMENTS_PATH}/{comment.uuid}", headers=user_token_headers)
        assert response.status_code == 404, response.text
        assert db_session.query(Comment).filter(Comment.id == comment.id).first() is not None

    def test_a_platform_admin_can_still_read_a_quarantined_files_comment(
        self, client, db_session, normal_user, admin_token_headers
    ):
        """Control: admins retain review access, matching every other
        quarantine gate (``is_hidden_for`` bypasses on ``is_admin``)."""
        hidden = _make_file(db_session, normal_user, quarantined=True)
        comment = _make_comment(db_session, hidden, normal_user)
        response = client.get(f"{COMMENTS_PATH}/{comment.uuid}", headers=admin_token_headers)
        assert response.status_code == 200, response.text


# --------------------------------------------------------------------------- #
# The three-way authorization asymmetry                                        #
# --------------------------------------------------------------------------- #
class TestTenantGateAsymmetry:
    """One comment, one caller, one out-of-scope file — three different answers.

    ``normal_user`` authored the comment, so ``require_resource_owner`` (PUT) and
    the author branch (DELETE) both admit them. The file, however, belongs to a
    different organization than the caller's active scope, which the read path
    rejects. Read and write therefore disagree about whether the caller may touch
    the resource at all, and the write side wins.
    """

    @pytest.fixture
    def out_of_scope_comment(self, db_session, normal_user, org_context) -> Comment:
        home = Organization(
            external_org_id=f"org_home_{uuid_pkg.uuid4().hex[:10]}", name="Home", is_active=True
        )
        elsewhere = Organization(
            external_org_id=f"org_else_{uuid_pkg.uuid4().hex[:10]}",
            name="Elsewhere",
            is_active=True,
        )
        db_session.add_all([home, elsewhere])
        db_session.commit()
        db_session.add(
            OrganizationMembership(
                organization_id=home.id, user_id=normal_user.id, role="org:member"
            )
        )
        db_session.commit()

        file_elsewhere = _make_file(db_session, normal_user, org_id=elsewhere.id)
        comment = _make_comment(db_session, file_elsewhere, normal_user, text="written earlier")
        org_context(org_id=home.id, org_role="org:member", only_for=normal_user.id)
        return comment

    def test_reading_it_is_403(self, client, out_of_scope_comment, user_token_headers):
        """The read path threads ``ctx.org_id``, so the tenant gate fires."""
        response = client.get(
            f"{COMMENTS_PATH}/{out_of_scope_comment.uuid}", headers=user_token_headers
        )
        assert response.status_code == 403, response.text

    def test_editing_it_is_403(self, client, out_of_scope_comment, user_token_headers):
        """Authorship does not survive the caller moving organizations."""
        response = client.put(
            f"{COMMENTS_PATH}/{out_of_scope_comment.uuid}",
            headers=user_token_headers,
            json={"text": "edited out of scope"},
        )
        assert response.status_code == 403, response.text

    def test_the_refused_edit_did_not_land(
        self, client, db_session, out_of_scope_comment, user_token_headers
    ):
        """A 403 that still wrote the row would satisfy the status assertion above.

        The gate runs before the field loop in ``update_comment``, so this pins
        that ordering rather than trusting it.
        """
        client.put(
            f"{COMMENTS_PATH}/{out_of_scope_comment.uuid}",
            headers=user_token_headers,
            json={"text": "edited out of scope"},
        )
        db_session.refresh(out_of_scope_comment)
        assert out_of_scope_comment.text == "written earlier"

    def test_deleting_it_is_403(self, client, out_of_scope_comment, user_token_headers):
        """The author and file-owner branches are both behind the scope gate."""
        response = client.delete(
            f"{COMMENTS_PATH}/{out_of_scope_comment.uuid}", headers=user_token_headers
        )
        assert response.status_code == 403, response.text

    def test_the_refused_delete_did_not_land(
        self, client, db_session, out_of_scope_comment, user_token_headers
    ):
        """``delete_comment`` commits inside each branch, so prove nothing committed."""
        comment_id = out_of_scope_comment.id
        client.delete(f"{COMMENTS_PATH}/{out_of_scope_comment.uuid}", headers=user_token_headers)
        surviving = db_session.query(Comment).filter(Comment.id == comment_id).one_or_none()
        assert surviving is not None, "the refused delete removed the row anyway"
        assert surviving.text == "written earlier"


# --------------------------------------------------------------------------- #
# Upload-backed happy paths (need MinIO)                                       #
# --------------------------------------------------------------------------- #
@requires_s3
class TestUploadBackedRoundTrip:
    @pytest.fixture
    def uploaded_file_uuid(self, user_token_headers, upload_test_file) -> str:
        data = upload_test_file(user_token_headers, filename="comment_test.wav")
        return str(data.get("uuid") or data.get("id"))

    def test_create_then_list_then_edit_then_delete(
        self, client, user_token_headers, uploaded_file_uuid
    ):
        """The original happy path, kept as one round trip against a real upload."""
        created = client.post(
            COMMENTS_PATH,
            headers=user_token_headers,
            json={"media_file_id": uploaded_file_uuid, "text": "first", "timestamp": 30.5},
        )
        assert created.status_code == 200, created.text
        comment_uuid = created.json()["uuid"]

        listed = client.get(
            COMMENTS_PATH,
            params={"media_file_id": uploaded_file_uuid},
            headers=user_token_headers,
        )
        assert listed.status_code == 200, listed.text
        assert comment_uuid in {row["uuid"] for row in listed.json()}

        edited = client.put(
            f"{COMMENTS_PATH}/{comment_uuid}",
            headers=user_token_headers,
            json={"text": "second", "timestamp": 31.0},
        )
        assert edited.status_code == 200, edited.text
        assert edited.json()["text"] == "second"

        removed = client.delete(f"{COMMENTS_PATH}/{comment_uuid}", headers=user_token_headers)
        assert removed.status_code == 204, removed.text
