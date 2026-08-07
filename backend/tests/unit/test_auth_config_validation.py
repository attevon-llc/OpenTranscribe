"""The auth-config write path validates; the read path does not fail open.

``PUT /admin/auth-config/{category}`` took a bare ``dict[str, Any]`` and
``bulk_update_category`` wrote every key verbatim. Three consequences, all pinned
here:

* a typo'd key (``keycloak_verify_audiance``) was stored forever and read by
  nothing, because ``CONFIG_CATEGORIES`` was never consulted on write;
* ``_convert_value`` read ANY unrecognised string as ``False``, so a malformed
  ``keycloak_verify_issuer`` / ``keycloak_verify_audience`` / ``ldap_use_ssl``
  silently turned that security control OFF — failing open because a string did
  not parse;
* nothing bounded the numbers, and a key longer than ``VARCHAR(100)`` reached
  Postgres and came back as a generic 500.

``GET /audit/{category}`` had no category check at all and ``get_audit_log``
skipped its filter for an unrecognised one, so ``/audit/anything`` returned the
entire audit log; ``limit`` had no ceiling.

Finally, eight keys were listed under two categories each. ``config_key`` is
globally UNIQUE, so such a key stuck to whichever tab wrote it first and went
missing from the other tab's GET.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest
from fastapi import HTTPException

from app.api.endpoints import auth_config as endpoint
from app.schemas.auth_config import CATEGORY_SCHEMAS
from app.schemas.auth_config import MAX_CONFIG_KEY_LENGTH
from app.schemas.auth_config import coded_default
from app.schemas.auth_config import validate_category_config
from app.services.auth_config_service import MAX_AUDIT_LOG_LIMIT
from app.services.auth_config_service import AuthConfigService


# --------------------------------------------------------------------------- #
# Fakes — these tests never touch a database.
# --------------------------------------------------------------------------- #
class _FakeQuery:
    """Records the pagination the service asked for and returns nothing."""

    def __init__(self, first_result: Any = None):
        self._first_result = first_result
        self.filtered = False
        self.limit_used: int | None = None
        self.offset_used: int | None = None

    def filter(self, *args, **kwargs) -> _FakeQuery:
        self.filtered = True
        return self

    def first(self) -> Any:
        return self._first_result

    def order_by(self, *args) -> _FakeQuery:
        return self

    def offset(self, value: int) -> _FakeQuery:
        self.offset_used = value
        return self

    def limit(self, value: int) -> _FakeQuery:
        self.limit_used = value
        return self

    def all(self) -> list:
        return []


class _FakeSession:
    def __init__(self, first_result: Any = None):
        self.query_obj = _FakeQuery(first_result)
        self.added: list[Any] = []
        self.committed = False
        self.rolled_back = False

    def query(self, *args, **kwargs) -> _FakeQuery:
        return self.query_obj

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    def commit(self) -> None:
        self.committed = True

    def refresh(self, obj: Any) -> None:
        pass

    def rollback(self) -> None:
        self.rolled_back = True


class _FakeRow:
    """Stand-in for an ``AuthConfig`` row already in the table."""

    def __init__(self, config_key: str, category: str, config_value: str | None = "old"):
        self.config_key = config_key
        self.category = category
        self.config_value = config_value
        self.is_sensitive = False
        self.data_type = "string"
        self.description = None
        self.updated_by = None
        self.updated_at = None


class _FakeUser:
    id = 1
    email = "super@example.com"


@pytest.fixture
def no_writes(monkeypatch):
    """Capture what ``bulk_update_category`` would have written."""
    written: dict[str, Any] = {}

    def _fake_set_config(*, db, key, value, **kwargs):
        written[key] = value
        return object()

    monkeypatch.setattr(AuthConfigService, "set_config", staticmethod(_fake_set_config))
    return written


# --------------------------------------------------------------------------- #
# Defect 1a — unknown keys
# --------------------------------------------------------------------------- #
class TestUnknownKeysAreRejected:
    def test_typo_key_is_rejected_and_named(self):
        with pytest.raises(ValueError) as exc:
            validate_category_config("keycloak", {"keycloak_verify_audiance": True})

        assert "keycloak_verify_audiance" in str(exc.value)

    def test_bulk_update_writes_nothing_when_a_key_is_unknown(self, no_writes):
        """The whole payload is refused — not partially applied."""
        with pytest.raises(ValueError):
            AuthConfigService.bulk_update_category(
                db=None,
                category="keycloak",
                config_dict={
                    "keycloak_realm": "opentranscribe",
                    "keycloak_verify_audiance": True,
                },
                user_id=1,
            )

        assert no_writes == {}, "nothing may be stored when any key is rejected"

    def test_key_from_another_category_is_rejected(self):
        """``config_key`` is global, but each key belongs to exactly one tab."""
        with pytest.raises(ValueError):
            validate_category_config("ldap", {"keycloak_client_secret": "x"})

    def test_known_keys_pass_and_come_back_typed(self):
        cleaned = validate_category_config(
            "keycloak", {"keycloak_verify_issuer": "true", "keycloak_timeout": "45"}
        )

        assert cleaned == {"keycloak_verify_issuer": True, "keycloak_timeout": 45}

    def test_only_the_submitted_keys_are_returned(self):
        """Saving one field must not rewrite the rest of the tab with defaults."""
        cleaned = validate_category_config("mfa", {"mfa_issuer_name": "OpenTranscribe-Test"})

        assert list(cleaned) == ["mfa_issuer_name"]


# --------------------------------------------------------------------------- #
# Defect 1b — booleans must not fail open
# --------------------------------------------------------------------------- #
class TestBooleansNeverSilentlyBecomeFalse:
    @pytest.mark.parametrize(
        "key", ["keycloak_verify_issuer", "keycloak_verify_audience", "keycloak_use_pkce"]
    )
    def test_malformed_boolean_is_rejected_on_write(self, key):
        with pytest.raises(ValueError) as exc:
            validate_category_config("keycloak", {key: "yes please"})

        assert key in str(exc.value)

    def test_malformed_boolean_never_reaches_storage(self, no_writes):
        with pytest.raises(ValueError):
            AuthConfigService.bulk_update_category(
                db=None,
                category="keycloak",
                config_dict={"keycloak_verify_issuer": "yes please"},
                user_id=1,
            )

        assert "keycloak_verify_issuer" not in no_writes
        assert no_writes.get("keycloak_verify_issuer") is not False, (
            "a value that failed to parse must not be stored as False — that is "
            "issuer validation silently switched off"
        )

    def test_malformed_stored_boolean_reads_back_as_the_secure_default(self):
        """The defect verbatim: 'yes please' must not read as False.

        A row can still hold garbage (written before this validation existed, or
        migrated from a bad .env), so the read path fails CLOSED to the declared
        default instead of to the zero value.
        """
        assert (
            AuthConfigService._convert_value("yes please", "bool", "keycloak_verify_issuer") is True
        )

    @pytest.mark.parametrize(
        "key", ["keycloak_verify_audience", "keycloak_verify_issuer", "ldap_use_ssl"]
    )
    def test_security_controls_stay_on_when_the_stored_value_is_garbage(self, key):
        assert coded_default(key) is True
        assert AuthConfigService._convert_value("<garbage>", "bool", key) is True

    def test_ldap_use_ssl_is_rejected_on_write_too(self):
        with pytest.raises(ValueError):
            validate_category_config("ldap", {"ldap_use_ssl": "maybe"})

    @pytest.mark.parametrize("spelling", ["true", "TRUE", "1", "yes", "on"])
    def test_documented_truthy_spellings_are_accepted(self, spelling):
        assert validate_category_config("ldap", {"ldap_use_ssl": spelling}) == {
            "ldap_use_ssl": True
        }

    @pytest.mark.parametrize("spelling", ["false", "FALSE", "0", "no", "off"])
    def test_documented_falsy_spellings_are_accepted(self, spelling):
        assert validate_category_config("ldap", {"ldap_use_ssl": spelling}) == {
            "ldap_use_ssl": False
        }

    def test_unknown_key_still_falls_back_to_the_generic_zero_value(self):
        """No key means no declared default; keep the old, documented behaviour."""
        assert AuthConfigService._convert_value("anything", "bool") is False


# --------------------------------------------------------------------------- #
# Defect 1c — numeric bounds
# --------------------------------------------------------------------------- #
class TestNumericBounds:
    @pytest.mark.parametrize(
        ("category", "key", "value"),
        [
            ("ldap", "ldap_port", 0),
            ("ldap", "ldap_port", 65536),
            ("ldap", "ldap_timeout", 0),
            ("keycloak", "keycloak_timeout", -1),
            ("password_policy", "password_min_length", 7),
            ("mfa", "mfa_backup_code_count", 0),
            ("session", "jwt_access_token_expire_minutes", 0),
            ("session", "session_idle_timeout_minutes", 0),
            ("lockout", "account_lockout_threshold", 0),
            ("lockout", "rate_limit_auth_per_minute", 0),
            ("pki", "pki_ocsp_timeout_seconds", 0),
        ],
    )
    def test_out_of_range_int_is_rejected(self, category, key, value):
        with pytest.raises(ValueError) as exc:
            validate_category_config(category, {key: value})

        assert key in str(exc.value)

    @pytest.mark.parametrize(
        ("category", "key", "value"),
        [
            ("ldap", "ldap_port", 636),
            ("password_policy", "password_min_length", 8),
            ("lockout", "account_lockout_threshold", 1),
            ("session", "max_concurrent_sessions", 0),  # 0 = unlimited
            ("password_policy", "password_max_age_days", 0),  # 0 = never expires
        ],
    )
    def test_in_range_int_is_accepted(self, category, key, value):
        assert validate_category_config(category, {key: value}) == {key: value}

    def test_unparseable_int_is_rejected_not_coerced_to_zero(self, no_writes):
        with pytest.raises(ValueError):
            AuthConfigService.bulk_update_category(
                db=None,
                category="ldap",
                config_dict={"ldap_port": "not-a-number"},
                user_id=1,
            )

        assert no_writes == {}

    def test_free_string_choice_fields_are_constrained(self):
        with pytest.raises(ValueError):
            validate_category_config("pki", {"pki_mode": "whatever"})
        with pytest.raises(ValueError):
            validate_category_config("session", {"concurrent_session_policy": "whatever"})


# --------------------------------------------------------------------------- #
# Defect 1c — over-long key is a 400, not a DataError-shaped 500
# --------------------------------------------------------------------------- #
class TestOverLongKey:
    def test_validator_rejects_a_key_longer_than_the_column(self):
        with pytest.raises(ValueError) as exc:
            validate_category_config("ldap", {"x" * (MAX_CONFIG_KEY_LENGTH + 1): "v"})

        assert str(MAX_CONFIG_KEY_LENGTH) in str(exc.value)

    def test_endpoint_returns_400_not_500(self):
        db = _FakeSession()

        with pytest.raises(HTTPException) as exc:
            endpoint.update_config_category(
                category="ldap",
                config={"y" * 250: "v"},
                request=None,
                db=db,
                current_user=_FakeUser(),
            )

        assert exc.value.status_code == 400
        assert "100-character" in exc.value.detail
        assert db.added == []

    def test_endpoint_returns_400_for_an_unknown_key(self):
        with pytest.raises(HTTPException) as exc:
            endpoint.update_config_category(
                category="keycloak",
                config={"keycloak_verify_audiance": True},
                request=None,
                db=_FakeSession(),
                current_user=_FakeUser(),
            )

        assert exc.value.status_code == 400
        assert "keycloak_verify_audiance" in exc.value.detail


# --------------------------------------------------------------------------- #
# Defect 2 — the audit log leaked every category
# --------------------------------------------------------------------------- #
class TestAuditLogScoping:
    def test_endpoint_rejects_a_bogus_category(self):
        with pytest.raises(HTTPException) as exc:
            endpoint.get_audit_log(
                category="anything",
                limit=100,
                offset=0,
                db=_FakeSession(),
                current_user=_FakeUser(),
            )

        assert exc.value.status_code == 400
        assert exc.value.detail.startswith("Invalid category. Must be one of:")

    def test_service_refuses_an_unknown_category_instead_of_dropping_the_filter(self):
        db = _FakeSession()

        with pytest.raises(ValueError):
            AuthConfigService.get_audit_log(db=db, category="anything")

        assert db.query_obj.filtered is False, (
            "an unknown category must never fall through to an unfiltered query"
        )

    def test_a_known_category_is_always_filtered(self):
        db = _FakeSession()

        AuthConfigService.get_audit_log(db=db, category="mfa")

        assert db.query_obj.filtered is True

    def test_every_category_has_keys_to_filter_on(self):
        """An empty key list is what used to disable the filter."""
        for category, keys in AuthConfigService.CONFIG_CATEGORIES.items():
            assert keys, f"category {category} has no keys"

    def test_limit_ceiling_is_declared_on_the_route(self):
        def _constraints(name: str) -> dict[str, Any]:
            param = inspect.signature(endpoint.get_audit_log).parameters[name].default
            return {
                type(item).__name__.lower(): getattr(item, type(item).__name__.lower())
                for item in param.metadata
            }

        assert _constraints("limit") == {"ge": 1, "le": MAX_AUDIT_LOG_LIMIT}
        assert _constraints("offset")["ge"] == 0

    def test_service_clamps_an_absurd_limit(self):
        db = _FakeSession()

        AuthConfigService.get_audit_log(db=db, category="mfa", limit=10_000_000, offset=-5)

        assert db.query_obj.limit_used == MAX_AUDIT_LOG_LIMIT
        assert db.query_obj.offset_used == 0


# --------------------------------------------------------------------------- #
# Defect 3 — one key, one category
# --------------------------------------------------------------------------- #
class TestCategoriesAreDisjoint:
    def test_each_key_belongs_to_exactly_one_category(self):
        seen: dict[str, str] = {}
        duplicates: dict[str, list[str]] = {}

        for category, keys in AuthConfigService.CONFIG_CATEGORIES.items():
            for key in keys:
                if key in seen:
                    duplicates.setdefault(key, [seen[key]]).append(category)
                else:
                    seen[key] = category

        assert duplicates == {}, (
            "config_key is globally UNIQUE, so a key claimed by two categories "
            "sticks to whichever tab saved it first and vanishes from the other"
        )

    @pytest.mark.parametrize(
        ("key", "category"),
        [
            ("password_min_length", "password_policy"),
            ("password_require_uppercase", "password_policy"),
            ("password_require_lowercase", "password_policy"),
            ("password_require_special", "password_policy"),
            ("password_max_age_days", "password_policy"),
            ("password_history_count", "password_policy"),
            ("mfa_enabled", "mfa"),
            ("mfa_required", "mfa"),
        ],
    )
    def test_shared_keys_moved_to_their_specific_category(self, key, category):
        assert key in AuthConfigService.CONFIG_CATEGORIES[category]
        assert key not in AuthConfigService.CONFIG_CATEGORIES["local"]

    def test_set_config_heals_a_stale_category_on_update(self):
        """An existing row written by the Local tab is corrected in place.

        The UPDATE branch never rewrote ``category``, so rows created before the
        split keep pointing at ``local`` and never show up in
        ``GET /password_policy``, which filters by category.
        """
        row = _FakeRow("password_min_length", category="local")
        db = _FakeSession(first_result=row)

        AuthConfigService.set_config(
            db=db,
            key="password_min_length",
            value=14,
            is_sensitive=False,
            category="password_policy",
            user_id=1,
        )

        assert row.category == "password_policy"
        assert row.config_value == "14"

    def test_set_config_leaves_a_correct_category_alone(self):
        row = _FakeRow("mfa_enabled", category="mfa")
        db = _FakeSession(first_result=row)

        AuthConfigService.set_config(
            db=db,
            key="mfa_enabled",
            value=True,
            is_sensitive=False,
            category="mfa",
            user_id=1,
        )

        assert row.category == "mfa"


# --------------------------------------------------------------------------- #
# allow_registration vs local_enabled
# --------------------------------------------------------------------------- #
class TestSelfRegistrationRequiresLocalLogin:
    """Registration mints ``auth_type='local'`` accounts with a local password.

    With local password login off, every account it creates is one that can never
    sign in — so the combination is refused. The admin UI already warns about it;
    this is the rule that makes the warning true.
    """

    def test_enabling_registration_while_local_login_is_off_is_rejected(self):
        with pytest.raises(ValueError) as exc:
            validate_category_config("local", {"allow_registration": True, "local_enabled": False})

        assert "allow_registration" in str(exc.value)
        assert "local_enabled" in str(exc.value)

    def test_disabling_local_login_while_registration_is_on_is_rejected(self):
        with pytest.raises(ValueError) as exc:
            validate_category_config("local", {"local_enabled": False, "allow_registration": True})

        assert "allow_registration" in str(exc.value)

    def test_partial_payload_cannot_walk_around_the_rule(self):
        """The whole point: the rule sees the RESULTING state, not the payload.

        Sending one field at a time used to be enough to assemble the rejected
        pair, because each request looked valid on its own.
        """
        with pytest.raises(ValueError) as exc:
            validate_category_config(
                "local", {"allow_registration": True}, current={"local_enabled": False}
            )

        assert "local_enabled" in str(exc.value)

        with pytest.raises(ValueError) as exc:
            validate_category_config(
                "local", {"local_enabled": False}, current={"allow_registration": True}
            )

        assert "Turn off allow_registration first" in str(exc.value)

    def test_the_payload_wins_over_the_stored_value(self):
        """Turning both off in one save is fine — the result is coherent."""
        cleaned = validate_category_config(
            "local",
            {"local_enabled": False, "allow_registration": False},
            current={"allow_registration": True},
        )

        assert cleaned == {"local_enabled": False, "allow_registration": False}

    def test_registration_on_with_local_login_on_is_fine(self):
        assert validate_category_config(
            "local", {"allow_registration": True}, current={"local_enabled": True}
        ) == {"allow_registration": True}

    def test_bulk_update_loads_the_missing_half_from_the_effective_config(
        self, monkeypatch, no_writes
    ):
        """``bulk_update_category`` is where the merge happens for a real request."""
        monkeypatch.setattr(
            AuthConfigService,
            "get_effective_config",
            staticmethod(lambda db, key: False if key == "local_enabled" else None),
        )

        with pytest.raises(ValueError):
            AuthConfigService.bulk_update_category(
                db=_FakeSession(),
                category="local",
                config_dict={"allow_registration": True},
                user_id=1,
            )

        assert no_writes == {}

    def test_other_categories_are_unaffected(self, no_writes):
        AuthConfigService.bulk_update_category(
            db=None,
            category="mfa",
            config_dict={"mfa_enabled": True},
            user_id=1,
        )

        assert no_writes == {"mfa_enabled": True}


# --------------------------------------------------------------------------- #
# The schemas are the contract — keep them honest
# --------------------------------------------------------------------------- #
class TestSchemasDriveTheValidation:
    def test_every_category_has_a_schema(self):
        assert set(CATEGORY_SCHEMAS) == set(AuthConfigService.CONFIG_CATEGORIES)
        assert set(CATEGORY_SCHEMAS) == set(endpoint.VALID_CATEGORIES)

    def test_every_field_has_a_default(self):
        """Partial payloads are validated against the full model.

        A required field would make an unrelated single-field save fail with a
        confusing "field required" 400.
        """
        for category, model in CATEGORY_SCHEMAS.items():
            for name, field in model.model_fields.items():
                assert not field.is_required(), f"{category}.{name} has no default"

    def test_declared_types_agree_with_the_service_data_type_mapping(self):
        """``DATA_TYPE_MAPPING`` drives storage/read conversion; keep it in step."""
        expected = {bool: "bool", int: "int", str: "string"}

        for model in CATEGORY_SCHEMAS.values():
            for name, field in model.model_fields.items():
                declared = AuthConfigService.DATA_TYPE_MAPPING.get(name)
                if declared is None:
                    continue
                assert declared == expected.get(field.annotation, "string"), (
                    f"{name}: schema says {field.annotation}, mapping says {declared}"
                )

    @pytest.mark.parametrize(
        ("key", "expected"),
        [
            # core/config.py is the authority for these; the schema mirrors it.
            ("keycloak_verify_audience", True),
            ("keycloak_verify_issuer", True),
            ("keycloak_use_pkce", True),
            ("ldap_use_ssl", True),
            ("ldap_port", 636),
            ("password_min_length", 12),
            ("password_history_count", 24),
            ("password_max_age_days", 60),
            ("account_lockout_threshold", 5),
            ("rate_limit_auth_per_minute", 10),
            ("session_idle_timeout_minutes", 15),
            ("max_concurrent_sessions", 5),
            # Strict revocation checking is the deployed default in config.py;
            # the schema used to say True (soft-fail) and contradict it.
            ("pki_revocation_soft_fail", False),
        ],
    )
    def test_defaults_match_core_config(self, key, expected):
        from app.core.config import settings

        assert coded_default(key) == expected
        env_attr = AuthConfigService.env_var_for(key)
        if hasattr(settings, env_attr) and env_attr != "PKI_REVOCATION_SOFT_FAIL":
            assert getattr(settings, env_attr) == expected, (
                f"{key} disagrees with core/config.py:{env_attr}"
            )

    def test_unknown_category_is_rejected_by_the_validator(self):
        with pytest.raises(ValueError):
            validate_category_config("bogus", {})
