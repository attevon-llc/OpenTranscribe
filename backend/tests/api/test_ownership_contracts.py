"""Ownership / authorization contract snapshots — THE refactor spec.

For every authorization point in the plan's Appendix A (across MediaFile,
SpeakerProfile, SpeakerCollection, Speaker, CustomVocabulary, SummaryPrompt,
Comment, Collection, Group, WatchSource, UserLLMSettings), this module creates
the minimal resource as ``normal_user`` (via direct ORM inserts that the
savepoint rolls back) and hits the endpoint as ``other_user``, asserting the
EXACT ``(status_code, detail)`` the code currently produces. Admin-bypass cases
listed in Appendix A additionally assert that an admin succeeds (or at least is
not blocked by the ownership gate).

These must pass byte-for-byte through the Phase-4..7 refactors — they are the
characterization spec, not aspirational tests. Where a site's behavior differs
from the Appendix-A inventory (e.g. an upstream permission helper short-circuits
with a different detail string), the test snapshots the REAL current behavior
and a comment explains the discrepancy.

Mutating calls are savepoint-isolated; nothing persists to dev data.

Run: ``venv/bin/pytest tests/api/test_ownership_contracts.py -v -n0``
"""

import uuid as uuid_pkg

from app.models.custom_vocabulary import CustomVocabulary
from app.models.group import UserGroup
from app.models.media import Collection
from app.models.media import Comment
from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import SpeakerCollection
from app.models.media import SpeakerProfile
from app.models.media import TranscriptSegment
from app.models.prompt import SummaryPrompt
from app.models.user_llm_settings import UserLLMSettings
from app.models.watch_source import WatchSource


# --------------------------------------------------------------------------- #
# ORM factory helpers — create resources OWNED BY ``owner`` in the test session
# --------------------------------------------------------------------------- #
def _make_media_file(db, owner) -> MediaFile:
    mf = MediaFile(
        user_id=owner.id,
        filename="owned.wav",
        storage_path=f"test/{uuid_pkg.uuid4().hex}.wav",
        file_size=1024,
        content_type="audio/wav",
    )
    db.add(mf)
    db.commit()
    db.refresh(mf)
    return mf


def _make_speaker(db, owner, media_file) -> Speaker:
    spk = Speaker(
        user_id=owner.id,
        media_file_id=media_file.id,
        name="SPEAKER_00",
    )
    db.add(spk)
    db.commit()
    db.refresh(spk)
    return spk


def _make_segment(db, media_file) -> TranscriptSegment:
    seg = TranscriptSegment(
        media_file_id=media_file.id,
        start_time=0.0,
        end_time=1.0,
        text="hello",
    )
    db.add(seg)
    db.commit()
    db.refresh(seg)
    return seg


def _make_speaker_profile(db, owner) -> SpeakerProfile:
    prof = SpeakerProfile(user_id=owner.id, name=f"Profile {uuid_pkg.uuid4().hex[:6]}")
    db.add(prof)
    db.commit()
    db.refresh(prof)
    return prof


def _make_speaker_collection(db, owner) -> SpeakerCollection:
    col = SpeakerCollection(user_id=owner.id, name=f"SpkCol {uuid_pkg.uuid4().hex[:6]}")
    db.add(col)
    db.commit()
    db.refresh(col)
    return col


def _make_collection(db, owner) -> Collection:
    col = Collection(user_id=owner.id, name=f"Col {uuid_pkg.uuid4().hex[:6]}")
    db.add(col)
    db.commit()
    db.refresh(col)
    return col


def _make_comment(db, owner, media_file) -> Comment:
    com = Comment(user_id=owner.id, media_file_id=media_file.id, text="a comment", timestamp=1.0)
    db.add(com)
    db.commit()
    db.refresh(com)
    return com


def _make_vocab(db, owner) -> CustomVocabulary:
    term = CustomVocabulary(
        user_id=owner.id,
        term=f"term-{uuid_pkg.uuid4().hex[:6]}",
        domain="general",
        is_active=True,
    )
    db.add(term)
    db.commit()
    db.refresh(term)
    return term


