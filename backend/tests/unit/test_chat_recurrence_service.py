"""W2.5 — the I/O half of cross-meeting recurrence (`aggregation_service.py`).

Real Postgres (`db_session`), real permission plumbing
(`_accessible_scoped_files`/`PermissionService`), and the redaction config
resolver mocked at the SAME seam `test_chat_redactor.py` mocks it — proving
the masking wiring, not re-testing Presidio itself.
"""

from __future__ import annotations

import datetime as dt
import uuid as uuid_pkg
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from app.services.chat import recurrence
from app.services.chat.aggregation_service import answer_recurrence
from app.services.chat.aggregation_service import gather_recurrence_source_items
from app.services.chat.router import route

pytestmark = pytest.mark.unit


@contextmanager
def _one_session(db):
    yield db


def _sf(db):
    return lambda: _one_session(db)


def _make_file(db, user, *, title="Recording", organization_id=None):
    from app.models.media import MediaFile

    media = MediaFile(
        uuid=uuid_pkg.uuid4(),
        user_id=user.id,
        organization_id=organization_id,
        filename=f"{title}.wav",
        title=title,
        storage_path=f"x/{uuid_pkg.uuid4().hex}.wav",
        file_size=1,
        content_type="audio/wav",
        status="completed",
        upload_time=dt.datetime.now(dt.UTC),
    )
    db.add(media)
    db.commit()
    db.refresh(media)
    return media


def _add_fresh_summary(db, media_file, *, action_items=None, keyphrases=None, language="en"):
    from app.models.file_facts import FileFacts

    fingerprint = f"fp-{uuid_pkg.uuid4().hex[:8]}"
    facts = FileFacts(
        media_file_id=media_file.id,
        generator_version="1.1.1",
        source_fingerprint=fingerprint,
        facts={},
        digest={"sections": []},
        keyphrases=keyphrases or {"language": language, "phrases": []},
    )
    db.add(facts)
    media_file.summary_status = "completed"
    media_file.summary_data = {
        "action_items": action_items or [],
        "key_decisions": [],
        "follow_up_items": [],
        "metadata": {"source_fingerprint": fingerprint, "language": language},
    }
    db.add(media_file)
    db.commit()
    return fingerprint


def _add_stale_summary(db, media_file, *, action_items=None, keyphrases=None):
    """A summary whose fingerprint does NOT match file_facts — stale, must be excluded."""
    from app.models.file_facts import FileFacts

    facts = FileFacts(
        media_file_id=media_file.id,
        generator_version="1.1.1",
        source_fingerprint=f"fp-current-{uuid_pkg.uuid4().hex[:8]}",
        facts={},
        digest={"sections": []},
        keyphrases=keyphrases or {"language": "en", "phrases": []},
    )
    db.add(facts)
    media_file.summary_status = "completed"
    media_file.summary_data = {
        "action_items": action_items or [{"item": "Stale item", "owner": "Nobody"}],
        "metadata": {"source_fingerprint": "fp-OLD-does-not-match"},
    }
    db.add(media_file)
    db.commit()


def _add_keyphrases_only(db, media_file, phrases: list[str], *, language="en"):
    """D6: no LLM summary at all — only file_facts.keyphrases (no-LLM tier)."""
    from app.models.file_facts import FileFacts

    facts = FileFacts(
        media_file_id=media_file.id,
        generator_version="1.1.1",
        source_fingerprint=f"fp-{uuid_pkg.uuid4().hex[:8]}",
        facts={},
        digest={"sections": []},
        keyphrases={
            "language": language,
            "phrases": [{"phrase": p, "score": 1.0, "count": 2} for p in phrases],
        },
    )
    db.add(facts)
    # No summary at all — LLM_PROVIDER-empty deployment.
    media_file.summary_status = None
    media_file.summary_data = None
    db.add(media_file)
    db.commit()


def _cfg(*, enabled: bool, redact_before_llm: bool, categories=("pii", "profanity", "custom")):
    """A REAL ``EffectiveRedactionConfig``, not a mock.

    It has to be real: the summary-prose masker forces ``style="label"`` via
    ``dataclasses.replace``, which raises on anything that is not a dataclass
    instance. A ``MagicMock`` stood in here and silently answered every attribute,
    so the test passed without ever exercising that forcing — and the local
    duplicate masker it was written against skipped it entirely.
    """
    from app.services.redaction.config import EffectiveRedactionConfig

    return EffectiveRedactionConfig(
        enabled=enabled,
        enabled_categories=set(categories),
        redact_before_llm=redact_before_llm,
        redact_before_llm_locked=False,
    )


# --------------------------------------------------------------------------- #
# Flag gating
# --------------------------------------------------------------------------- #


def test_answer_recurrence_declines_with_zero_io_when_the_flag_is_off(db_session, normal_user):
    file1 = _make_file(db_session, normal_user, title="A")
    _add_keyphrases_only(db_session, file1, ["budget review"])
    r = route("what keeps coming up across our meetings?", recurrence_enabled=True)

    with patch("app.services.redaction.config.resolve_effective_config") as resolve_mock:
        result = answer_recurrence(
            r,
            session_factory=_sf(db_session),
            user_id=normal_user.id,
            file_uuids=[str(file1.uuid)],
            recurrence_enabled=False,
        )

    assert result is None
    resolve_mock.assert_not_called()


