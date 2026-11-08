"""The computed "Legal hold" tag and collection (v0.7.0, legal hold plan section 9).

No tag, collection or membership rows exist for it: ``GET /api/files?held=true`` (and the same
param on a collection's media list) narrows the caller's already-scoped list by
``hold_guard.held_pred()``, ``GET /api/files/metadata-filters`` counts the same scoped set as
``held_count``, and every list item carries a ``hold`` summary from ONE batched
``hold_guard.hold_states`` call per page. Each test pairs its claim with a control: the same file
before the hold, after the release, or from the other tenant.
"""

from __future__ import annotations

import datetime

import pytest
from fastapi import status
from sqlalchemy import event

from app.models.content_hold import ContentHold
from app.models.media import MediaFile
from app.services import content_hold_service as holds

REASON = "Litigation matter 2026-14 (privileged)"
FILES = "/api/files"
FACETS = "/api/files/metadata-filters"


def _hold(gov, *files: MediaFile, org=None) -> ContentHold:
    change = holds.place_file_hold(
        gov.db, org_id=(org or gov.org).id, actor=gov.admin, files=list(files), reason=REASON
    )
    gov.db.commit()
    return change.hold


def _release(gov, hold: ContentHold, org=None) -> None:
    holds.release_hold(
        gov.db, org_id=(org or gov.org).id, actor=gov.admin, hold=hold, reason=REASON
    )
    gov.db.commit()


def _platform_hold(gov, media_file: MediaFile) -> None:
    media_file.legal_hold = True
    gov.db.commit()


def _list(client, path: str = FILES, **params) -> dict[str, dict]:
    response = client.get(path, params={"page_size": 100, **params})
    assert response.status_code == status.HTTP_200_OK, response.text
    return {item["uuid"]: item for item in response.json()["items"]}


def _held_count(client, **params) -> int:
    response = client.get(FACETS, params=params)
    assert response.status_code == status.HTTP_200_OK, response.text
    count = response.json()["held_count"]
    assert isinstance(count, int), f"held_count must be an integer on the wire, got {count!r}"
    return count


def _uuids(*files: MediaFile) -> set[str]:
    return {str(f.uuid) for f in files}


# --- the filter --------------------------------------------------------------------------------


@pytest.mark.parametrize("ownership", ["mine", "shared", "all"])
def test_held_true_lists_exactly_the_held_files_the_caller_already_sees(client, gov, ownership):
    """Per ownership mode: the filter is the visible list intersected with "held", nothing more."""
    own_free, own_org_held, own_platform_held = (gov.file(gov.alice) for _ in range(3))
    bob_free, bob_held = gov.file(gov.bob), gov.file(gov.bob)
    shared = gov.collection(gov.bob)
    gov.put(shared, bob_free, bob_held)
    gov.share(shared, gov.alice, by=gov.bob)
    _hold(gov, own_org_held, bob_held)
    _platform_hold(gov, own_platform_held)

    with gov.acting_as(gov.alice):
        everything = _list(client, ownership=ownership)
        held = _list(client, ownership=ownership, held="true")
        free = _list(client, ownership=ownership, held="false")

    expected_held = {u for u, item in everything.items() if item["hold"]["held"]}
    assert set(held) == expected_held
    assert set(free) == set(everything) - expected_held
    assert held, "control: the scenario must put at least one held file in every mode"
    if ownership == "mine":
        assert set(held) == _uuids(own_org_held, own_platform_held)
        assert set(free) == _uuids(own_free)
    else:
        assert str(bob_held.uuid) in held and str(bob_free.uuid) in free


def test_another_tenants_held_file_is_never_listed_or_counted(client, gov):
    mine = gov.file(gov.alice)
    gov.join(gov.outsider, gov.other_org, role="org:admin")
    theirs = gov.file(gov.outsider, org=gov.other_org)
    alice_elsewhere = gov.file(gov.alice, org=gov.other_org)  # alice's own, wrong tenant
    _hold(gov, mine)
    _hold(gov, theirs, alice_elsewhere, org=gov.other_org)
    _platform_hold(gov, theirs)

    with gov.acting_as(gov.alice):
        for ownership in ("mine", "shared", "all"):
            assert set(_list(client, ownership=ownership, held="true")) <= _uuids(mine)
            expected = 0 if ownership == "shared" else 1
            assert _held_count(client, ownership=ownership) == expected
    with gov.acting_as(gov.outsider, org=gov.other_org):
        assert set(_list(client, ownership="all", held="true")) == _uuids(theirs)
        assert _held_count(client, ownership="all") == 1, "control: the other tenant sees its own"


def test_a_quarantined_held_file_stays_hidden_from_a_non_admin(client, gov):
    visible = gov.file(gov.alice)
    taken_down = gov.file(gov.alice, is_quarantined=True)
    _hold(gov, visible, taken_down)
    _platform_hold(gov, taken_down)

    with gov.acting_as(gov.alice):
        assert set(_list(client, held="true", ownership="all")) == _uuids(visible)
        assert _held_count(client) == 1


def test_a_trashed_held_file_stays_out_of_the_list_and_the_count(client, gov):
    visible = gov.file(gov.alice)
    now = datetime.datetime.now(datetime.UTC)
    trashed = gov.file(
        gov.alice,
        deleted_at=now,
        purge_after=now + datetime.timedelta(days=30),
        trash_origin="user",
    )
    _hold(gov, visible, trashed)

    with gov.acting_as(gov.alice):
        assert set(_list(client, held="true", ownership="all")) == _uuids(visible)
        assert _held_count(client) == 1


