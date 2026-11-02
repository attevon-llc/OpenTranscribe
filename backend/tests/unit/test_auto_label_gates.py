"""Pins for the branches of ``auto_label_service`` / ``auto_label_settings`` that decide
WHICH tags, collections and files an unattended LLM suggestion may touch.

The tenancy suites exercise the happy path with a fresh service per call. These cover what
they leave open: the per-instance caches are keyed by tenant (one instance serves several
tenants within a task), a similar-named collection of ANOTHER tenant must not veto a create,
a restricted match in a batch is skipped rather than fatal, the confidence/blank-name gates,
and per-user scoping and validation of the persisted settings.
"""

from __future__ import annotations

import uuid

import pytest

from app.models.collection import Collection
from app.models.collection import CollectionMember
from app.models.prompt import UserSetting
from app.models.tag import FileTag
from app.models.tag import Tag
from app.models.topic import TopicSuggestion
from app.models.upload_batch import UploadBatch
from app.services.auto_label_service import AutoLabelService
from app.services.auto_label_settings import get_user_auto_label_settings
from app.services.auto_label_settings import save_user_auto_label_settings


def _u(prefix: str) -> str:
    return f"{prefix} {uuid.uuid4().hex[:8]}"


def _suggestion(gov, media_file, *, tags=(), collections=()) -> TopicSuggestion:
    row = TopicSuggestion(
        media_file_id=media_file.id,
        user_id=media_file.user_id,
        suggested_tags=list(tags),
        suggested_collections=list(collections),
        status="pending",
    )
    gov.db.add(row)
    gov.db.commit()
    gov.db.refresh(row)
    return row


def _mk_tag(db, name: str, user_id: int, organization_id: int | None) -> Tag:
    tag = Tag(
        name=name,
        normalized_name=name.lower(),
        user_id=user_id,
        organization_id=organization_id,
        source="manual",
    )
    db.add(tag)
    db.commit()
    db.refresh(tag)
    return tag


# --------------------------------------------------------------------------- #
# Per-tenant caches: ONE instance, several tenants                             #
# --------------------------------------------------------------------------- #


def test_tag_fuzzy_cache_is_keyed_by_tenant(gov):
    base = _u("Quarterly Planning Session")
    org_tag = _mk_tag(gov.db, base, gov.alice.id, gov.org.id)
    svc = AutoLabelService(gov.db)

    assert svc.find_existing_similar_tag(base + "s", gov.alice.id, gov.org.id) == org_tag
    # Same instance, personal tenant: the org's warm cache must not answer.
    assert svc.find_existing_similar_tag(base + "s", gov.alice.id, None) is None


def test_collection_cache_is_keyed_by_tenant(gov):
    base = _u("Quarterly Planning Session")
    org_coll = gov.collection(gov.alice, name=base)
    svc = AutoLabelService(gov.db)

    assert svc.find_existing_similar_collection(gov.alice.id, base + "s", gov.org.id) == org_coll
    assert svc.find_existing_similar_collection(gov.alice.id, base + "s", None) is None


def test_other_tenants_collection_does_not_veto_a_create(gov):
    """A similar collection of another tenant is not 'unusable' for this file's tenant."""
    name = _u("Roadmap planning")
    gov.collection(gov.admin, name=name)  # org A's, invisible from alice's personal tenant
    personal_file = gov.file(gov.alice, org=None)
    suggestion = _suggestion(gov, personal_file, collections=[{"name": name, "confidence": 0.99}])

    result = AutoLabelService(gov.db).auto_apply_suggestions(
        personal_file, suggestion, gov.alice.id, confidence_threshold=0.5
    )

    assert result["auto_applied_collections"] == [name]
    created = (
        gov.db.query(Collection)
        .join(CollectionMember, CollectionMember.collection_id == Collection.id)
        .filter(CollectionMember.media_file_id == personal_file.id)
        .one()
    )
    assert created.organization_id is None and created.user_id == gov.alice.id


# --------------------------------------------------------------------------- #
# Batch grouping: a restricted match is skipped, not fatal                     #
# --------------------------------------------------------------------------- #


def test_batch_grouping_skips_a_hidden_collection_match(gov):
    hidden = gov.collection(gov.alice, name="Matter 7 review", visibility="restricted")
    batch = UploadBatch(user_id=gov.bob.id, source="upload", file_count=2)
    gov.db.add(batch)
    gov.db.commit()
    files = [gov.file(gov.bob), gov.file(gov.bob)]
    for f in files:
        f.upload_batch_id = batch.id
        _suggestion(gov, f, tags=[{"name": "matter 7 review", "confidence": 0.9}])
    gov.db.commit()
    before = gov.db.query(Collection).filter_by(organization_id=gov.org.id).count()

    result = AutoLabelService(gov.db).group_batch_by_topics(batch.id, gov.bob.id)

    assert result == {"collections_created": 0, "files_grouped": 0}
    assert gov.db.query(Collection).filter_by(organization_id=gov.org.id).count() == before
    assert (
        not gov.db.query(CollectionMember)
        .filter_by(collection_id=hidden.id)
        .filter(CollectionMember.media_file_id.in_([f.id for f in files]))
        .count()
    )
    gov.db.refresh(batch)
    assert batch.grouping_status == "completed"


