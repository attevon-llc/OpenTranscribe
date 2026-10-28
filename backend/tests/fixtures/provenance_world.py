"""One file with the three kinds of speaker identity the provenance suites care about (#1280).

``build_world`` writes a completed file with three speakers and a few segments each:

* ``alice``: a voiceprint wrote the name AND the profile link (``identity_source =
  'voiceprint'``, verified, linked to a profile named like the speaker),
* ``carol``: a person typed the name, a voiceprint added only the link
  (``voiceprint_link``),
* ``bob``: a person typed and confirmed it (``human``), linked to a profile.

Every suite then flips the file's scope off or on and asserts on what each surface prints:
Alice and the profile links of Alice and Carol must disappear while off, Carol's name and
Bob's everything must not. The diarizer labels (``SPEAKER_00``..) are what replaces a hidden
name.
"""

from __future__ import annotations

import uuid as uuid_pkg
from dataclasses import dataclass
from typing import Any

from app.models.media import SpeakerProfile
from app.models.media import TranscriptSegment
from tests.fixtures.speaker_cleanup_seams import make_file
from tests.fixtures.speaker_cleanup_seams import make_speaker

ALICE = "Alice Zylofenix"
CAROL = "Carol Quillon"
BOB = "Bob Brackett"


@dataclass
class World:
    file: Any
    alice: Any
    carol: Any
    bob: Any
    profiles: dict[str, Any]
    segments: list[Any]


def _profile(db, user, name: str) -> Any:
    # Unique per call: (owner, tenant, name) is unique, and suites build several worlds.
    profile = SpeakerProfile(
        uuid=uuid_pkg.uuid4(), user_id=user.id, name=f"{name} {uuid_pkg.uuid4().hex[:8]}"
    )
    db.add(profile)
    db.flush()
    return profile


def build_world(db, user, *, organization=None, segments_per_speaker: int = 2) -> World:
    """A completed file of ``user`` (in ``organization`` or personal) with the three speakers."""
    media_file = make_file(db, user)
    media_file.organization_id = organization.id if organization is not None else None
    db.flush()
    profiles = {
        "alice": _profile(db, user, ALICE),
        "carol": _profile(db, user, CAROL),
        "bob": _profile(db, user, BOB),
    }
    alice = make_speaker(
        db,
        user,
        media_file,
        name="SPEAKER_00",
        display_name=ALICE,
        verified=True,
        profile_id=profiles["alice"].id,
        identity_source="voiceprint",
        suggested_name=ALICE,
        suggestion_source="profile_match",
        confidence=0.9,
    )
    carol = make_speaker(
        db,
        user,
        media_file,
        name="SPEAKER_01",
        display_name=CAROL,
        verified=True,
        profile_id=profiles["carol"].id,
        identity_source="voiceprint_link",
    )
    bob = make_speaker(
        db,
        user,
        media_file,
        name="SPEAKER_02",
        display_name=BOB,
        verified=True,
        profile_id=profiles["bob"].id,
        identity_source="human",
    )
    segments: list[Any] = []
    clock = 0.0
    for round_ in range(segments_per_speaker):
        for speaker in (alice, carol, bob):
            segment = TranscriptSegment(
                media_file_id=media_file.id,
                speaker_id=speaker.id,
                start_time=clock,
                end_time=clock + 0.9,
                text=f"remark number {len(segments)}",
            )
            db.add(segment)
            segments.append(segment)
            clock += 1.0
    db.flush()
    return World(media_file, alice, carol, bob, profiles, segments)
