"""Pins for the validation of the persisted auto-label switches."""

from __future__ import annotations

import pytest

from app.models.prompt import UserSetting
from app.services.auto_label_service import AutoLabelService

_BOOL_FIELDS = (
    "enabled",
    "tags_enabled",
    "collections_enabled",
    "bulk_grouping_enabled",
    "auto_topic_extraction",
)


@pytest.mark.parametrize("field", _BOOL_FIELDS)
@pytest.mark.parametrize("bad", ["yes", "true", 1, 0, None])
def test_settings_reject_a_non_bool_switch_without_writing(gov, field, bad):
    with pytest.raises(ValueError, match=field):
        AutoLabelService(gov.db).save_user_auto_label_settings(
            gov.alice.id, {"confidence_threshold": 0.8, field: bad}
        )
    assert not gov.db.query(UserSetting).filter_by(user_id=gov.alice.id).count()


@pytest.mark.parametrize("field", _BOOL_FIELDS)
@pytest.mark.parametrize("value", [True, False])
def test_settings_store_each_bool_switch(gov, field, value):
    service = AutoLabelService(gov.db)
    service.save_user_auto_label_settings(gov.alice.id, {field: value})
    assert service.get_user_auto_label_settings(gov.alice.id)[field] is value