# --------------------------------------------------------------------------- #
# Confidence / blank-name / switch gates                                       #
# --------------------------------------------------------------------------- #


def test_confidence_gate_is_inclusive_and_blank_names_are_skipped(gov):
    f = gov.file(gov.alice, org=None)
    t_at, t_below, t_none = _u("At Threshold"), _u("Below Threshold"), _u("No Confidence")
    c_at, c_below = _u("Coll At"), _u("Coll Below")
    suggestion = _suggestion(
        gov,
        f,
        tags=[
            {"name": t_at, "confidence": 0.75},
            {"name": t_below, "confidence": 0.7499},
            {"name": t_none},
            {"name": "   ", "confidence": 0.99},
        ],
        collections=[
            {"name": c_at, "confidence": 0.75},
            {"name": c_below, "confidence": 0.7499},
            {"name": "   ", "confidence": 0.99},
        ],
    )

    result = AutoLabelService(gov.db).auto_apply_suggestions(
        f, suggestion, gov.alice.id, confidence_threshold=0.75
    )

    assert result["auto_applied_tags"] == [t_at]
    assert result["skipped_tags"] == [t_below, t_none, "   "]
    assert result["auto_applied_collections"] == [c_at]
    assert result["skipped_collections"] == [c_below, "   "]
    assert gov.db.query(FileTag).filter_by(media_file_id=f.id).count() == 1
    assert gov.db.query(CollectionMember).filter_by(media_file_id=f.id).count() == 1


@pytest.mark.parametrize(("apply_tags", "apply_collections"), [(False, True), (True, False)])
def test_apply_switches_each_gate_their_own_kind(gov, apply_tags, apply_collections):
    f = gov.file(gov.alice, org=None)
    suggestion = _suggestion(
        gov,
        f,
        tags=[{"name": _u("Switch Tag"), "confidence": 0.99}],
        collections=[{"name": _u("Switch Coll"), "confidence": 0.99}],
    )

    result = AutoLabelService(gov.db).auto_apply_suggestions(
        f,
        suggestion,
        gov.alice.id,
        confidence_threshold=0.5,
        apply_tags=apply_tags,
        apply_collections=apply_collections,
    )

    assert bool(result["auto_applied_tags"]) is apply_tags
    assert bool(result["auto_applied_collections"]) is apply_collections
    assert (gov.db.query(FileTag).filter_by(media_file_id=f.id).count() == 1) is apply_tags
    assert (
        gov.db.query(CollectionMember).filter_by(media_file_id=f.id).count() == 1
    ) is apply_collections


# --------------------------------------------------------------------------- #
# Persisted settings: per-user scope and validation                            #
# --------------------------------------------------------------------------- #


def test_settings_are_read_and_written_per_user(gov):
    save_user_auto_label_settings(gov.db, gov.alice.id, {"enabled": False, "tags_enabled": False})

    assert get_user_auto_label_settings(gov.db, gov.alice.id)["enabled"] is False
    bobs = get_user_auto_label_settings(gov.db, gov.bob.id)
    assert bobs["enabled"] is True and bobs["tags_enabled"] is True
    assert not gov.db.query(UserSetting).filter_by(user_id=gov.bob.id).count()

    save_user_auto_label_settings(gov.db, gov.bob.id, {"enabled": True})
    assert get_user_auto_label_settings(gov.db, gov.alice.id)["enabled"] is False


@pytest.mark.parametrize("bad", [-0.01, 1.01, "abc", None])
def test_settings_reject_an_out_of_range_threshold_without_writing(gov, bad):
    with pytest.raises(ValueError, match="confidence_threshold"):
        save_user_auto_label_settings(
            gov.db, gov.alice.id, {"confidence_threshold": bad, "enabled": False}
        )
    assert not gov.db.query(UserSetting).filter_by(user_id=gov.alice.id).count()


@pytest.mark.parametrize("edge", [0.0, 1.0])
def test_settings_accept_the_threshold_range_edges(gov, edge):
    save_user_auto_label_settings(gov.db, gov.alice.id, {"confidence_threshold": edge})
    assert get_user_auto_label_settings(gov.db, gov.alice.id)["confidence_threshold"] == edge


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
        save_user_auto_label_settings(
            gov.db, gov.alice.id, {"confidence_threshold": 0.8, field: bad}
        )
    assert not gov.db.query(UserSetting).filter_by(user_id=gov.alice.id).count()


@pytest.mark.parametrize("field", _BOOL_FIELDS)
@pytest.mark.parametrize("value", [True, False])
def test_settings_store_each_bool_switch(gov, field, value):
    save_user_auto_label_settings(gov.db, gov.alice.id, {field: value})
    assert get_user_auto_label_settings(gov.db, gov.alice.id)[field] is value
