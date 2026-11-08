"""Batched voiceprint decisions (scale foundations plan 5.1, M2 #4).

``GET /api/speaker-profiles/profiles`` asked the voiceprint policy once per (owner,
organization) scope holding a machine-made identity, about four statements each: 980 scopes
cost ~3,900 statements for one ordinary user. The scopes are now decided in one batch
(``policy.voiceprint_decisions``). This is the privacy plane, so the batch must answer exactly
what the one-scope call answers in every policy case; the parity tests below hold the two
together, and the budget test keeps the N+1 from coming back.
"""

from __future__ import annotations

import datetime as dt
import uuid as uuid_pkg

import pytest
from sqlalchemy import text

from app.core.security import get_password_hash
from app.services.privacy import policy
from app.services.privacy.consent import clear_voiceprint_consent_hooks
from app.services.privacy.consent import register_voiceprint_consent_hook
from app.services.privacy.policy import Decision
from app.services.privacy.policy import PrivacyCeiling
from app.services.privacy.redact_view import PrivacyView
from app.services.privacy.redact_view import machine_identity_clauses
from tests.fixtures.privacy_switches import file_in
from tests.fixtures.privacy_switches import make_org
from tests.fixtures.privacy_switches import set_instance_switch
from tests.fixtures.privacy_switches import set_user_switch
from tests.query_budget import count_statements

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_privacy_state():
    """The resolver, the hooks and the backstop cache are process-wide; never leak them."""
    clear_voiceprint_consent_hooks()
    policy.reset_privacy_ceiling_resolver()
    yield
    clear_voiceprint_consent_hooks()
    policy.reset_privacy_ceiling_resolver()


def _user(db, label: str):
    from app.models.user import User

    user = User(
        email=f"{label}_{uuid_pkg.uuid4().hex[:10]}@example.com",
        full_name=label,
        hashed_password=get_password_hash("x" * 12),
        is_active=True,
        role="user",
    )
    db.add(user)
    db.flush()
    return user


def _matrix(db):
    """One scope per policy case. Returns (scopes, the org whose ceiling is off)."""
    from app.models.organization import Organization

    on_user, user_off, org_member, org_user_off = (
        _user(db, n) for n in ("on", "user_off", "org_member", "org_user_off")
    )
    org_on = make_org(db, voiceprints=True)
    org_off = make_org(db, voiceprints=False)
    org_no_row = Organization(name=f"norow-{uuid_pkg.uuid4().hex[:8]}")
    ceiling_org = make_org(db, voiceprints=True)
    db.add(org_no_row)
    db.flush()
    set_user_switch(db, user_off, "voiceprints", False)
    set_user_switch(db, org_user_off, "voiceprints", False)
    scopes = [
        (on_user.id, None),  # personal, everything on
        (user_off.id, None),  # personal, user layer off
        (org_member.id, org_on.id),  # org row present and on
        (org_member.id, org_off.id),  # org layer off
        (org_member.id, org_no_row.id),  # no org row: inherits
        (org_user_off.id, org_on.id),  # user layer off inside an org
        (on_user.id, ceiling_org.id),  # platform ceiling off for this org only
    ]
    return scopes, ceiling_org.id


def _per_scope(db, scopes):
    return {s: policy.voiceprint_decision(db, user_id=s[0], organization_id=s[1]) for s in scopes}


def _ceiling_off_for(org_id):
    return lambda db, org: PrivacyCeiling(voiceprints=False) if org == org_id else None


def test_batch_equals_per_scope_across_every_layer(db_session):
    scopes, ceiling_org = _matrix(db_session)
    policy.set_privacy_ceiling_resolver(_ceiling_off_for(ceiling_org))

    batched = policy.voiceprint_decisions(db_session, scopes)

    assert batched == _per_scope(db_session, scopes)
    # Control: the matrix really exercises every layer, so equality is not two empty answers.
    assert [batched[s] for s in scopes] == [
        Decision(True),
        Decision(False, "user"),
        Decision(True),
        Decision(False, "organization"),
        Decision(True),
        Decision(False, "user"),
        Decision(False, "platform"),
    ]


def test_batch_equals_per_scope_with_the_instance_layer_off(db_session):
    scopes, _ = _matrix(db_session)
    set_instance_switch(db_session, "voiceprints", False)

    batched = policy.voiceprint_decisions(db_session, scopes)

    assert batched == _per_scope(db_session, scopes)
    assert set(batched.values()) == {Decision(False, "instance")}


