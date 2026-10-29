"""Open self-registration inside the test's savepoint.

``POST /api/auth/register`` reads ``auth_config.allow_registration`` from the database, and
a developer stack's database is long-lived: an admin who closed registration there once
makes every test that registers an account fail with a 403 that has nothing to do with the
code under test. A test that needs the endpoint open says so and states the premise itself,
rather than inheriting whatever the database happens to hold.

Opt in per test or class with ``@pytest.mark.usefixtures("open_registration")``. The write
lands in the ``db_session`` savepoint and is rolled back with it.
"""

from __future__ import annotations

import pytest

from app.services.auth_config_service import AuthConfigService


@pytest.fixture
def open_registration(db_session, super_admin_user) -> None:
    AuthConfigService.bulk_update_category(
        db=db_session,
        category="local",
        config_dict={"allow_registration": True},
        user_id=super_admin_user.id,
    )
