"""The privacy wire contract (``app.schemas.privacy``) and the vocabularies it must share.

Two vocabularies are declared in the schema module and must equal the ones they mirror, or the
UI offers a value the server rejects (or the reverse): the policy layer names, and the redaction
categories the instance floor can force.
"""

from __future__ import annotations

import typing
from typing import cast

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.schemas import privacy as schemas
from app.services.privacy import policy


def test_layer_names_are_the_policy_modules():
    assert set(typing.get_args(schemas.PrivacyLayer)) == set(typing.get_args(policy.Layer))
    assert set(typing.get_args(schemas.UserLockLayer)) == {"platform", "instance", "organization"}


def test_floor_categories_are_the_ones_the_instance_floor_can_force(monkeypatch):
    from app.services.redaction import config as cfgmod

    settings = {
        "redaction.force_pii": "true",
        "redaction.force_toxicity": "true",
        "redaction.force_profanity": "true",
    }
    monkeypatch.setattr(
        "app.services.system_settings_service.get_settings_map",
        lambda db, keys: settings,
    )
    forced = cfgmod._load_admin_policy(cast(Session, None))["forced_categories"]
    assert set(schemas.ORG_FLOOR_CATEGORIES) == forced


@pytest.mark.parametrize(
    "payload",
    [
        {"forced_categories": ["custom"]},
        {"forced_categories": ["nonsense"]},
        {"forced_pii_entities": ["NOT_AN_ENTITY"]},
        {"forced_custom_words": ["x" * 101]},
        {"forced_custom_words": ["  "]},
        {"forced_custom_words": [f"w{i}" for i in range(201)]},
        {"force_toxicity_threshold": 1.5},
        {"force_toxicity_threshold": -0.1},
        {"forced_categories": None},
        {"force_export_redacted": None},
    ],
)
def test_floor_input_rejects_what_the_server_cannot_use(payload):
    with pytest.raises(ValidationError):
        schemas.RedactionFloorIn(**payload)


def test_floor_input_normalizes_and_remembers_what_was_sent():
    floor = schemas.RedactionFloorIn(
        forced_categories=["pii", "pii"],
        forced_custom_words=[" Alpha ", "alpha", "Beta"],
        force_toxicity_threshold=None,
    )
    assert floor.forced_categories == ["pii"]
    assert floor.forced_custom_words == ["Alpha", "Beta"]
    # an explicit null threshold is "clear it"; an absent field is "leave it"
    assert floor.model_fields_set == {
        "forced_categories",
        "forced_custom_words",
        "force_toxicity_threshold",
    }


@pytest.mark.parametrize(
    "model, payload",
    [
        (schemas.InstancePrivacyPolicyUpdate, {}),
        (schemas.OrgPolicyUpdate, {}),
        (schemas.OrgPolicyUpdate, {"redaction_floor": {}}),
        (schemas.OrgPolicyUpdate, {"redaction_floor": None}),
    ],
)
def test_an_update_that_changes_nothing_is_rejected(model, payload):
    with pytest.raises(ValidationError):
        model(**payload)


@pytest.mark.parametrize("bad", ["no", 0, "false", 1])
def test_switches_are_strict_booleans(bad):
    with pytest.raises(ValidationError):
        schemas.InstancePrivacyPolicyUpdate(voiceprint_matching_enabled=bad)
    with pytest.raises(ValidationError):
        schemas.UserPrivacyPolicyUpdate(voiceprint_matching_enabled=bad)
    with pytest.raises(ValidationError):
        schemas.OrgPolicyUpdate(gender_inference_enabled=bad)


def test_purge_needs_the_literal_confirmation_and_a_target():
    ok = schemas.PurgeRequest(targets=["voiceprints", "voiceprints"], confirm="PURGE")
    assert ok.targets == ["voiceprints"]
    for bad in (
        {"targets": ["voiceprints"], "confirm": "purge"},
        {"targets": ["voiceprints"], "confirm": ""},
        {"targets": ["voiceprints"]},
        {"targets": [], "confirm": "PURGE"},
        {"targets": ["profiles"], "confirm": "PURGE"},
    ):
        with pytest.raises(ValidationError):
            schemas.PurgeRequest(**bad)
    with pytest.raises(ValidationError):
        schemas.AdminPurgeRequest.model_validate(
            {"targets": ["voiceprints"], "confirm": "PURGE"}
        )  # scope is required