def test_batch_equals_per_scope_when_the_ceiling_resolver_raises(db_session):
    scopes, _ = _matrix(db_session)

    def boom(db, org):
        raise RuntimeError("platform down")

    policy.set_privacy_ceiling_resolver(boom)

    batched = policy.voiceprint_decisions(db_session, scopes)

    assert batched == _per_scope(db_session, scopes)
    assert set(batched.values()) == {Decision(False, "platform")}


def test_batch_asks_consent_hooks_per_scope_and_matches_per_scope(db_session):
    scopes, _ = _matrix(db_session)
    refused_subject = scopes[2][0]  # the org member, on in two of their scopes
    asked: list[tuple[int, int | None, int | None]] = []

    def hook(db, request):
        asked.append((request.subject_user_id, request.organization_id, request.media_file_id))
        return request.subject_user_id != refused_subject

    register_voiceprint_consent_hook(hook)

    batched = policy.voiceprint_decisions(db_session, scopes)
    batch_asks = sorted(asked, key=repr)
    asked.clear()
    per_scope = _per_scope(db_session, scopes)

    assert batched == per_scope
    assert batch_asks == sorted(asked, key=repr)  # the same create requests, file-less
    assert batched[scopes[2]] == Decision(False, "consent")
    assert batched[scopes[0]] == Decision(True)


@pytest.mark.ddl_exclusive
def test_an_unreadable_store_denies_exactly_the_scopes_a_single_read_denies(db_session):
    """A broken ``organization_policy`` fails every org scope closed, and only those.

    A personal scope never reads that table, so one at a time it still answers from its own
    layers; the batch must not widen the failure to it (nor narrow it for the org scopes).
    """
    scopes, _ = _matrix(db_session)
    db_session.execute(text("DROP TABLE organization_policy"))

    batched = policy.voiceprint_decisions(db_session, scopes)
    gender = policy.gender_decisions(db_session, scopes)

    assert batched == _per_scope(db_session, scopes)
    assert gender == {
        s: policy.gender_decision(db_session, user_id=s[0], organization_id=s[1]) for s in scopes
    }
    org_scopes = [s for s in scopes if s[1] is not None]
    assert {batched[s] for s in org_scopes} == {Decision(False, "instance")}  # fail closed
    assert batched[scopes[0]] == Decision(True)  # personal, on
    assert batched[scopes[1]] == Decision(False, "user")  # personal, its own layer
    assert db_session.execute(text("SELECT 1")).scalar() == 1


def test_a_hook_refusing_one_file_still_hides_that_files_speakers(db_session):
    """Preloading the file-less scope answer must not shadow the per-file consent answer."""
    from sqlalchemy import select

    from app.models.speaker import Speaker

    owner = _user(db_session, "hook_owner")
    allowed_file, refused_file = file_in(db_session, owner, None), file_in(db_session, owner, None)
    for media in (allowed_file, refused_file):
        db_session.add(
            Speaker(
                uuid=uuid_pkg.uuid4(),
                user_id=owner.id,
                media_file_id=media.id,
                name="SPEAKER_00",
                identity_source="voiceprint",
            )
        )
    db_session.flush()
    register_voiceprint_consent_hook(lambda db, request: request.media_file_id != refused_file.id)

    view = PrivacyView(db_session)
    machine_identity_clauses(view, select(Speaker.media_file_id))  # fills the scope memo

    assert view.voiceprints_for_scope(owner.id, None) == Decision(True)
    assert view.voiceprints(refused_file.id) == Decision(False, "consent")
    assert view.voiceprints(allowed_file.id) == Decision(True)


