"""User endpoint tests."""


def test_get_users(client, admin_token_headers, normal_user, admin_user):
    """Test listing all users (admin only endpoint)"""
    response = client.get("/api/users", headers=admin_token_headers)
    assert response.status_code == 200
    users = response.json()
    assert isinstance(users, list)
    # With fixtures, we should have at least 2 users
    assert len(users) >= 2

    # Basic schema validation - response uses uuid not id
    assert "uuid" in users[0]
    assert "email" in users[0]
    assert "full_name" in users[0]
    assert "is_active" in users[0]
    assert "role" in users[0]


def test_get_users_unauthorized(client, user_token_headers):
    """Test that regular users cannot list all users"""
    response = client.get("/api/users", headers=user_token_headers)
    assert response.status_code == 403, (
        response.text
    )  # authenticated but not an admin: get_current_admin_user/get_current_active_superuser raise 403


def test_get_current_user(client, user_token_headers, normal_user):
    """Test getting current user info"""
    response = client.get("/api/users/me", headers=user_token_headers)
    assert response.status_code == 200
    user_data = response.json()
    assert user_data["email"] == normal_user.email
    assert user_data["full_name"] == normal_user.full_name
    # Response uses uuid not id
    assert user_data["uuid"] == str(normal_user.uuid)


def test_update_current_user(client, user_token_headers):
    """Test updating current user info"""
    update_data = {"full_name": "Updated Name"}
    response = client.put("/api/users/me", headers=user_token_headers, json=update_data)
    assert response.status_code == 200
    user_data = response.json()
    assert user_data["full_name"] == "Updated Name"


def test_get_user_by_uuid(client, admin_token_headers, normal_user):
    """Test getting user by UUID (admin only)"""
    response = client.get(f"/api/users/{normal_user.uuid}", headers=admin_token_headers)
    assert response.status_code == 200
    user_data = response.json()
    assert user_data["email"] == normal_user.email
    assert user_data["uuid"] == str(normal_user.uuid)


def test_get_user_by_uuid_invalid(client, admin_token_headers):
    """Test getting user with invalid UUID format"""
    response = client.get("/api/users/not-a-valid-uuid", headers=admin_token_headers)
    assert response.status_code == 400
    assert "Invalid UUID format" in response.json()["detail"]


def test_get_user_by_uuid_unauthorized(client, user_token_headers, admin_user):
    """Test that regular users cannot get other users by UUID"""
    response = client.get(f"/api/users/{admin_user.uuid}", headers=user_token_headers)
    assert response.status_code == 403, (
        response.text
    )  # authenticated but not an admin: get_current_admin_user/get_current_active_superuser raise 403


def test_update_user(client, super_admin_token_headers, normal_user):
    """Test updating a user including privileged fields (super_admin only)"""
    update_data = {"full_name": "Admin Updated User", "is_active": False}
    response = client.put(
        f"/api/users/{normal_user.uuid}", headers=super_admin_token_headers, json=update_data
    )
    assert response.status_code == 200
    user_data = response.json()
    assert user_data["full_name"] == "Admin Updated User"
    assert user_data["is_active"] is False


def test_update_user_admin_cannot_set_privileged_fields(client, admin_token_headers, normal_user):
    """Test that regular admin cannot set privileged fields (is_active, role, etc.)"""
    update_data = {"full_name": "Admin Updated Name", "is_active": False}
    response = client.put(
        f"/api/users/{normal_user.uuid}", headers=admin_token_headers, json=update_data
    )
    assert response.status_code == 200
    user_data = response.json()
    assert user_data["full_name"] == "Admin Updated Name"
    assert user_data["is_active"] is True  # is_active stripped for non-super_admin


def test_update_user_sets_account_expiration(client, admin_token_headers, normal_user):
    """account_expires_at (AC-2 time-boxed accounts) is admin-tier, not super_admin-only —
    it is enforced on every request by dependencies.py but, before this write path, had
    no way to ever be set."""
    response = client.put(
        f"/api/users/{normal_user.uuid}",
        headers=admin_token_headers,
        json={"account_expires_at": "2099-01-01T00:00:00"},
    )
    assert response.status_code == 200
    user_data = response.json()
    assert user_data["account_expires_at"] is not None
    assert user_data["account_expires_at"].startswith("2099-01-01")


def test_update_user_clears_account_expiration(client, admin_token_headers, normal_user):
    """`None` clears a previously-set expiration rather than being ignored as unset —
    `exclude_unset=True` means the key must actually be present in the payload."""
    client.put(
        f"/api/users/{normal_user.uuid}",
        headers=admin_token_headers,
        json={"account_expires_at": "2099-01-01T00:00:00"},
    )
    response = client.put(
        f"/api/users/{normal_user.uuid}",
        headers=admin_token_headers,
        json={"account_expires_at": None},
    )
    assert response.status_code == 200
    assert response.json()["account_expires_at"] is None


def test_expired_account_is_refused_on_the_next_request(
    client, admin_token_headers, normal_user, user_token_headers, db_session
):
    """The read half (dependencies.py:_enforce_account_expiry) already enforced this,
    checked per-request rather than at login — this pins that the write half added here
    (setting the column through the admin API) actually reaches it end-to-end."""
    response = client.put(
        f"/api/users/{normal_user.uuid}",
        headers=admin_token_headers,
        json={"account_expires_at": "2020-01-01T00:00:00"},
    )
    assert response.status_code == 200

    response = client.get("/api/users/me", headers=user_token_headers)
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "account_expired"


def test_update_user_invalid_uuid(client, admin_token_headers):
    """Test updating user with invalid UUID format"""
    update_data = {"full_name": "Should Fail"}
    response = client.put(
        "/api/users/not-a-valid-uuid", headers=admin_token_headers, json=update_data
    )
    assert response.status_code == 400
    assert "Invalid UUID format" in response.json()["detail"]


def test_update_user_unauthorized(client, user_token_headers, admin_user):
    """Test that regular users cannot update other users"""
    update_data = {"full_name": "Should Fail"}
    response = client.put(
        f"/api/users/{admin_user.uuid}", headers=user_token_headers, json=update_data
    )
    assert response.status_code == 403, (
        response.text
    )  # authenticated but not an admin: get_current_admin_user/get_current_active_superuser raise 403


def test_delete_user(client, admin_token_headers, normal_user, db_session):
    """Test deleting a user (admin only)"""
    user_uuid = str(normal_user.uuid)
    user_id = normal_user.id
    response = client.delete(f"/api/users/{user_uuid}", headers=admin_token_headers)
    assert response.status_code == 204

    # Verify the user is deleted from the database
    from app.models.user import User

    db_session.expire_all()
    deleted_user = db_session.query(User).filter(User.id == user_id).first()
    assert deleted_user is None


def test_delete_user_invalid_uuid(client, admin_token_headers):
    """Test deleting user with invalid UUID format"""
    response = client.delete("/api/users/not-a-valid-uuid", headers=admin_token_headers)
    assert response.status_code == 400
    assert "Invalid UUID format" in response.json()["detail"]


def test_delete_user_unauthorized(client, user_token_headers, admin_user):
    """Test that regular users cannot delete other users"""
    response = client.delete(f"/api/users/{admin_user.uuid}", headers=user_token_headers)
    assert response.status_code == 403, (
        response.text
    )  # authenticated but not an admin: get_current_admin_user/get_current_active_superuser raise 403
