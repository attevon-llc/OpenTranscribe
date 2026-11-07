"""``hold_guard`` and the hold reads know a PLATFORM hold (v467) as well as an organization's.

A platform hold is a ``content_hold`` row with ``authority = 'platform'`` and no organization.
The wire vocabulary does not grow: such a file is ``held_by: ["platform"]``, exactly as a file
held by the takedown's flag is. What differs is who is handed the hold uuid (the release handle):
an organization administrator gets their organization's uuids and never a platform one; an
instance administrator gets the platform's. The reason is never in any of it.

Rows are inserted directly here so the guard is tested without the hold service.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime

import pytest

from app.models.content_hold import AUTHORITY_ORGANIZATION
from app.models.content_hold import AUTHORITY_PLATFORM
from app.models.content_hold import SCOPE_COLLECTION
from app.models.content_hold import SCOPE_FILE
from app.models.content_hold import ContentHold
from app.models.content_hold import ContentHoldFile
from app.services.content_hold_queries import collection_hold_states
from app.services.content_hold_queries import collection_hold_summaries
from app.services.content_hold_queries import file_hold_summary
from app.services.content_hold_queries import list_holds
from app.services.content_hold_queries import list_platform_holds
from app.services.hold_guard import HoldRefusedError
from app.services.hold_guard import HoldState
from app.services.hold_guard import assert_not_held
from app.services.hold_guard import hold_state
from app.services.hold_guard import refusal_detail

REASON = "Preservation demand 2026-77 (privileged)"


def _hold(gov, *files, authority=AUTHORITY_PLATFORM, collection=None) -> ContentHold:
    hold = ContentHold(
        authority=authority,
        organization_id=gov.org.id if authority == AUTHORITY_ORGANIZATION else None,
        scope=SCOPE_COLLECTION if collection is not None else SCOPE_FILE,
        collection_id=collection.id if collection is not None else None,
        reason=REASON,
        placed_by_user_id=gov.admin.id,
    )
    gov.db.add(hold)
    gov.db.flush()
    gov.db.add_all(ContentHoldFile(hold_id=hold.id, media_file_id=f.id) for f in files)
    gov.db.commit()
    return hold


def test_a_platform_hold_is_held_by_the_platform_and_kept_apart_from_organization_uuids(gov):
    platform_only, org_only, both, free = (gov.file(gov.alice) for _ in range(4))
    p = _hold(gov, platform_only, both)
    o = _hold(gov, org_only, both, authority=AUTHORITY_ORGANIZATION)

    state = hold_state(gov.db, platform_only.id)
    assert (state.held, state.held_by) == (True, ["platform"])
    assert (state.platform_flag, state.org_hold_uuids) == (False, ())
    assert state.platform_hold_uuids == (str(p.uuid),)

    mixed = hold_state(gov.db, both.id)
    assert mixed.held_by == ["platform", "organization"]
    assert mixed.org_hold_uuids == (str(o.uuid),)
    assert mixed.platform_hold_uuids == (str(p.uuid),)

    assert hold_state(gov.db, org_only.id).platform_hold_uuids == ()
    assert hold_state(gov.db, free.id).held is False


def test_the_takedown_flag_and_a_platform_hold_report_the_same_word(gov):
    flagged, held = gov.file(gov.alice, legal_hold=True), gov.file(gov.alice)
    _hold(gov, held)
    assert hold_state(gov.db, flagged.id).held_by == hold_state(gov.db, held.id).held_by
    assert hold_state(gov.db, flagged.id).platform_flag is True


def test_a_refusal_hands_each_kind_of_uuid_only_to_its_own_administrator(gov):
    f = gov.file(gov.alice)
    p = _hold(gov, f)
    o = _hold(gov, f, authority=AUTHORITY_ORGANIZATION)
    state = hold_state(gov.db, f.id)

    member = refusal_detail(state, reveal_uuids=False)
    org_admin = refusal_detail(state, reveal_uuids=True)
    operator = refusal_detail(state, reveal_uuids=False, reveal_platform_uuids=True)

    assert "holds" not in member
    assert org_admin["holds"] == [str(o.uuid)]
    assert operator["holds"] == [str(p.uuid)]
    for detail in (member, org_admin, operator):
        assert detail["held_by"] == ["platform", "organization"]
        assert REASON not in str(detail)


def test_a_refused_batch_merges_platform_uuids_too(gov):
    a, b = gov.file(gov.alice), gov.file(gov.alice)
    pa, pb = _hold(gov, a), _hold(gov, b)
    with pytest.raises(HoldRefusedError) as refused:
        assert_not_held(gov.db, [a.id, b.id])
    merged = refused.value.merged()
    assert merged == HoldState(
        platform_flag=False, platform_hold_uuids=(str(pa.uuid), str(pb.uuid))
    )


def test_the_file_badge_reveals_platform_uuids_only_when_asked(gov):
    f = gov.file(gov.alice)
    p = _hold(gov, f)
    assert file_hold_summary(gov.db, f.id, reveal_uuids=True).model_dump() == {
        "held": True,
        "held_by": ["platform"],
        "hold_uuids": [],
    }
    assert file_hold_summary(
        gov.db, f.id, reveal_uuids=False, reveal_platform_uuids=True
    ).hold_uuids == [str(p.uuid)]


def test_a_collection_badge_names_the_authority_that_holds_it(gov):
    by_platform, by_org, by_both = (gov.collection(gov.alice) for _ in range(3))
    p1 = _hold(gov, collection=by_platform)
    o1 = _hold(gov, collection=by_org, authority=AUTHORITY_ORGANIZATION)
    p2 = _hold(gov, collection=by_both)
    o2 = _hold(gov, collection=by_both, authority=AUTHORITY_ORGANIZATION)
    ids = [by_platform.id, by_org.id, by_both.id]

    summaries = collection_hold_summaries(gov.db, ids, reveal_uuids=True)
    assert summaries[by_platform.id].held_by == ["platform"]
    assert summaries[by_platform.id].hold_uuids == [], "an org admin never gets a platform uuid"
    assert summaries[by_org.id].held_by == ["organization"]
    assert summaries[by_org.id].hold_uuids == [str(o1.uuid)]
    assert summaries[by_both.id].held_by == ["platform", "organization"]

    operator = collection_hold_summaries(
        gov.db, ids, reveal_uuids=False, reveal_platform_uuids=True
    )
    assert operator[by_platform.id].hold_uuids == [str(p1.uuid)]
    assert operator[by_both.id].hold_uuids == [str(p2.uuid)]
    assert collection_hold_states(gov.db, [by_both.id])[by_both.id] == HoldState(
        platform_flag=False, org_hold_uuids=(str(o2.uuid),), platform_hold_uuids=(str(p2.uuid),)
    )


def test_the_organization_list_never_shows_a_platform_hold_and_the_platform_list_only_those(gov):
    f = gov.file(gov.alice)
    p = _hold(gov, f)
    o = _hold(gov, f, authority=AUTHORITY_ORGANIZATION)
    released = _hold(gov, f)
    released.released_at = datetime.now(UTC)
    gov.db.commit()

    org_rows, org_total = list_holds(gov.db, gov.org.id, active_only=False)
    assert [h.uuid for h in org_rows] == [o.uuid] and org_total == 1

    active, active_total = list_platform_holds(gov.db, active_only=True)
    assert [h.uuid for h in active] == [p.uuid] and active_total == 1
    everything, total = list_platform_holds(gov.db, active_only=False)
    assert [h.uuid for h in everything] == [released.uuid, p.uuid] and total == 2
    page, _ = list_platform_holds(gov.db, active_only=False, limit=1, offset=1)
    assert [h.uuid for h in page] == [p.uuid]
