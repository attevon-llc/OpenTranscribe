"""What an admin can save for the password policy (PUT /admin/auth-config/password_policy).

Exercises the same validator the endpoint runs (``validate_category_config``), no database.
"""

from __future__ import annotations

import pytest

from app.schemas.auth_config import validate_category_config


def _validate(payload: dict, current: dict | None = None) -> dict:
    return validate_category_config("password_policy", payload, current=current)


class TestTierNames:
    @pytest.mark.parametrize("tier", ["basic", "standard", "hardened", "custom"])
    def test_tiers_are_accepted(self, tier):
        assert _validate({"password_policy_profile": tier}) == {"password_policy_profile": tier}

    @pytest.mark.parametrize(
        ("old", "new"), [("nist", "standard"), ("stig", "hardened"), (" NIST ", "standard")]
    )
    def test_old_names_are_stored_as_their_tier(self, old, new):
        assert _validate({"password_policy_profile": old}) == {"password_policy_profile": new}

    def test_unknown_tier_is_rejected(self):
        with pytest.raises(ValueError, match="password_policy_profile"):
            _validate({"password_policy_profile": "paranoid"})


class TestCustomValues:
    def test_a_lighter_custom_policy_is_valid(self):
        saved = _validate(
            {
                "password_policy_profile": "custom",
                "password_min_length": 8,
                "password_require_uppercase": False,
                "password_require_lowercase": False,
                "password_require_digit": False,
                "password_require_special": False,
                "password_history_count": 0,
                "password_max_age_days": 0,
                "password_min_age_hours": 0,
            }
        )
        assert saved["password_min_length"] == 8
        assert saved["password_max_age_days"] == 0

    @pytest.mark.parametrize(
        "payload",
        [
            {"password_min_length": 7},
            {"password_min_length": 129},
            {"password_max_length": -1},
            {"password_history_count": 101},
            {"password_max_age_days": 3651},
            {"password_min_age_hours": -1},
            {"password_min_age_hours": 9000},
        ],
    )
    def test_out_of_range_values_are_rejected(self, payload):
        with pytest.raises(ValueError, match="password_"):
            _validate(payload)

    def test_maximum_below_minimum_is_rejected(self):
        with pytest.raises(ValueError, match="cannot be below"):
            _validate({"password_min_length": 20, "password_max_length": 12})

    def test_maximum_below_the_stored_minimum_is_rejected_one_field_at_a_time(self):
        with pytest.raises(ValueError, match="cannot be below"):
            _validate({"password_max_length": 10}, current={"password_min_length": 14})

    def test_zero_maximum_means_no_maximum(self):
        assert _validate({"password_min_length": 20, "password_max_length": 0})

    def test_unknown_key_is_rejected(self):
        with pytest.raises(ValueError, match="Unknown"):
            _validate({"password_min_lenght": 12})


class TestScreeningSwitches:
    @pytest.mark.parametrize(
        ("given", "stored"),
        [
            ("", ""),
            (None, ""),
            (True, "true"),
            (False, "false"),
            ("TRUE", "true"),
            ("false", "false"),
        ],
    )
    def test_blocklist_override_is_tri_state(self, given, stored):
        assert _validate({"password_blocklist_enabled": given}) == {
            "password_blocklist_enabled": stored
        }

    def test_blocklist_override_rejects_other_strings(self):
        with pytest.raises(ValueError, match="password_blocklist_enabled"):
            _validate({"password_blocklist_enabled": "sometimes"})

    def test_online_check_is_a_bool_and_off_by_default(self):
        assert _validate({"password_hibp_enabled": True}) == {"password_hibp_enabled": True}
        from app.schemas.auth_config import coded_default

        assert coded_default("password_hibp_enabled") is False
        assert coded_default("password_policy_profile") == "hardened"
        assert coded_default("password_min_age_hours") == 24
