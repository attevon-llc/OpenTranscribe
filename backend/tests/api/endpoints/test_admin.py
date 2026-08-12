"""Admin endpoint tests."""

import uuid
from unittest.mock import patch

from fastapi import status

from app.core.version import APP_VERSION
from tests.user_owned_rows import seed_owned_rows


def test_admin_stats(client, admin_token_headers):
    """Test getting admin statistics"""
    response = client.get("/api/admin/stats", headers=admin_token_headers)
    assert response.status_code == 200
    stats = response.json()

    # Basic schema validation
    assert "users" in stats
    assert "total" in stats["users"]
    assert "files" in stats
    assert "system" in stats


def test_admin_stats_reports_the_real_build_version(client, admin_token_headers):
    """``system.version`` is the build identity, not a hardcoded literal.

    It was ``"1.0.0"``, which no release ever moved, while ``/health`` and
    ``/api/system/stats`` reported the real ``APP_VERSION`` — so the admin panel
    disagreed with the About dialog about which build was running.
    """
    response = client.get("/api/admin/stats", headers=admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    version = response.json()["system"]["version"]
    assert version == APP_VERSION
    assert version != "1.0.0"


def test_admin_stats_gpu_is_a_list(client, admin_token_headers):
    """``system.gpu`` is a list of per-device dicts — one entry per active GPU."""
    response = client.get("/api/admin/stats", headers=admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    gpu = response.json()["system"]["gpu"]
    assert isinstance(gpu, list)
    assert gpu, "at least one entry is always reported, even when no GPU is present"
    assert all(isinstance(entry, dict) for entry in gpu)
    assert all("available" in entry for entry in gpu)


def test_admin_stats_gpu_stays_a_list_when_collection_fails(client, admin_token_headers):
    """The stats-collection fallback keeps ``gpu``'s type.

    It used to substitute a bare dict, so the key was a list on the happy path and
    a dict when psutil raised — a client indexing ``gpu[0]`` broke only in the
    failure path, which is exactly where it would not be noticed.
    """

    def _boom():
        raise RuntimeError("psutil unavailable")

    with patch("app.api.endpoints.admin.get_cpu_usage", _boom):
        response = client.get("/api/admin/stats", headers=admin_token_headers)

    assert response.status_code == status.HTTP_200_OK
    system = response.json()["system"]
    assert system["cpu"]["total_percent"] == "Unknown"
    assert isinstance(system["gpu"], list)
    assert system["gpu"][0]["available"] is False


def test_admin_stats_unauthorized(client, user_token_headers):
    """Test that regular users cannot access admin stats"""
    response = client.get("/api/admin/stats", headers=user_token_headers)
    assert response.status_code == 403, (
        response.text
    )  # authenticated but not an admin: get_current_admin_user/get_current_active_superuser raise 403


def test_admin_users_list(client, admin_token_headers, admin_user, normal_user):
    """Test admin users list endpoint"""
    response = client.get("/api/admin/users", headers=admin_token_headers)
    assert response.status_code == 200
    users = response.json()
    assert isinstance(users, list)

    # There should be at least 2 users (normal and admin from fixtures)
    assert len(users) >= 2

    # Basic schema validation - check for uuid field
    assert "uuid" in users[0] or "id" in users[0]
    assert "email" in users[0]


def test_admin_users_create(client, admin_token_headers, db_session):
    """Test admin user creation endpoint"""
    unique_id = str(uuid.uuid4())[:8]
    new_user_data = {
        "email": f"newuser_{unique_id}@example.com",
        "password": "Password123!",
        "full_name": "New Test User",
        "role": "user",
        "is_active": True,
        "is_superuser": False,
    }

    response = client.post("/api/admin/users", headers=admin_token_headers, json=new_user_data)
    assert response.status_code == 200, f"Create user failed: {response.json()}"
    user_data = response.json()

    # Check that the user was created properly
    assert user_data["email"] == new_user_data["email"]
    assert user_data["full_name"] == new_user_data["full_name"]
    assert user_data["role"] == new_user_data["role"]

    # Verify user exists in the database
    from app.models.user import User

    db_user = db_session.query(User).filter(User.email == new_user_data["email"]).first()
    assert db_user is not None
    assert db_user.email == new_user_data["email"]


def test_admin_users_delete(client, admin_token_headers, normal_user, db_session):
    """Deleting a user with real owned data removes ALL of it.

    This test used to delete the bare ``normal_user`` fixture, which owns nothing.
    ``_delete_user_owned_records`` and ``_delete_user_media_files`` are hand-maintained
    lists of the FKs with no DB-level CASCADE, and every branch in them reads
    ``if <ids>:`` — so with no owned rows every branch was skipped, ``db.delete(user)``
    succeeded trivially, and the test would still have passed with both helpers'
    bodies replaced by ``pass``.
    """
    from app.models.user import User

    owned = seed_owned_rows(db_session, normal_user)
    owned.assert_all_present(db_session)

    response = client.delete(f"/api/admin/users/{normal_user.uuid}", headers=admin_token_headers)
    assert response.status_code == status.HTTP_200_OK, response.text
    assert response.json() == {"message": "User deleted successfully"}

    db_session.expire_all()
    assert db_session.query(User).filter(User.id == owned.user_id).first() is None
    assert owned.remaining(db_session) == {}


def test_admin_users_delete_leaves_the_bystanders_data_alone(
    client, admin_token_headers, normal_user, db_session
):
    """The other half of the cascade: only the target's data goes.

    ``_delete_user_media_files`` now deletes comments and tasks by ``media_file_id``
    rather than by author, which is what lets a collaborator's comment go with the
    file. That widening is only correct if it stops at the file boundary — a sweep
    keyed on the wrong column would take the bystander's own file with it.
    """
    from app.models.media import MediaFile
    from app.models.user import User

    owned = seed_owned_rows(db_session, normal_user)

    response = client.delete(f"/api/admin/users/{normal_user.uuid}", headers=admin_token_headers)
    assert response.status_code == status.HTTP_200_OK, response.text

    db_session.expire_all()
    assert db_session.query(User).filter(User.id == owned.other_user_id).first() is not None
    assert (
        db_session.query(MediaFile).filter(MediaFile.id == owned.other_media_file_id).first()
        is not None
    )


def test_admin_can_delete_an_admin_who_changed_auth_config(
    client, super_admin_token_headers, admin_user, db_session
):
    """``auth_config_audit.changed_by`` must not pin an account in place.

    It was ``NOT NULL`` with ``ON DELETE NO ACTION``, so deleting any admin who had
    ever changed authentication configuration raised a ``ForeignKeyViolation`` that the
    endpoint's blanket ``except Exception`` turned into ``500 "User deletion failed"``.
    Nothing in the message said which constraint, and the account was the one most
    likely to be deleted: the admin who set up OIDC and then left. v387 makes the FK
    ``SET NULL``; the audit row survives, de-attributed, which is exactly what the read
    path in ``auth_config.get_audit_log`` was already written to render.
    """
    from app.models.auth_config import AuthConfigAudit
    from app.models.user import User

    audit = AuthConfigAudit(
        uuid=uuid.uuid4(),
        config_key="oidc_enabled",
        old_value="false",
        new_value="true",
        changed_by=admin_user.id,
        change_type="update",
    )
    db_session.add(audit)
    db_session.commit()
    audit_id = audit.id

    response = client.delete(
        f"/api/admin/users/{admin_user.uuid}", headers=super_admin_token_headers
    )
    assert response.status_code == status.HTTP_200_OK, response.text

    db_session.expire_all()
    assert db_session.query(User).filter(User.id == admin_user.id).first() is None
    surviving = db_session.query(AuthConfigAudit).filter(AuthConfigAudit.id == audit_id).first()
    assert surviving is not None, "the audit trail must outlive the admin it names"
    assert surviving.changed_by is None
    assert surviving.config_key == "oidc_enabled"


def test_admin_can_delete_an_admin_who_quarantined_someone_elses_file(
    client, super_admin_token_headers, admin_user, normal_user, db_session
):
    """``media_file.quarantined_by`` must not pin the reviewing admin in place.

    A takedown deliberately never deletes rows, so the ``media_file`` row it points at
    belongs to a *different* account and survives the admin's deletion. Under
    ``NO ACTION`` that made the takedown reviewer undeletable — and the failure
    surfaced as the same undifferentiated 500.
    """
    from app.models.media import MediaFile
    from app.models.user import User
    from tests.user_owned_rows import make_media_file

    victim_file = make_media_file(db_session, int(normal_user.id))
    victim_file.is_quarantined = True
    victim_file.quarantine_reason = "DMCA notice 1234"
    victim_file.quarantined_by = admin_user.id
    db_session.commit()
    file_id = victim_file.id

    response = client.delete(
        f"/api/admin/users/{admin_user.uuid}", headers=super_admin_token_headers
    )
    assert response.status_code == status.HTTP_200_OK, response.text

    db_session.expire_all()
    assert db_session.query(User).filter(User.id == admin_user.id).first() is None
    still_quarantined = db_session.query(MediaFile).filter(MediaFile.id == file_id).first()
    assert still_quarantined is not None, "a takedown never deletes the file"
    assert still_quarantined.is_quarantined is True
    assert still_quarantined.quarantined_by is None


def test_admin_can_delete_an_admin_who_shared_someone_elses_prompt(
    client, super_admin_token_headers, admin_user, normal_user, db_session
):
    """``summary_prompt.shared_by`` must not pin the sharing admin in place.

    ``prompts.share_prompt`` accepts the owner **or** an admin, and records the actor
    in ``shared_by`` — a column on a row the owner-scoped sweep (``user_id ==``) never
    matches, because the prompt belongs to somebody else.
    """
    from app.models.prompt import SummaryPrompt
    from app.models.user import User

    prompt = SummaryPrompt(
        uuid=uuid.uuid4(),
        name=f"shared-{uuid.uuid4().hex[:8]}",
        prompt_text="summarise",
        user_id=normal_user.id,
        is_shared=True,
        shared_by=admin_user.id,
    )
    db_session.add(prompt)
    db_session.commit()
    prompt_id = prompt.id

    response = client.delete(
        f"/api/admin/users/{admin_user.uuid}", headers=super_admin_token_headers
    )
    assert response.status_code == status.HTTP_200_OK, response.text

    db_session.expire_all()
    assert db_session.query(User).filter(User.id == admin_user.id).first() is None
    surviving = db_session.query(SummaryPrompt).filter(SummaryPrompt.id == prompt_id).first()
    assert surviving is not None, "the owner's prompt is not the sharer's to destroy"
    assert surviving.shared_by is None
    assert surviving.is_shared is True