def test_answer_recurrence_declines_for_a_non_recurrence_route(db_session, normal_user):
    r = route("what did we discuss yesterday?", recurrence_enabled=True)

    result = answer_recurrence(
        r,
        session_factory=_sf(db_session),
        user_id=normal_user.id,
        file_uuids=["11111111-1111-1111-1111-111111111111"],
        recurrence_enabled=True,
    )

    assert result is None


# --------------------------------------------------------------------------- #
# Bounded scope only
# --------------------------------------------------------------------------- #


def test_gather_declines_an_unbounded_scope(db_session, normal_user):
    items, coverage = gather_recurrence_source_items(_sf(db_session), normal_user.id, None, None)

    assert items == []
    assert "declined" in coverage


def test_gather_declines_no_session():
    items, coverage = gather_recurrence_source_items(None, 1, None, ["x"])

    assert items == []
    assert "declined" in coverage


# --------------------------------------------------------------------------- #
# D6 — keyphrases work with no LLM summary at all
# --------------------------------------------------------------------------- #


def test_keyphrases_populate_items_with_no_llm_summary(db_session, normal_user):
    file1 = _make_file(db_session, normal_user, title="A")
    _add_keyphrases_only(db_session, file1, ["budget review", "vendor pricing"])

    with patch(
        "app.services.redaction.config.resolve_effective_config",
        return_value=_cfg(enabled=False, redact_before_llm=False),
    ):
        items, coverage = gather_recurrence_source_items(
            _sf(db_session), normal_user.id, None, [str(file1.uuid)]
        )

    assert coverage["masking_failed_files"] == 0
    texts = {i.text for i in items}
    assert "budget review" in texts
    assert "vendor pricing" in texts
    assert all(i.leaf == recurrence.LEAF_KEYPHRASE for i in items)


# --------------------------------------------------------------------------- #
# Stale summaries are excluded, keyphrases still contribute
# --------------------------------------------------------------------------- #


def test_a_stale_summary_is_excluded_but_keyphrases_still_contribute(db_session, normal_user):
    file1 = _make_file(db_session, normal_user, title="A")
    _add_stale_summary(
        db_session,
        file1,
        keyphrases={
            "language": "en",
            "phrases": [{"phrase": "onboarding checklist", "score": 1.0, "count": 2}],
        },
    )

    with patch(
        "app.services.redaction.config.resolve_effective_config",
        return_value=_cfg(enabled=False, redact_before_llm=False),
    ):
        items, _coverage = gather_recurrence_source_items(
            _sf(db_session), normal_user.id, None, [str(file1.uuid)]
        )

    texts = {i.text for i in items}
    assert "Stale item" not in texts
    assert "onboarding checklist" in texts


def test_a_fresh_summary_contributes_action_items_with_owner_and_language(db_session, normal_user):
    file1 = _make_file(db_session, normal_user, title="A")
    _add_fresh_summary(
        db_session,
        file1,
        action_items=[{"item": "Update the roadmap", "owner": "Alice"}],
        language="es",
    )

    with patch(
        "app.services.redaction.config.resolve_effective_config",
        return_value=_cfg(enabled=False, redact_before_llm=False),
    ):
        items, _coverage = gather_recurrence_source_items(
            _sf(db_session), normal_user.id, None, [str(file1.uuid)]
        )

    action_item = next(i for i in items if i.leaf == recurrence.LEAF_ACTION_ITEM)
    assert action_item.text == "Update the roadmap"
    assert action_item.owner == "Alice"
    assert action_item.language == "es"


# --------------------------------------------------------------------------- #
# Masking (issue #402: requester's policy) — applied per file, failure drops
# the WHOLE file
# --------------------------------------------------------------------------- #


def test_masking_is_applied_to_every_item_when_the_policy_requires_it(db_session, normal_user):
    file1 = _make_file(db_session, normal_user, title="A")
    _add_fresh_summary(
        db_session, file1, action_items=[{"item": "Call 555-1234 about the budget", "owner": "Bob"}]
    )

    with (
        patch(
            "app.services.redaction.config.resolve_effective_config",
            return_value=_cfg(enabled=True, redact_before_llm=True),
        ),
        patch(
            "app.services.redaction.service.RedactionService.detect_segment_spans",
            return_value=([], None),
        ),
        patch(
            "app.services.redaction.service.RedactionService.mask_segment",
            return_value=("Call [PHONE] about the budget", []),
        ) as mask_segment,
    ):
        items, coverage = gather_recurrence_source_items(
            _sf(db_session), normal_user.id, None, [str(file1.uuid)]
        )

    assert coverage["masking_failed_files"] == 0
    assert mask_segment.called
    action_item = next(i for i in items if i.leaf == recurrence.LEAF_ACTION_ITEM)
    assert "555-1234" not in action_item.text
    assert action_item.text == "Call [PHONE] about the budget"


