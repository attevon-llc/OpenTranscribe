"""Tests for ``app/schemas/speaker_attribute_settings.py`` (issue #474).

Three plain Pydantic models with no custom validators — the behavior worth pinning is
the default values (they are the effective system behavior wherever the settings row is
absent), that ``*Update`` truly treats every field as optional/omittable (partial-update
semantics), and that ``*SystemDefaults`` genuinely requires every field (it is meant to
be the authoritative fully-populated default, not another partial shape). No DB/network
involved, so these are plain construction + ``model_dump``/validation-error assertions.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.speaker_attribute_settings import SpeakerAttributeSettings
from app.schemas.speaker_attribute_settings import SpeakerAttributeSettingsUpdate
from app.schemas.speaker_attribute_settings import SpeakerAttributeSystemDefaults


class TestSpeakerAttributeSettings:
    def test_defaults_are_all_true(self):
        settings = SpeakerAttributeSettings()
        assert settings.model_dump() == {
            "detection_enabled": True,
            "gender_detection_enabled": True,
            "age_detection_enabled": True,
            "show_attributes_on_cards": True,
        }

    def test_explicit_values_override_defaults(self):
        settings = SpeakerAttributeSettings(
            detection_enabled=False,
            gender_detection_enabled=False,
            age_detection_enabled=True,
            show_attributes_on_cards=False,
        )
        assert settings.detection_enabled is False
        assert settings.gender_detection_enabled is False
        assert settings.age_detection_enabled is True
        assert settings.show_attributes_on_cards is False

    def test_non_boolean_value_is_rejected(self):
        with pytest.raises(ValidationError):
            SpeakerAttributeSettings(detection_enabled="not-a-bool")


class TestSpeakerAttributeSettingsUpdate:
    def test_all_fields_default_to_none_for_partial_updates(self):
        update = SpeakerAttributeSettingsUpdate()
        assert update.model_dump() == {
            "detection_enabled": None,
            "gender_detection_enabled": None,
            "age_detection_enabled": None,
            "show_attributes_on_cards": None,
        }

    def test_setting_a_single_field_leaves_the_others_none(self):
        update = SpeakerAttributeSettingsUpdate(gender_detection_enabled=False)
        assert update.gender_detection_enabled is False
        assert update.detection_enabled is None
        assert update.age_detection_enabled is None
        assert update.show_attributes_on_cards is None

    def test_exclude_unset_only_carries_fields_actually_provided(self):
        # This is the shape a PATCH-style endpoint relies on: exclude_unset=True must
        # distinguish "the caller sent False" from "the caller sent nothing".
        update = SpeakerAttributeSettingsUpdate(detection_enabled=False)
        assert update.model_dump(exclude_unset=True) == {"detection_enabled": False}

    def test_non_boolean_value_is_rejected(self):
        with pytest.raises(ValidationError):
            SpeakerAttributeSettingsUpdate(age_detection_enabled="maybe")


class TestSpeakerAttributeSystemDefaults:
    def test_requires_every_field_no_defaults(self):
        with pytest.raises(ValidationError) as excinfo:
            SpeakerAttributeSystemDefaults()
        missing = {err["loc"][0] for err in excinfo.value.errors()}
        assert missing == {
            "detection_enabled",
            "gender_detection_enabled",
            "age_detection_enabled",
            "show_attributes_on_cards",
        }

    def test_constructs_when_all_fields_are_supplied(self):
        defaults = SpeakerAttributeSystemDefaults(
            detection_enabled=True,
            gender_detection_enabled=False,
            age_detection_enabled=True,
            show_attributes_on_cards=False,
        )
        assert defaults.model_dump() == {
            "detection_enabled": True,
            "gender_detection_enabled": False,
            "age_detection_enabled": True,
            "show_attributes_on_cards": False,
        }

    def test_missing_a_single_field_still_raises(self):
        with pytest.raises(ValidationError) as excinfo:
            SpeakerAttributeSystemDefaults(
                detection_enabled=True,
                gender_detection_enabled=True,
                age_detection_enabled=True,
                # show_attributes_on_cards omitted
            )
        missing = {err["loc"][0] for err in excinfo.value.errors()}
        assert missing == {"show_attributes_on_cards"}
