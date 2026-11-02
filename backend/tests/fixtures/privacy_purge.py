"""Seeding, state-inspection and request helpers for the privacy-purge tests."""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import numpy as np
from fastapi import status

from app.models.organization_policy import OrganizationPolicy
from app.models.speaker import Speaker
from app.models.speaker import SpeakerCluster
from app.models.speaker import SpeakerClusterMember
from app.models.speaker import SpeakerMatch
from app.models.speaker import SpeakerProfile
from app.services.speaker_matching_service import SpeakerMatchingService
from app.tasks.transcription.speaker_processor import create_or_get_speaker

ORG_PURGE = "/api/org-admin/policy/purge"


ME_PURGE = "/api/privacy/me/purge"


ADMIN_PURGE = "/api/admin/privacy-policy/purge"


BODY = {"targets": ["voiceprints", "inferred_gender"], "confirm": "PURGE"}


# --------------------------------------------------------------------------- #
# Seeding: one scope's worth of voiceprint and gender data
# --------------------------------------------------------------------------- #


def document_type_of(doc_id: str) -> str | None:
    """The ``document_type`` the application stamps on a document with this ``_id``."""
    for prefix in ("profile", "cluster"):
        if doc_id.startswith(f"{prefix}_"):
            return prefix
    return None


def seed(gov, os_, owner, org, tag: str) -> SimpleNamespace:
    """Speakers, a cluster, a promoted cluster, a profile, a match and documents for one scope."""
    db = gov.db
    org_id = org.id if org is not None else None
    now = datetime.now(UTC)
    file_a = gov.file(owner, org=org if org is not None else None)
    file_b = gov.file(owner, org=org if org is not None else None)

    def speaker(media_file, name, **fields) -> Speaker:
        # Built by the helper transcription uses, so the row has exactly the columns production
        # gives it (``organization_id`` is NOT one of them: a speaker's tenant is its file's).
        row = create_or_get_speaker(db, owner.id, media_file.id, name)
        for column, value in fields.items():
            setattr(row, column, value)
        db.flush()
        return row

    guess = speaker(
        file_a,
        "SPEAKER_00",
        suggested_name=f"Ann {tag}",
        suggestion_source="voice_match",
        confidence=0.91,
        resolved_display_name=f"Ann {tag}",
        verified=False,
        predicted_gender="female",
        attribute_confidence={
            "gender": 0.9,
            "age_range": 0.7,
            "alignment": "match",
            "alignment_hint": "Ann",
            "metadata_hints": [{"name": "Ann", "role": "host", "confidence": 0.5, "source": "x"}],
        },
        attributes_predicted_at=now,
        predicted_age_range="adult",
    )
    other = speaker(file_b, "SPEAKER_00", predicted_gender="male", gender_confirmed_by_user=False)
    confirmed = speaker(
        file_b,
        "SPEAKER_01",
        display_name=f"Bea {tag}",
        verified=True,
        suggested_name=f"Bea {tag}",
        suggestion_source="voice_match",
        confidence=0.95,
        predicted_gender="female",
        gender_confirmed_by_user=True,
    )
    profile = SpeakerProfile(
        user_id=owner.id,
        organization_id=org_id,
        name=f"Profile {tag}",
        embedding_count=4,
        last_embedding_update=now,
        predicted_gender="female",
    )
    db.add(profile)
    db.flush()
    confirmed.profile_id = profile.id

    loose = SpeakerCluster(
        user_id=owner.id, organization_id=org_id, label=f"loose {tag}", member_count=2
    )
    named = SpeakerCluster(
        user_id=owner.id,
        organization_id=org_id,
        label=f"named {tag}",
        member_count=1,
        promoted_to_profile_id=profile.id,
    )
    db.add_all([loose, named])
    db.flush()
    for cluster, members in ((loose, (guess, other)), (named, (confirmed,))):
        for member in members:
            db.add(
                SpeakerClusterMember(cluster_id=cluster.id, speaker_id=member.id, confidence=0.9)
            )
            member.cluster_id = cluster.id
    first, second = sorted((guess.id, other.id))
    db.add(SpeakerMatch(speaker1_id=first, speaker2_id=second, confidence=0.9))
    db.commit()

    # One voiceprint document per speaker, the profile's, and each cluster's centroid, under the
    # ids the application writes them with.
    doc_ids = {
        *(str(row.uuid) for row in (guess, other, confirmed)),
        f"profile_{profile.uuid}",
        f"cluster_{loose.uuid}",
        f"cluster_{named.uuid}",
    }
    for doc_id in sorted(doc_ids):
        os_.add(doc_id, owner.id, org_id, document_type=document_type_of(doc_id))
    assert guess.organization_id is None, "speakers are not stamped with a tenant in production"
    return SimpleNamespace(
        tag=tag,
        owner=owner,
        org_id=org_id,
        files=(file_a, file_b),
        guess=guess.id,
        other=other.id,
        confirmed=confirmed.id,
        profile=profile.id,
        loose=loose.id,
        named=named.id,
        doc_ids=doc_ids,
    )


def turn_off(db, org, *, voiceprints=True, gender=True) -> None:
    row = db.get(OrganizationPolicy, org.id) or OrganizationPolicy(organization_id=org.id)
    if voiceprints:
        row.voiceprint_matching_enabled = False
    if gender:
        row.gender_inference_enabled = False
    db.add(row)
    db.commit()