def test_a_masking_failure_drops_the_whole_file_and_is_counted(db_session, normal_user):
    """Fail CLOSED: a file whose masking cannot complete contributes NOTHING —
    not a half-masked item — and the drop is counted, never silent."""
    file1 = _make_file(db_session, normal_user, title="A")
    _add_fresh_summary(
        db_session, file1, action_items=[{"item": "Call about the budget", "owner": "Bob"}]
    )
    file2 = _make_file(db_session, normal_user, title="B")
    _add_fresh_summary(
        db_session, file2, action_items=[{"item": "Renew the vendor contract", "owner": "Alice"}]
    )

    def _raise_blocking(*_a, **kwargs):
        failures = kwargs.get("failures")
        if failures is not None:
            failures.append("pii")
        return [], None

    with (
        patch(
            "app.services.redaction.config.resolve_effective_config",
            return_value=_cfg(enabled=True, redact_before_llm=True, categories=("pii",)),
        ),
        patch(
            "app.services.redaction.service.RedactionService.detect_segment_spans",
            side_effect=_raise_blocking,
        ),
    ):
        items, coverage = gather_recurrence_source_items(
            _sf(db_session), normal_user.id, None, [str(file1.uuid), str(file2.uuid)]
        )

    # Both files hit the blocking failure — both dropped whole.
    assert items == []
    assert coverage["masking_failed_files"] == 2


# --------------------------------------------------------------------------- #
# T8 permission pair — LEAK / SHARED, real share rows
# --------------------------------------------------------------------------- #


@pytest.fixture
def org(db_session):
    from app.models.organization import Organization

    organization = Organization(
        uuid=uuid_pkg.uuid4(),
        external_org_id=f"org-w25-{uuid_pkg.uuid4().hex[:8]}",
        name="W2.5 Org",
        is_active=True,
    )
    db_session.add(organization)
    db_session.commit()
    db_session.refresh(organization)
    return organization


def test_leak_a_personal_scope_answer_excludes_the_callers_org_stamped_file(
    db_session, normal_user, org
):
    personal = _make_file(db_session, normal_user, title="Personal", organization_id=None)
    _add_keyphrases_only(db_session, personal, ["personal topic"])

    org_file = _make_file(db_session, normal_user, title="OrgOnly", organization_id=org.id)
    _add_keyphrases_only(db_session, org_file, ["org topic"])

    with patch(
        "app.services.redaction.config.resolve_effective_config",
        return_value=_cfg(enabled=False, redact_before_llm=False),
    ):
        items, _coverage = gather_recurrence_source_items(
            _sf(db_session),
            normal_user.id,
            None,  # personal scope
            [str(personal.uuid), str(org_file.uuid)],
        )

    file_uuids = {i.file_uuid for i in items}
    assert str(org_file.uuid) not in file_uuids, "personal scope leaked the org-stamped file"
    assert str(personal.uuid) in file_uuids


def _share_with(db, owner, recipient, media_file) -> None:
    from app.models.media import Collection
    from app.models.media import CollectionMember
    from app.models.sharing import CollectionShare

    collection = Collection(
        user_id=owner.id, name=f"share-{uuid_pkg.uuid4().hex[:8]}", description="w2.5 test"
    )
    db.add(collection)
    db.commit()
    db.add(CollectionMember(collection_id=collection.id, media_file_id=media_file.id))
    db.add(
        CollectionShare(
            collection_id=collection.id,
            shared_by_id=owner.id,
            target_type="user",
            target_user_id=recipient.id,
            permission="viewer",
        )
    )
    db.commit()


def test_shared_a_group_spanning_owned_and_shared_files_is_reported_not_split(
    db_session, normal_user, other_user
):
    """SHARED: a bounded scope spanning one file the viewer OWNS and one
    genuinely SHARED with them must be reported as ONE recurring group
    across both — asserted non-zero against a real share row, not merely
    "did not raise"."""
    owned = _make_file(db_session, normal_user, title="Mine")
    _add_keyphrases_only(db_session, owned, ["quarterly budget review"])

    shared = _make_file(db_session, other_user, title="Theirs")
    _add_keyphrases_only(db_session, shared, ["quarterly budget review"])
    _share_with(db_session, other_user, normal_user, shared)

    with patch(
        "app.services.redaction.config.resolve_effective_config",
        return_value=_cfg(enabled=False, redact_before_llm=False),
    ):
        items, _coverage = gather_recurrence_source_items(
            _sf(db_session), normal_user.id, None, [str(owned.uuid), str(shared.uuid)]
        )

    file_uuids = {i.file_uuid for i in items}
    assert str(owned.uuid) in file_uuids
    assert str(shared.uuid) in file_uuids, "the genuinely-shared file must contribute"

    result = recurrence.detect_recurring_items(items)
    assert len(result.groups) == 1
    assert set(result.groups[0].file_uuids) == {str(owned.uuid), str(shared.uuid)}
