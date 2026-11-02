"""A file only ever carries tags of its own tenant (or system tags).

Legacy data (and any path that skipped the tenant rules) can leave a ``file_tag`` row whose
tag belongs to another tenant than the file: a personal tag on an organization file, or one
organization's tag on another organization's file. Four places now keep those apart:

* the file-aware resolver ignores an attached tag of another tenant;
* the file detail payload lists only tags bound to the request tenant, plus system tags;
* the bulk rail refuses to attach a tag of another tenant (``TAG_TENANT_MISMATCH``);
* ``repair_cross_tenant_file_tags`` repoints stray rows to the file tenant's own tag,
  deleting no tag and keeping every file tagged with the same word.
"""

from __future__ import annotations

import pytest
from fixtures.tag_world import acting_as
from fixtures.tag_world import attach
from fixtures.tag_world import build_org_world
from fixtures.tag_world import make_file
from fixtures.tag_world import make_tag
from fixtures.tag_world import word

from app.models.tag import FileTag
from app.models.tag import Tag
from app.services.tag_bulk import BulkTagOutcome
from app.services.tag_bulk import apply_tag_to_file
from app.services.tag_service import lookup_tag_on_file
from app.services.tag_service import normalize_tag_name
from app.services.tag_service import resolve_or_create_tag


def _tag_id_of(link) -> int:
    """The tag id of an association row; every row these tests read has one."""
    assert link.tag_id is not None
    return int(link.tag_id)


@pytest.fixture
def w(db_session):
    return build_org_world(db_session)


def _tag_ids_on(db, media_file) -> set[int]:
    db.expire_all()
    return {
        _tag_id_of(link)
        for link in db.query(FileTag).filter(FileTag.media_file_id == media_file.id).all()
    }


def _links_on(db, media_file) -> list[FileTag]:
    db.expire_all()
    links: list[FileTag] = (
        db.query(FileTag).filter(FileTag.media_file_id == media_file.id).order_by(FileTag.id).all()
    )
    return links


def repair_cross_tenant_file_tags(db, **kwargs):
    from app.services.tag_tenant_repair import repair_cross_tenant_file_tags as repair

    return repair(db, **kwargs)


def _row(db, tag_id: int) -> Tag | None:
    db.expire_all()
    row: Tag | None = db.query(Tag).filter(Tag.id == tag_id).first()
    return row


# ---------------------------------------------------------------------------
# 1. The file-aware resolver stays inside the tenant it resolves in
# ---------------------------------------------------------------------------


def test_the_on_file_lookup_ignores_another_tenants_tag_on_the_file(w):
    name = word("stray")
    org_file = make_file(w.db, w.alice, w.org_a.id)
    stray = make_tag(w.db, name, user_id=w.alice.id)
    attach(w.db, org_file, stray)

    found = lookup_tag_on_file(w.db, normalize_tag_name(name), org_file.id, w.org_a.id)
    resolved = resolve_or_create_tag(
        w.db, name, user_id=w.bob.id, organization_id=w.org_a.id, file_id=org_file.id
    )

    assert found is None
    assert resolved.id != stray.id
    assert (resolved.organization_id, resolved.user_id) == (w.org_a.id, w.bob.id)


def test_the_on_file_lookup_reuses_the_tenants_own_and_system_tags(w):
    """Control: the reuse the lookup exists for still happens inside the tenant."""
    org_file = make_file(w.db, w.alice, w.org_a.id)
    org_tag = make_tag(w.db, word("org"), user_id=w.alice.id, org_id=w.org_a.id)
    system = make_tag(w.db, word("sys"), user_id=None)
    attach(w.db, org_file, org_tag)
    attach(w.db, org_file, system)

    assert (
        lookup_tag_on_file(w.db, normalize_tag_name(org_tag.name), org_file.id, w.org_a.id)
        == org_tag
    )
    assert (
        lookup_tag_on_file(w.db, normalize_tag_name(system.name), org_file.id, w.org_a.id) == system
    )


def test_the_on_file_lookup_prefers_the_tenant_row_over_a_same_named_system_row(w):
    name = word("both")
    org_file = make_file(w.db, w.alice, w.org_a.id)
    system = make_tag(w.db, name, user_id=None)
    org_tag = make_tag(w.db, name.upper(), user_id=w.alice.id, org_id=w.org_a.id)
    attach(w.db, org_file, system)
    attach(w.db, org_file, org_tag)

    assert lookup_tag_on_file(w.db, normalize_tag_name(name), org_file.id, w.org_a.id) == org_tag


# ---------------------------------------------------------------------------
# 2. The file detail payload
# ---------------------------------------------------------------------------