def row_state(db, seeded) -> dict[str, Any]:
    db.expire_all()
    guess = db.get(Speaker, seeded.guess)
    other = db.get(Speaker, seeded.other)
    confirmed = db.get(Speaker, seeded.confirmed)
    profile = db.get(SpeakerProfile, seeded.profile)
    return {
        "guess": guess,
        "other": other,
        "confirmed": confirmed,
        "profile": profile,
        "loose_cluster": db.get(SpeakerCluster, seeded.loose),
        "named_cluster": db.get(SpeakerCluster, seeded.named),
        "matches": db.query(SpeakerMatch)
        .filter(SpeakerMatch.speaker1_id.in_([seeded.guess, seeded.other]))
        .count(),
        "loose_members": db.query(SpeakerClusterMember)
        .filter(SpeakerClusterMember.cluster_id == seeded.loose)
        .count(),
    }


def assert_untouched(db, seeded, os_) -> None:
    state = row_state(db, seeded)
    assert state["guess"].suggestion_source == "voice_match", f"{seeded.tag}: suggestion cleared"
    assert state["guess"].predicted_gender == "female", f"{seeded.tag}: gender cleared"
    assert state["profile"].embedding_count == 4, f"{seeded.tag}: profile embedding reset"
    assert state["loose_cluster"] is not None, f"{seeded.tag}: cluster deleted"
    assert state["matches"] == 1, f"{seeded.tag}: match deleted"
    assert seeded.doc_ids <= os_.ids(), f"{seeded.tag}: documents deleted"


def assert_purged(db, seeded, os_) -> None:
    state = row_state(db, seeded)
    guess, other, confirmed, profile = (
        state["guess"],
        state["other"],
        state["confirmed"],
        state["profile"],
    )
    # --- voiceprints: deleted
    assert state["matches"] == 0
    assert state["loose_cluster"] is None and state["loose_members"] == 0
    assert guess.cluster_id is None and other.cluster_id is None
    assert (profile.embedding_count, profile.last_embedding_update) == (0, None)
    assert (guess.suggested_name, guess.suggestion_source, guess.confidence) == (None, None, None)
    assert not (seeded.doc_ids & os_.ids())
    # --- inferred gender: deleted, together with its derived name-versus-voice comparison
    assert guess.predicted_gender is None and other.predicted_gender is None
    assert guess.attributes_predicted_at is None
    bag = guess.attribute_confidence
    assert "gender" not in bag and "alignment" not in bag and "alignment_hint" not in bag
    # --- kept: what a person asserted, and what is not about gender or voice
    assert bag["age_range"] == 0.7 and bag["metadata_hints"][0]["name"] == "Ann"
    assert guess.predicted_age_range == "adult"
    assert confirmed.verified is True and confirmed.display_name == f"Bea {seeded.tag}"
    # the voice comparison on a person-verified speaker goes too; the person's own word stays
    assert (confirmed.suggested_name, confirmed.suggestion_source) == (None, None)
    assert (confirmed.confidence, confirmed.suggestion_detail) == (None, None)
    assert confirmed.predicted_gender == "female" and confirmed.gender_confirmed_by_user is True
    assert confirmed.profile_id == profile.id
    assert profile.name == f"Profile {seeded.tag}"
    assert profile.predicted_gender == "female", "a profile's gender is only ever user-confirmed"
    assert state["named_cluster"] is not None and confirmed.cluster_id == seeded.named


def purge_via_api(client, gov, who, url, body=BODY, **acting):
    with gov.acting_as(who, **acting):
        return client.post(url, json=body)


# --------------------------------------------------------------------------- #
# Names and links a voiceprint made are reverted; a person's are kept (#1280)
# --------------------------------------------------------------------------- #


def make_speaker_row(gov, owner, org, label: str, **fields) -> Speaker:
    """A speaker in a fresh file of ``org`` (None = personal), built as diarization builds it."""
    media_file = gov.file(owner, org=org)
    row = create_or_get_speaker(gov.db, owner.id, media_file.id, label)
    for column, value in fields.items():
        setattr(row, column, value)
    gov.db.commit()
    return row


def make_profile_row(gov, owner, org, name: str) -> SpeakerProfile:
    profile = SpeakerProfile(
        user_id=owner.id, organization_id=org.id if org is not None else None, name=name
    )
    gov.db.add(profile)
    gov.db.commit()
    return profile


def auto_accept_through_the_matcher(gov, owner, speaker: Speaker, profile: SpeakerProfile) -> None:
    """Make ``speaker`` a voiceprint-named one the way upload-time matching does."""
    match = {
        "confidence": 0.93,
        "suggested_name": profile.name,
        "auto_accept": True,
        "profile_id": profile.id,
        "profile_uuid": str(profile.uuid),
    }
    with (
        patch("app.services.speaker_matching.match_handling.add_speaker_embedding"),
        patch("app.services.opensearch_service.update_speaker_profile"),
        patch.object(SpeakerMatchingService, "find_and_store_speaker_matches"),
        patch.object(SpeakerMatchingService, "_propagate_profile_assignment"),
        patch.object(SpeakerMatchingService, "match_speaker_to_known_speakers", return_value=match),
        patch("app.tasks.rename_propagation_task.propagate_speaker_rename.delay"),
    ):
        SpeakerMatchingService(gov.db, None).process_speaker_embeddings_native(
            media_file_id=int(speaker.media_file_id),
            user_id=int(owner.id),
            native_embeddings={int(speaker.id): np.ones(256, dtype=np.float32) / 16.0},
        )
    gov.db.expire_all()


def run_org_purge(client, gov, broker, in_process_task, targets=("voiceprints",)):
    body = {"targets": list(targets), "confirm": "PURGE"}
    response = purge_via_api(client, gov, gov.admin, ORG_PURGE, body)
    assert response.status_code == status.HTTP_202_ACCEPTED, response.text
    return response.json()["task_id"], in_process_task(broker[-1])