def test_the_callers_hidden_files_compose_with_the_held_filter(client, gov):
    shown, hidden = gov.file(gov.alice), gov.file(gov.alice)
    _hold(gov, shown, hidden)
    with gov.acting_as(gov.alice):
        response = client.put(f"/api/files/{hidden.uuid}/hidden")
        assert response.status_code == status.HTTP_204_NO_CONTENT, response.text

        assert set(_list(client, held="true", ownership="all")) == _uuids(shown)
        assert _held_count(client) == 1
        assert set(_list(client, held="true", ownership="all", hidden="only")) == _uuids(hidden)
        assert _held_count(client, hidden="include") == 2


def test_release_removes_the_file_from_the_filter_and_the_count_with_no_other_write(client, gov):
    media_file = gov.file(gov.alice)
    with gov.acting_as(gov.alice):
        assert _held_count(client) == 0, "control: nothing held yet"
    hold = _hold(gov, media_file)
    with gov.acting_as(gov.alice):
        assert set(_list(client, held="true")) == _uuids(media_file)
        assert _held_count(client) == 1
    _release(gov, hold)
    with gov.acting_as(gov.alice):
        assert _list(client, held="true") == {}
        assert _held_count(client) == 0
        assert _list(client)[str(media_file.uuid)]["hold"]["held"] is False


# --- the list summary --------------------------------------------------------------------------


@pytest.mark.parametrize("caller", ["alice", "admin"])
def test_list_items_say_who_holds_and_never_carry_hold_ids(client, gov, caller):
    """The org admin is the one caller the DETAIL page gives hold ids to; a list never does."""
    user = getattr(gov, caller)
    free, by_org, by_platform, by_both = (gov.file(user) for _ in range(4))
    hold = _hold(gov, by_org, by_both)
    _platform_hold(gov, by_platform)
    _platform_hold(gov, by_both)

    with gov.acting_as(user):
        items = _list(client)
        detail = client.get(f"/api/files/{by_org.uuid}")
    assert items[str(free.uuid)]["hold"] == {"held": False, "held_by": [], "hold_uuids": []}
    assert items[str(by_org.uuid)]["hold"]["held_by"] == ["organization"]
    assert items[str(by_platform.uuid)]["hold"]["held_by"] == ["platform"]
    assert items[str(by_both.uuid)]["hold"]["held_by"] == ["platform", "organization"]
    for item in items.values():
        assert item["hold"]["hold_uuids"] == []
        assert REASON not in str(item) and str(hold.uuid) not in str(item)
    if caller == "admin":
        assert detail.status_code == status.HTTP_200_OK, detail.text
        assert detail.json()["hold"]["hold_uuids"] == [str(hold.uuid)], (
            "control: this caller IS given the ids on the detail page"
        )


def test_a_collections_media_list_filters_by_hold_and_carries_the_summary(client, gov):
    collection = gov.collection(gov.alice)
    held, free = gov.file(gov.alice), gov.file(gov.alice)
    outside = gov.file(gov.alice)  # held, but not in this collection
    gov.put(collection, held, free)
    _hold(gov, held, outside)

    path = f"/api/collections/{collection.uuid}/media"
    with gov.acting_as(gov.alice):
        everything = _list(client, path)
        assert set(_list(client, path, held="true")) == _uuids(held)
        assert set(_list(client, path, held="false")) == _uuids(free)
    assert everything[str(held.uuid)]["hold"] == {
        "held": True,
        "held_by": ["organization"],
        "hold_uuids": [],
    }
    assert everything[str(free.uuid)]["hold"]["held"] is False


# --- batching ----------------------------------------------------------------------------------


def _statements_during(gov, fn) -> list[str]:
    seen: list[str] = []
    engine = gov.db.connection().engine

    def _record(conn, cursor, statement, parameters, context, executemany):
        # The harness restarts its SAVEPOINT after the first commit; that is not app work.
        if "SAVEPOINT" not in statement:
            seen.append(statement)

    event.listen(engine, "before_cursor_execute", _record)
    try:
        fn()
    finally:
        event.remove(engine, "before_cursor_execute", _record)
    return seen


@pytest.mark.parametrize("path_kind", ["gallery", "collection"])
def test_the_hold_summary_costs_one_query_per_page_whatever_the_page_size(client, gov, path_kind):
    collection = gov.collection(gov.alice)
    files = [gov.file(gov.alice) for _ in range(100)]
    gov.put(collection, *files)
    _hold(gov, *files[::3])
    path = FILES if path_kind == "gallery" else f"/api/collections/{collection.uuid}/media"

    def _page(size: int) -> list[str]:
        def _call():
            response = client.get(path, params={"page_size": size, "ownership": "mine"})
            assert response.status_code == status.HTTP_200_OK, response.text
            assert len(response.json()["items"]) == size

        with gov.acting_as(gov.alice):
            return _statements_during(gov, _call)

    small, large = _page(5), _page(100)
    hold_reads = [[s for s in run if "content_hold" in s] for run in (small, large)]
    assert [len(r) for r in hold_reads] == [1, 1], hold_reads
    assert len(large) == len(small), "a 100-row page must cost the same statements as 5 rows"