def test_file_detail_lists_only_the_request_tenants_and_system_tags(client, w):
    org_file = make_file(w.db, w.alice, w.org_a.id)
    own = make_tag(w.db, word("own"), user_id=w.alice.id, org_id=w.org_a.id)
    system = make_tag(w.db, word("system"), user_id=None)
    personal_stray = make_tag(w.db, word("personal"), user_id=w.alice.id)
    other_org_stray = make_tag(w.db, word("other-org"), user_id=w.alice.id, org_id=w.org_b.id)
    for tag in (own, system, personal_stray, other_org_stray):
        attach(w.db, org_file, tag)

    with acting_as(w.alice, w.org_a.id, "org:admin"):
        response = client.get(f"/api/files/{org_file.uuid}")

    assert response.status_code == 200, response.text
    assert {t["uuid"] for t in response.json()["tags"]} == {str(own.uuid), str(system.uuid)}


def test_personal_file_detail_keeps_personal_tags_of_any_owner(client, w):
    """Control: on a personal file every personal tag is in the request tenant."""
    personal_file = make_file(w.db, w.alice, None)
    mine = make_tag(w.db, word("mine"), user_id=w.alice.id)
    bobs = make_tag(w.db, word("bobs"), user_id=w.bob.id)
    org_stray = make_tag(w.db, word("org-stray"), user_id=w.alice.id, org_id=w.org_a.id)
    for tag in (mine, bobs, org_stray):
        attach(w.db, personal_file, tag)

    with acting_as(w.alice, None):
        response = client.get(f"/api/files/{personal_file.uuid}")

    assert response.status_code == 200, response.text
    assert {t["uuid"] for t in response.json()["tags"]} == {str(mine.uuid), str(bobs.uuid)}


# ---------------------------------------------------------------------------
# 3. The write-side guard on the bulk rail
# ---------------------------------------------------------------------------


def test_bulk_attach_refuses_a_tag_of_another_tenant(w):
    org_file = make_file(w.db, w.alice, w.org_a.id)
    personal = make_tag(w.db, word("personal"), user_id=w.alice.id)
    other_org = make_tag(w.db, word("org-b"), user_id=w.alice.id, org_id=w.org_b.id)

    results = [
        apply_tag_to_file(w.db, file_id=org_file.id, tag=tag, add=True, user_id=w.alice.id)
        for tag in (personal, other_org)
    ]

    for result in results:
        assert result.outcome is BulkTagOutcome.FAILED
        assert result.error == "TAG_TENANT_MISMATCH"
        assert result.message == "This tag belongs to a different organization than the file"
    assert _tag_ids_on(w.db, org_file) == set()


def test_bulk_attach_accepts_the_files_own_tenant_and_system_tags(w):
    """Control."""
    org_file = make_file(w.db, w.alice, w.org_a.id)
    personal_file = make_file(w.db, w.alice, None)
    org_tag = make_tag(w.db, word("org"), user_id=w.bob.id, org_id=w.org_a.id)
    system = make_tag(w.db, word("system"), user_id=None)
    bobs_personal = make_tag(w.db, word("bobs"), user_id=w.bob.id)

    outcomes = [
        apply_tag_to_file(w.db, file_id=f.id, tag=t, add=True, user_id=w.alice.id).outcome
        for f, t in (
            (org_file, org_tag),
            (org_file, system),
            (personal_file, system),
            (personal_file, bobs_personal),
        )
    ]

    assert outcomes == [BulkTagOutcome.ADDED] * 4


def test_bulk_detach_may_still_remove_a_stray_association(w):
    """Removing a cross-tenant row only moves the data toward the invariant."""
    org_file = make_file(w.db, w.alice, w.org_a.id)
    stray = make_tag(w.db, word("stray"), user_id=w.alice.id)
    attach(w.db, org_file, stray)

    result = apply_tag_to_file(w.db, file_id=org_file.id, tag=stray, add=False, user_id=w.alice.id)

    assert result.outcome is BulkTagOutcome.REMOVED
    assert _tag_ids_on(w.db, org_file) == set()


# ---------------------------------------------------------------------------
# 4. The repair pass
# ---------------------------------------------------------------------------


def test_repair_repoints_a_personal_tag_on_an_org_file_to_the_orgs_same_named_tag(w):
    name = word("Board")
    org_file = make_file(w.db, w.alice, w.org_a.id)
    stray = make_tag(w.db, name, user_id=w.alice.id)
    org_tag = make_tag(w.db, name, user_id=w.bob.id, org_id=w.org_a.id)
    attach(w.db, org_file, stray, source="auto_ai")

    report = repair_cross_tenant_file_tags(w.db)

    (link,) = _links_on(w.db, org_file)
    assert (link.tag_id, link.source) == (org_tag.id, "auto_ai")
    assert (report.repointed, report.collapsed, report.tags_created) == (1, 0, 0)
    assert report.file_ids == [org_file.id]
    assert _row(w.db, stray.id) is not None, "the repair never deletes a tag"