def test_off_scopes_match_personal_and_organization_files_exactly(db_session):
    """The off-scope predicate, grouped per organization, with a NULL (personal) organization.

    ``(user, NULL) IN (...)`` is NULL in SQL, so a personal scope must be matched with
    ``organization_id IS NULL``; and a scope that is off in one organization must not hide the
    same owner's files in another organization or in personal scope.
    """
    from sqlalchemy import select

    from app.models.speaker import Speaker

    user_off, org_member, on_user = (_user(db_session, n) for n in ("u_off", "member", "on"))
    org_off, org_on = make_org(db_session, voiceprints=False), make_org(db_session)
    set_user_switch(db_session, user_off, "voiceprints", False)
    files = {
        "user_off_personal": file_in(db_session, user_off, None),
        "user_off_in_org": file_in(db_session, user_off, org_on),
        "member_in_off_org": file_in(db_session, org_member, org_off),
        "member_personal": file_in(db_session, org_member, None),
        "member_in_on_org": file_in(db_session, org_member, org_on),
        "on_personal": file_in(db_session, on_user, None),
    }
    speakers = {}
    for name, media in files.items():
        speaker = Speaker(
            uuid=uuid_pkg.uuid4(),
            user_id=media.user_id,
            organization_id=media.organization_id,
            media_file_id=media.id,
            name="SPEAKER_00",
            identity_source="voiceprint_link",
        )
        db_session.add(speaker)
        db_session.flush()
        speakers[speaker.id] = name

    _name_hidden, link_hidden = machine_identity_clauses(
        PrivacyView(db_session), select(Speaker.media_file_id).where(Speaker.id.in_(speakers))
    )
    hidden = {
        speakers[sid]
        for (sid,) in db_session.query(Speaker.id).filter(Speaker.id.in_(speakers), link_hidden)
    }

    assert hidden == {"user_off_personal", "user_off_in_org", "member_in_off_org"}


# --- the endpoint budget ---------------------------------------------------------------------


def _shared_voiceprint_profiles(db, member, n, *, off_every: int = 3):
    """``n`` owners sharing one profile each with ``member`` through a voiceprint-made link.

    Every ``off_every``-th owner has voiceprints switched off, so the scope decision is not
    the same answer everywhere and the hidden links reach the counts.
    """
    from app.core import constants
    from app.models.collection import Collection
    from app.models.collection import CollectionMember
    from app.models.media import MediaFile
    from app.models.prompt import UserSetting
    from app.models.sharing import CollectionShare
    from app.models.speaker import Speaker
    from app.models.speaker import SpeakerProfile

    off_names = set()
    for i in range(n):
        owner = _user(db, f"vp_owner_{i}")
        if i % off_every == 0:
            off_names.add(f"VP Profile {owner.id}")
            db.add(
                UserSetting(
                    user_id=owner.id,
                    setting_key=constants.PRIVACY_USER_VOICEPRINT_MATCHING_KEY,
                    setting_value="false",
                )
            )
        profile = SpeakerProfile(
            uuid=uuid_pkg.uuid4(), user_id=owner.id, name=f"VP Profile {owner.id}"
        )
        media = MediaFile(
            uuid=uuid_pkg.uuid4(),
            user_id=owner.id,
            filename=f"vp{i}.mp4",
            title=f"vp{i}",
            storage_path=f"media/test/{uuid_pkg.uuid4()}.mp4",
            content_type="video/mp4",
            file_size=1,
            status="completed",
            upload_time=dt.datetime.now(dt.UTC),
        )
        collection = Collection(user_id=owner.id, name=f"vp{i}", description="")
        db.add_all([profile, media, collection])
        db.flush()
        db.add_all(
            [
                Speaker(
                    uuid=uuid_pkg.uuid4(),
                    user_id=owner.id,
                    media_file_id=media.id,
                    name="SPEAKER_00",
                    profile_id=profile.id,
                    identity_source="voiceprint_link",
                ),
                CollectionMember(collection_id=collection.id, media_file_id=media.id),
                CollectionShare(
                    collection_id=collection.id,
                    shared_by_id=owner.id,
                    target_type="user",
                    target_user_id=member.id,
                    permission="viewer",
                ),
            ]
        )
    db.commit()
    return off_names


def test_profile_list_statements_do_not_grow_with_voiceprint_scopes(
    client, db_session, normal_user, user_token_headers
):
    off_small = _shared_voiceprint_profiles(db_session, normal_user, 2)
    with count_statements(db_session.connection()) as small:
        r2 = client.get("/api/speaker-profiles/profiles", headers=user_token_headers)
    off_large = off_small | _shared_voiceprint_profiles(db_session, normal_user, 10)
    with count_statements(db_session.connection()) as large:
        r12 = client.get("/api/speaker-profiles/profiles", headers=user_token_headers)

    body = r12.json()
    assert (len(r2.json()), len(body)) == (2, 12)  # control: the rows came back
    # Control: the policy really ran per scope; an off owner's voiceprint link is not counted.
    counts = {p["name"]: p["instance_count"] for p in body}
    assert {n for n, c in counts.items() if c == 0} == off_large
    assert all(c == 1 for n, c in counts.items() if n not in off_large)
    assert large.count == small.count, large.describe()
