"""Admin endpoint tests."""

import uuid


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
    """Test admin user deletion endpoint"""
    # Use UUID for the delete endpoint
    user_uuid = str(normal_user.uuid)
    response = client.delete(f"/api/admin/users/{user_uuid}", headers=admin_token_headers)
    assert response.status_code == 200, f"Delete user failed: {response.json()}"

    result = response.json()
    assert "message" in result or "success" in result

    # Verify user was deleted from the database
    from app.models.user import User

    db_session.expire_all()  # Clear cached objects
    db_user = db_session.query(User).filter(User.id == normal_user.id).first()
    assert db_user is None