def _make_prompt(db, owner, *, is_shared=False, is_system_default=False) -> SummaryPrompt:
    p = SummaryPrompt(
        user_id=owner.id,
        name=f"Prompt {uuid_pkg.uuid4().hex[:6]}",
        prompt_text="Summarize.",
        is_system_default=is_system_default,
        is_active=True,
        is_shared=is_shared,
        content_type="general",
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def _make_group(db, owner) -> UserGroup:
    g = UserGroup(owner_id=owner.id, name=f"Group {uuid_pkg.uuid4().hex[:6]}")
    db.add(g)
    db.commit()
    db.refresh(g)
    return g


def _make_watch_source(db, owner) -> WatchSource:
    ws = WatchSource(
        user_id=owner.id, name=f"Watch {uuid_pkg.uuid4().hex[:6]}", source_type="local"
    )
    db.add(ws)
    db.commit()
    db.refresh(ws)
    return ws


def _make_llm_config(db, owner, *, is_shared=False) -> UserLLMSettings:
    cfg = UserLLMSettings(
        user_id=owner.id,
        name=f"LLM {uuid_pkg.uuid4().hex[:6]}",
        provider="openai",
        model_name="gpt-4o-mini",
        is_shared=is_shared,
    )
    db.add(cfg)
    db.commit()
    db.refresh(cfg)
    return cfg


# --------------------------------------------------------------------------- #
# MediaFile — transcript_segments.py:241-245
# --------------------------------------------------------------------------- #
class TestMediaFileSegmentOwnership:
    def test_segment_speaker_update_403_other_user(
        self, client, db_session, normal_user, other_user, other_user_auth_headers
    ):
        mf = _make_media_file(db_session, normal_user)
        seg = _make_segment(db_session, mf)

        resp = client.put(
            f"/api/transcripts/segments/{seg.uuid}/speaker",
            headers=other_user_auth_headers,
            json={"speaker_uuid": None},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Not authorized to modify this transcript segment"


# --------------------------------------------------------------------------- #
# SpeakerProfile — speaker_profiles.py:239,581,639,706,745 (none bypass)
# --------------------------------------------------------------------------- #
class TestSpeakerProfileOwnership:
    def test_profile_update_403_other_user(
        self, client, db_session, normal_user, other_user, other_user_auth_headers
    ):
        prof = _make_speaker_profile(db_session, normal_user)
        # name/description are query params on this PUT.
        resp = client.put(
            f"/api/speaker-profiles/profiles/{prof.uuid}?name=Renamed",
            headers=other_user_auth_headers,
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Not authorized to access this profile"

    def test_profile_delete_403_other_user(
        self, client, db_session, normal_user, other_user, other_user_auth_headers
    ):
        prof = _make_speaker_profile(db_session, normal_user)
        resp = client.delete(
            f"/api/speaker-profiles/profiles/{prof.uuid}", headers=other_user_auth_headers
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Not authorized to access this profile"

    def test_profile_occurrences_403_other_user(
        self, client, db_session, normal_user, other_user, other_user_auth_headers
    ):
        prof = _make_speaker_profile(db_session, normal_user)
        resp = client.get(
            f"/api/speaker-profiles/profiles/{prof.uuid}/occurrences",
            headers=other_user_auth_headers,
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Not authorized to access this profile"

    def test_profile_delete_avatar_403_other_user(
        self, client, db_session, normal_user, other_user, other_user_auth_headers
    ):
        prof = _make_speaker_profile(db_session, normal_user)
        resp = client.delete(
            f"/api/speaker-profiles/profiles/{prof.uuid}/avatar",
            headers=other_user_auth_headers,
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Not authorized to access this profile"

    def test_profile_confirm_gender_403_other_user(
        self, client, db_session, normal_user, other_user, other_user_auth_headers
    ):
        prof = _make_speaker_profile(db_session, normal_user)
        resp = client.post(
            f"/api/speaker-profiles/profiles/{prof.uuid}/confirm-gender?gender=male",
            headers=other_user_auth_headers,
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Not authorized to access this profile"


# --------------------------------------------------------------------------- #
# SpeakerCollection — speaker_profiles.py:63-65 (list filtered by collection)
# --------------------------------------------------------------------------- #
class TestSpeakerCollectionOwnership:
    def test_list_profiles_filtered_by_other_collection_masked_to_500(
        self, client, db_session, normal_user, other_user, other_user_auth_headers
    ):
        # BUG(app/api/endpoints/speaker_profiles.py): the collection-ownership
        # check at :63-65 DOES raise 403 "Not authorized to access this
        # collection", but list_speaker_profiles wraps the whole body in a bare
        # `except Exception` (:174-176) that swallows the intentional
        # HTTPException and re-raises it as 500 "Internal server error". So the
        # ACTUAL client-visible contract for a foreign-collection filter is a
        # 500, not the 403 the inner code intends. Snapshotting the real current
        # behavior keeps the suite honest; Phase 6 should narrow that except so
        # HTTPException propagates, at which point this flips to 403 and the
        # assertion below must be updated alongside the fix.
        _make_speaker_profile(db_session, other_user)
        col = _make_speaker_collection(db_session, normal_user)
        resp = client.get(
            f"/api/speaker-profiles/profiles?collection_uuid={col.uuid}",
            headers=other_user_auth_headers,
        )
        assert resp.status_code == 500
        assert resp.json()["detail"] == "Internal server error"

    def test_list_profiles_no_accessible_profiles_returns_empty(
        self, client, db_session, normal_user, other_user, other_user_auth_headers
    ):
        """Pin the early-return branch: a caller with no profiles gets [] (200),
        NOT a 403 — the collection check at :63 is never reached. Phase-6 dedup
        must preserve this ordering."""
        col = _make_speaker_collection(db_session, normal_user)
        resp = client.get(
            f"/api/speaker-profiles/profiles?collection_uuid={col.uuid}",
            headers=other_user_auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json() == []


# --------------------------------------------------------------------------- #
# Speaker — speakers.py:1080,1115,1510,1596 (PermissionService fallback; admin bypass)
# Literal detail is "Requires editor permission".
# --------------------------------------------------------------------------- #
class TestSpeakerOwnership:
    def test_speaker_update_403_other_user(
        self, client, db_session, normal_user, other_user, other_user_auth_headers
    ):
        mf = _make_media_file(db_session, normal_user)
        spk = _make_speaker(db_session, normal_user, mf)
        resp = client.put(
            f"/api/speakers/{spk.uuid}",
            headers=other_user_auth_headers,
            json={"display_name": "Hacker"},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Requires editor permission"

    def test_speaker_delete_403_other_user(
        self, client, db_session, normal_user, other_user, other_user_auth_headers
    ):
        mf = _make_media_file(db_session, normal_user)
        spk = _make_speaker(db_session, normal_user, mf)
        resp = client.delete(f"/api/speakers/{spk.uuid}", headers=other_user_auth_headers)
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Requires editor permission"

    def test_speaker_confirm_gender_403_other_user(
        self, client, db_session, normal_user, other_user, other_user_auth_headers
    ):
        mf = _make_media_file(db_session, normal_user)
        spk = _make_speaker(db_session, normal_user, mf)
        resp = client.post(
            f"/api/speakers/{spk.uuid}/confirm-gender?gender=male",
            headers=other_user_auth_headers,
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Requires editor permission"

    def test_speaker_update_admin_bypasses_ownership(
        self, client, db_session, normal_user, admin_user, admin_token_headers
    ):
        """Admin bypass (Appendix A): an admin is not blocked by the ownership gate."""
        mf = _make_media_file(db_session, normal_user)
        spk = _make_speaker(db_session, normal_user, mf)
        resp = client.put(
            f"/api/speakers/{spk.uuid}",
            headers=admin_token_headers,
            json={"display_name": "Admin Edit"},
        )
        # Admin sails past the 403 ownership gate (200 success).
        assert resp.status_code == 200, resp.json()


# --------------------------------------------------------------------------- #
# Speaker (segment ctx) — transcript_segments.py:111-120 (admin bypass).
# NOTE (discrepancy vs Appendix A): the inner speaker-ownership check
# (_get_new_speaker_id, detail "Not authorized to use this speaker") is
# UNREACHABLE through this endpoint. update_segment_speaker first enforces an
# EXACT segment-file owner gate at :241 ("Not authorized to modify this
# transcript segment"), so a non-owner is rejected before the speaker is
# resolved; and for the owner, PermissionService.get_file_permission returns
# "owner" on their own file, so the inner check passes. The 403 at :118-120 is
# therefore defense-in-depth only. We snapshot the EFFECTIVE reachable contract
# (the segment-owner gate) plus the owner's successful self-reassignment.
# --------------------------------------------------------------------------- #
class TestSpeakerSegmentContextOwnership:
    def test_segment_owner_gate_blocks_before_speaker_check(
        self, client, db_session, normal_user, other_user, other_user_auth_headers
    ):
        """A non-owner is stopped by the segment-file owner gate (:241) before the
        inner speaker-ownership branch (:111-120) can run."""
        mf = _make_media_file(db_session, normal_user)
        seg = _make_segment(db_session, mf)
        foreign_speaker = _make_speaker(
            db_session, other_user, _make_media_file(db_session, other_user)
        )

        resp = client.put(
            f"/api/transcripts/segments/{seg.uuid}/speaker",
            headers=other_user_auth_headers,
            json={"speaker_uuid": str(foreign_speaker.uuid)},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Not authorized to modify this transcript segment"

    def test_owner_can_reassign_own_segment_to_own_speaker(
        self, client, db_session, normal_user, user_token_headers
    ):
        """The owner passes both the segment gate and the inner speaker check when
        the speaker is on the same file (no false 403)."""
        mf = _make_media_file(db_session, normal_user)
        seg = _make_segment(db_session, mf)
        spk = _make_speaker(db_session, normal_user, mf)

        resp = client.put(
            f"/api/transcripts/segments/{seg.uuid}/speaker",
            headers=user_token_headers,
            json={"speaker_uuid": str(spk.uuid)},
        )
        assert resp.status_code == 200, resp.json()


# --------------------------------------------------------------------------- #
# CustomVocabulary — custom_vocabulary.py:217-220 / 302-305 (none bypass)
# --------------------------------------------------------------------------- #
class TestCustomVocabularyOwnership:
    def test_vocab_update_403_other_user(
        self, client, db_session, normal_user, other_user, other_user_auth_headers
    ):
        term = _make_vocab(db_session, normal_user)
        resp = client.put(
            f"/api/custom-vocabulary/{term.id}",
            headers=other_user_auth_headers,
            json={"term": "hijacked"},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Not authorized to modify this vocabulary term"

    def test_vocab_delete_403_other_user(
        self, client, db_session, normal_user, other_user, other_user_auth_headers
    ):
        term = _make_vocab(db_session, normal_user)
        resp = client.delete(f"/api/custom-vocabulary/{term.id}", headers=other_user_auth_headers)
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Not authorized to delete this vocabulary term"


# --------------------------------------------------------------------------- #
# SummaryPrompt — prompts.py:473-474,618-619,678-679,698-700,727-729,760-761
# --------------------------------------------------------------------------- #
class TestSummaryPromptOwnership:
    def test_prompt_get_private_403_other_user(
        self, client, db_session, normal_user, other_user, other_user_auth_headers
    ):
        """GET /{uuid} of a private foreign prompt → prompts.py:678-679."""
        p = _make_prompt(db_session, normal_user)
        resp = client.get(f"/api/prompts/{p.uuid}", headers=other_user_auth_headers)
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Not enough permissions"

    def test_prompt_set_active_private_403_other_user(
        self, client, db_session, normal_user, other_user, other_user_auth_headers
    ):
        """POST /active/set on a private foreign prompt → prompts.py:473-474."""
        p = _make_prompt(db_session, normal_user)
        resp = client.post(
            "/api/prompts/active/set",
            headers=other_user_auth_headers,
            json={"prompt_id": str(p.uuid)},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Cannot use other users' private prompts"

    def test_prompt_share_toggle_403_other_user(
        self, client, db_session, normal_user, other_user, other_user_auth_headers
    ):
        """POST /shared/{uuid}/toggle by a non-owner non-admin → prompts.py:618-619."""
        p = _make_prompt(db_session, normal_user)
        resp = client.post(
            f"/api/prompts/shared/{p.uuid}/toggle",
            headers=other_user_auth_headers,
            json={"is_shared": True},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Not authorized to share this prompt"

    def test_prompt_update_403_other_user(
        self, client, db_session, normal_user, other_user, other_user_auth_headers
    ):
        """PUT /{uuid} of a foreign prompt → prompts.py:698-700 (detail captured live)."""
        p = _make_prompt(db_session, normal_user)
        resp = client.put(
            f"/api/prompts/{p.uuid}",
            headers=other_user_auth_headers,
            json={"name": "stolen"},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Cannot modify system prompts or other users' prompts"

    def test_prompt_delete_403_other_user(
        self, client, db_session, normal_user, other_user, other_user_auth_headers
    ):
        """DELETE /{uuid} of a foreign prompt → prompts.py:727-729 (detail captured live)."""
        p = _make_prompt(db_session, normal_user)
        resp = client.delete(f"/api/prompts/{p.uuid}", headers=other_user_auth_headers)
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Cannot delete system prompts or other users' prompts"

    def test_prompt_clone_private_403_other_user(
        self, client, db_session, normal_user, other_user, other_user_auth_headers
    ):
        """POST /{uuid}/clone of a foreign PRIVATE prompt → prompts.py:760-761."""
        p = _make_prompt(db_session, normal_user, is_shared=False)
        resp = client.post(f"/api/prompts/{p.uuid}/clone", headers=other_user_auth_headers)
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Cannot clone other users' private prompts"

    def test_prompt_get_shared_allowed(
        self, client, db_session, normal_user, other_user, other_user_auth_headers
    ):
        """Bypass case: a SHARED foreign prompt is viewable by another user (no 403)."""
        p = _make_prompt(db_session, normal_user, is_shared=True)
        resp = client.get(f"/api/prompts/{p.uuid}", headers=other_user_auth_headers)
        assert resp.status_code == 200


# --------------------------------------------------------------------------- #
# Comment — comments.py:204-208 (admin bypass)
# --------------------------------------------------------------------------- #
class TestCommentOwnership:
    def test_comment_update_403_other_user(
        self, client, db_session, normal_user, other_user, other_user_auth_headers
    ):
        mf = _make_media_file(db_session, normal_user)
        com = _make_comment(db_session, normal_user, mf)
        resp = client.put(
            f"/api/comments/{com.uuid}",
            headers=other_user_auth_headers,
            json={"text": "edited by stranger"},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == "You do not have permission to edit this comment"

    def test_comment_update_admin_bypasses_ownership(
        self, client, db_session, normal_user, admin_user, admin_token_headers
    ):
        """Admin bypass (Appendix A): admin can edit another user's comment."""
        mf = _make_media_file(db_session, normal_user)
        com = _make_comment(db_session, normal_user, mf)
        resp = client.put(
            f"/api/comments/{com.uuid}",
            headers=admin_token_headers,
            json={"text": "edited by admin"},
        )
        assert resp.status_code == 200, resp.json()


# --------------------------------------------------------------------------- #
# Collection — media_collections.py:629-633.
# NOTE (discrepancy vs Appendix A): delete_collection calls
# get_collection_by_uuid_with_sharing(min_permission="owner") FIRST. For a
# complete stranger that helper short-circuits at uuid_helpers.py:397 with
# 403 "Not authorized to access this collection" BEFORE the line-632
# "Only the collection owner can delete it" is reachable. We snapshot the REAL
# behavior. The line-632 detail is therefore effectively dead for non-shared
# foreign users — relevant for the Phase-6 ownership dedup.
# --------------------------------------------------------------------------- #
class TestCollectionOwnership:
    def test_collection_delete_403_other_user(
        self, client, db_session, normal_user, other_user, other_user_auth_headers
    ):
        col = _make_collection(db_session, normal_user)
        resp = client.delete(f"/api/collections/{col.uuid}", headers=other_user_auth_headers)
        assert resp.status_code == 403
        # Real current behavior: the sharing helper rejects first.
        assert resp.json()["detail"] == "Not authorized to access this collection"


# --------------------------------------------------------------------------- #
# Group — groups.py:325-329 (owner_id; none bypass for delete)
# --------------------------------------------------------------------------- #
class TestGroupOwnership:
    def test_group_delete_403_other_user(
        self, client, db_session, normal_user, other_user, other_user_auth_headers
    ):
        g = _make_group(db_session, normal_user)
        resp = client.delete(f"/api/groups/{g.uuid}", headers=other_user_auth_headers)
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Only the group owner can delete this group"


# --------------------------------------------------------------------------- #
# WatchSource — watch_sources.py:157-158 (admin role bypass)
# --------------------------------------------------------------------------- #
class TestWatchSourceOwnership:
    def test_watch_source_get_403_other_user(
        self, client, db_session, normal_user, other_user, other_user_auth_headers
    ):
        ws = _make_watch_source(db_session, normal_user)
        resp = client.get(f"/api/watch-sources/{ws.uuid}", headers=other_user_auth_headers)
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Not authorized for this watch source"

    def test_watch_source_update_403_other_user(
        self, client, db_session, normal_user, other_user, other_user_auth_headers
    ):
        ws = _make_watch_source(db_session, normal_user)
        resp = client.put(
            f"/api/watch-sources/{ws.uuid}",
            headers=other_user_auth_headers,
            json={"name": "stolen-watch"},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Not authorized for this watch source"

    def test_watch_source_get_admin_bypasses_ownership(
        self, client, db_session, normal_user, admin_user, admin_token_headers
    ):
        """Admin-role bypass (Appendix A): admin can read another user's watch source."""
        ws = _make_watch_source(db_session, normal_user)
        resp = client.get(f"/api/watch-sources/{ws.uuid}", headers=admin_token_headers)
        assert resp.status_code == 200, resp.json()


# --------------------------------------------------------------------------- #
# UserLLMSettings — llm_settings.py:302-305,388-390,475-477,498-500,763-765,810-812
# All six share the detail "Not authorized to access this configuration".
# is_shared bypass applies to the GET path.
# --------------------------------------------------------------------------- #
class TestUserLLMSettingsOwnership:
    def test_llm_config_get_403_other_user(
        self, client, db_session, normal_user, other_user, other_user_auth_headers
    ):
        """GET /config/{uuid} of a private foreign config → llm_settings.py:302-305."""
        cfg = _make_llm_config(db_session, normal_user, is_shared=False)
        resp = client.get(f"/api/llm-settings/config/{cfg.uuid}", headers=other_user_auth_headers)
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Not authorized to access this configuration"

    def test_llm_config_update_403_other_user(
        self, client, db_session, normal_user, other_user, other_user_auth_headers
    ):
        """PUT /config/{uuid} of a foreign config → llm_settings.py:388-390."""
        cfg = _make_llm_config(db_session, normal_user)
        resp = client.put(
            f"/api/llm-settings/config/{cfg.uuid}",
            headers=other_user_auth_headers,
            json={"name": "stolen-llm"},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Not authorized to access this configuration"

    def test_llm_config_get_shared_allowed(
        self, client, db_session, normal_user, other_user, other_user_auth_headers
    ):
        """is_shared bypass: a SHARED foreign config is viewable (no 403)."""
        cfg = _make_llm_config(db_session, normal_user, is_shared=True)
        resp = client.get(f"/api/llm-settings/config/{cfg.uuid}", headers=other_user_auth_headers)
        assert resp.status_code == 200
        # Public schema never leaks the (encrypted) API key.
        assert "api_key" not in resp.json()


# --------------------------------------------------------------------------- #
# Sanity: the contract resources are genuinely owned by normal_user, so the
# OWNER can perform the same action (guards against false-positive 403s that
# would pass for the wrong reason).
# --------------------------------------------------------------------------- #
class TestOwnerStillAllowed:
    def test_owner_can_update_own_vocab(self, client, db_session, normal_user, user_token_headers):
        term = _make_vocab(db_session, normal_user)
        resp = client.put(
            f"/api/custom-vocabulary/{term.id}",
            headers=user_token_headers,
            json={"term": "owner-edit"},
        )
        assert resp.status_code == 200, resp.json()

    def test_owner_can_get_own_prompt(self, client, db_session, normal_user, user_token_headers):
        p = _make_prompt(db_session, normal_user)
        resp = client.get(f"/api/prompts/{p.uuid}", headers=user_token_headers)
        assert resp.status_code == 200