def test_repair_creates_the_tenants_copy_when_it_has_none(w):
    name = word("Copy")
    org_file = make_file(w.db, w.bob, w.org_b.id)
    stray = make_tag(w.db, name, user_id=w.alice.id, org_id=w.org_a.id, source="ai_accepted")
    attach(w.db, org_file, stray)

    report = repair_cross_tenant_file_tags(w.db)

    (link,) = _links_on(w.db, org_file)
    copy = _row(w.db, _tag_id_of(link))
    assert copy is not None and copy.id != stray.id
    assert (copy.name, copy.source, copy.normalized_name) == (
        stray.name,
        "ai_accepted",
        stray.normalized_name,
    )
    assert (copy.organization_id, copy.user_id) == (w.org_b.id, w.alice.id)
    assert report.tags_created == 1


def test_repair_gives_a_personal_file_the_tag_creators_personal_row(w):
    name = word("Mine")
    personal_file = make_file(w.db, w.bob, None)
    attributed = make_tag(w.db, name, user_id=w.alice.id, org_id=w.org_a.id)
    unattributed = make_tag(w.db, word("Orphan"), user_id=None, org_id=w.org_a.id)
    attach(w.db, personal_file, attributed)
    attach(w.db, personal_file, unattributed)

    repair_cross_tenant_file_tags(w.db)

    rows = [_row(w.db, _tag_id_of(link)) for link in _links_on(w.db, personal_file)]
    owners = {(r.name, r.user_id, r.organization_id) for r in rows if r is not None}
    # An unattributed org tag goes to the file's owner, as v420 phase 2 did.
    assert owners == {(name, w.alice.id, None), (unattributed.name, w.bob.id, None)}


def test_repair_collapses_onto_an_association_the_file_already_has(w):
    """No duplicate row; the kept association takes the more human origin and the confidence."""
    name = word("Dup")
    org_file = make_file(w.db, w.alice, w.org_a.id)
    stray = make_tag(w.db, name, user_id=w.alice.id)
    org_tag = make_tag(w.db, name, user_id=w.alice.id, org_id=w.org_a.id)
    kept = attach(w.db, org_file, org_tag, source="auto_ai")
    kept.ai_confidence = 0.4
    stray_link = attach(w.db, org_file, stray, source="manual")
    stray_link.ai_confidence = 0.9
    w.db.commit()

    report = repair_cross_tenant_file_tags(w.db)

    (link,) = _links_on(w.db, org_file)
    assert (link.tag_id, link.source, link.ai_confidence) == (org_tag.id, "manual", 0.9)
    assert (report.repointed, report.collapsed) == (0, 1)


def test_repair_leaves_rows_already_in_the_files_tenant_alone(w):
    org_file = make_file(w.db, w.alice, w.org_a.id)
    personal_file = make_file(w.db, w.alice, None)
    org_tag = make_tag(w.db, word("org"), user_id=w.alice.id, org_id=w.org_a.id)
    system = make_tag(w.db, word("system"), user_id=None)
    bobs_personal = make_tag(w.db, word("bobs"), user_id=w.bob.id)
    for f, t in ((org_file, org_tag), (org_file, system), (personal_file, bobs_personal)):
        attach(w.db, f, t)
    before = {f.id: _tag_ids_on(w.db, f) for f in (org_file, personal_file)}

    report = repair_cross_tenant_file_tags(w.db)

    assert {f.id: _tag_ids_on(w.db, f) for f in (org_file, personal_file)} == before
    assert (report.repointed, report.collapsed, report.tags_created) == (0, 0, 0)


def test_repair_is_idempotent_and_works_in_small_batches(w):
    name = word("Batch")
    files = [make_file(w.db, w.alice, w.org_a.id) for _ in range(3)]
    stray = make_tag(w.db, name, user_id=w.alice.id)
    for media_file in files:
        attach(w.db, media_file, stray)

    first = repair_cross_tenant_file_tags(w.db, batch_size=1)
    second = repair_cross_tenant_file_tags(w.db, batch_size=1)

    targets = {frozenset(_tag_ids_on(w.db, f)) for f in files}
    assert len(targets) == 1, "every file lands on the one org row"
    assert (first.repointed, first.tags_created) == (3, 1)
    assert (second.repointed, second.collapsed, second.tags_created) == (0, 0, 0)


# ---------------------------------------------------------------------------
# 5. The startup pass that delivers the repair
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("elected", [True, False])
def test_the_startup_pass_runs_the_repair_only_when_elected(monkeypatch, elected):
    import asyncio

    from app.startup import maintenance

    calls: list[str] = []

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(maintenance.asyncio, "sleep", no_wait)
    monkeypatch.setattr("app.utils.boot_once.run_once_per_boot", lambda step: elected)
    monkeypatch.setattr(maintenance, "_repair_tag_tenancy", lambda: calls.append("repair"))

    asyncio.run(maintenance._run_tag_tenant_repair())

    assert calls == (["repair"] if elected else [])
