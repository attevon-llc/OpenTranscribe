"""Tests for speaker clustering constraints: outlier analysis, unassign, and blacklisting."""

import uuid as uuid_pkg

import pytest
from sqlalchemy import text

from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import SpeakerCannotLink
from app.models.media import SpeakerCluster
from app.models.media import SpeakerClusterMember
from app.models.media import SpeakerProfile
from app.models.media import SpeakerProfileBlacklist

# These routes now run the shared speaker cleanup (#1214), which reports a store it cannot reach
# as a failure. CI has no OpenSearch, so stand the stores in with the recorders; the live
# behaviour is covered by test_deletion_residue_live.
pytestmark = pytest.mark.usefixtures("speaker_os_seams")


@pytest.fixture
def cluster_with_members(db_session, normal_user):
    """Create a cluster with 4 members (3 male, 1 female) for testing."""
    cluster = SpeakerCluster(
        uuid=uuid_pkg.uuid4(),
        user_id=normal_user.id,
        label="Test Cluster",
        member_count=4,
    )
    db_session.add(cluster)
    db_session.flush()

    # Create a media file for the speakers
    media = MediaFile(
        uuid=uuid_pkg.uuid4(),
        user_id=normal_user.id,
        filename="test.mp4",
        content_type="video/mp4",
        file_size=1000,
        storage_path="/test/path",
    )
    db_session.add(media)
    db_session.flush()

    speakers = []
    genders = ["male", "male", "male", "female"]
    for i, gender in enumerate(genders):
        speaker = Speaker(
            uuid=uuid_pkg.uuid4(),
            name=f"Speaker_{i}",
            user_id=normal_user.id,
            media_file_id=media.id,
            cluster_id=cluster.id,
            predicted_gender=gender,
        )
        db_session.add(speaker)
        db_session.flush()

        member = SpeakerClusterMember(
            uuid=uuid_pkg.uuid4(),
            cluster_id=cluster.id,
            speaker_id=speaker.id,
            confidence=0.85,
        )
        db_session.add(member)
        speakers.append(speaker)

    db_session.commit()
    return {
        "cluster": cluster,
        "speakers": speakers,
        "media": media,
        "user": normal_user,
    }


@pytest.fixture
def promoted_cluster(db_session, cluster_with_members):
    """Create a cluster promoted to a profile."""
    data = cluster_with_members
    profile = SpeakerProfile(
        uuid=uuid_pkg.uuid4(),
        user_id=data["user"].id,
        name="Test Profile",
        source_cluster_id=data["cluster"].id,
    )
    db_session.add(profile)
    db_session.flush()
    data["cluster"].promoted_to_profile_id = profile.id
    db_session.commit()
    return {**data, "profile": profile}


class TestSpeakerConstraintModels:
    """Test the constraint database models."""

    def test_cannot_link_creation(self, db_session, cluster_with_members):
        """SpeakerCannotLink records can be created."""
        speakers = cluster_with_members["speakers"]
        link = SpeakerCannotLink(
            speaker_id=speakers[0].id,
            cannot_link_speaker_id=speakers[3].id,
            reason="test",
        )
        db_session.add(link)
        db_session.commit()

        result = (
            db_session.query(SpeakerCannotLink)
            .filter(SpeakerCannotLink.speaker_id == speakers[0].id)
            .first()
        )
        assert result is not None
        assert result.cannot_link_speaker_id == speakers[3].id
        assert result.reason == "test"

    def test_cannot_link_unique_constraint(self, db_session, cluster_with_members):
        """Duplicate cannot-link entries are rejected."""
        speakers = cluster_with_members["speakers"]
        link1 = SpeakerCannotLink(
            speaker_id=speakers[0].id,
            cannot_link_speaker_id=speakers[3].id,
        )
        db_session.add(link1)
        db_session.commit()

        link2 = SpeakerCannotLink(
            speaker_id=speakers[0].id,
            cannot_link_speaker_id=speakers[3].id,
        )
        db_session.add(link2)
        with pytest.raises(Exception):  # noqa: B017
            db_session.commit()
        db_session.rollback()

    def test_profile_blacklist_creation(self, db_session, promoted_cluster):
        """SpeakerProfileBlacklist records can be created."""
        data = promoted_cluster
        blacklist = SpeakerProfileBlacklist(
            speaker_id=data["speakers"][3].id,
            profile_id=data["profile"].id,
            reason="unassigned_by_user",
        )
        db_session.add(blacklist)
        db_session.commit()

        result = (
            db_session.query(SpeakerProfileBlacklist)
            .filter(SpeakerProfileBlacklist.speaker_id == data["speakers"][3].id)
            .first()
        )
        assert result is not None
        assert result.profile_id == data["profile"].id

    def test_cascade_delete_on_speaker(self, db_session, cluster_with_members):
        """Deleting a speaker cascades to constraint records."""
        speakers = cluster_with_members["speakers"]
        link = SpeakerCannotLink(
            speaker_id=speakers[0].id,
            cannot_link_speaker_id=speakers[3].id,
        )
        db_session.add(link)
        db_session.commit()

        # Delete the speaker
        db_session.delete(speakers[0])
        db_session.commit()

        # Constraint should be gone
        result = (
            db_session.query(SpeakerCannotLink)
            .filter(SpeakerCannotLink.speaker_id == speakers[0].id)
            .all()
        )
        assert len(result) == 0


class TestIsBlockedFromCluster:
    """Test ``speaker_cleanup.speaker_blocked_from_cluster``."""

    def test_not_blocked_by_default(self, db_session, cluster_with_members):
        """Speaker is not blocked when no constraints exist."""
        from app.services.speaker_cleanup import speaker_blocked_from_cluster

        data = cluster_with_members

        # Create a new speaker not in the cluster
        new_speaker = Speaker(
            uuid=uuid_pkg.uuid4(),
            name="New Speaker",
            user_id=data["user"].id,
            media_file_id=data["media"].id,
        )
        db_session.add(new_speaker)
        db_session.commit()

        assert not speaker_blocked_from_cluster(db_session, new_speaker.id, data["cluster"])

    def test_blocked_by_cannot_link(self, db_session, cluster_with_members):
        """Speaker is blocked when cannot-link exists with a cluster member."""
        from app.services.speaker_cleanup import speaker_blocked_from_cluster

        data = cluster_with_members

        new_speaker = Speaker(
            uuid=uuid_pkg.uuid4(),
            name="Blocked Speaker",
            user_id=data["user"].id,
            media_file_id=data["media"].id,
        )
        db_session.add(new_speaker)
        db_session.flush()

        # Add cannot-link constraint
        link = SpeakerCannotLink(
            speaker_id=new_speaker.id,
            cannot_link_speaker_id=data["speakers"][0].id,
        )
        db_session.add(link)
        db_session.commit()

        assert speaker_blocked_from_cluster(db_session, new_speaker.id, data["cluster"])

    def test_blocked_by_profile_blacklist(self, db_session, promoted_cluster):
        """Speaker is blocked when profile-blacklisted."""
        from app.services.speaker_cleanup import speaker_blocked_from_cluster

        data = promoted_cluster

        new_speaker = Speaker(
            uuid=uuid_pkg.uuid4(),
            name="Blacklisted Speaker",
            user_id=data["user"].id,
            media_file_id=data["media"].id,
        )
        db_session.add(new_speaker)
        db_session.flush()

        blacklist = SpeakerProfileBlacklist(
            speaker_id=new_speaker.id,
            profile_id=data["profile"].id,
        )
        db_session.add(blacklist)
        db_session.commit()

        assert speaker_blocked_from_cluster(db_session, new_speaker.id, data["cluster"])


class TestUnassignSpeakers:
    """Test the unassign_speakers service method."""

    def test_unassign_basic(self, db_session, cluster_with_members):
        """Unassigning removes member from cluster."""
        from app.services.speaker_clustering_service import SpeakerClusteringService

        data = cluster_with_members
        service = SpeakerClusteringService(db_session)
        female_speaker = data["speakers"][3]

        result = service.unassign_speakers(
            str(data["cluster"].uuid),
            [str(female_speaker.uuid)],
            data["user"].id,
            blacklist=False,
        )

        assert result["unassigned_count"] == 1

        # Speaker should not be in cluster
        db_session.refresh(female_speaker)
        assert female_speaker.cluster_id is None

        # Cluster member count updated
        db_session.refresh(data["cluster"])
        assert data["cluster"].member_count == 3

    def test_unassign_with_cannot_link(self, db_session, cluster_with_members):
        """Unassigning with blacklist creates cannot-link constraints."""
        from app.services.speaker_clustering_service import SpeakerClusteringService

        data = cluster_with_members
        service = SpeakerClusteringService(db_session)
        female_speaker = data["speakers"][3]

        service.unassign_speakers(
            str(data["cluster"].uuid),
            [str(female_speaker.uuid)],
            data["user"].id,
            blacklist=True,
        )

        # Should have cannot-link entries with each remaining member (bidirectional)
        links = (
            db_session.query(SpeakerCannotLink)
            .filter(SpeakerCannotLink.speaker_id == female_speaker.id)
            .all()
        )
        assert len(links) == 3  # one for each remaining member

    def test_unassign_with_profile_blacklist(self, db_session, promoted_cluster):
        """Unassigning from promoted cluster creates profile blacklist."""
        from app.services.speaker_clustering_service import SpeakerClusteringService

        data = promoted_cluster
        service = SpeakerClusteringService(db_session)
        female_speaker = data["speakers"][3]

        service.unassign_speakers(
            str(data["cluster"].uuid),
            [str(female_speaker.uuid)],
            data["user"].id,
            blacklist=True,
        )

        # Should have profile blacklist (not cannot-link)
        blacklist = (
            db_session.query(SpeakerProfileBlacklist)
            .filter(
                SpeakerProfileBlacklist.speaker_id == female_speaker.id,
                SpeakerProfileBlacklist.profile_id == data["profile"].id,
            )
            .first()
        )
        assert blacklist is not None

        # ...AND cannot-link entries with each remaining member: the blacklist is recorded
        # speaker-to-speaker as well as speaker-to-profile (issue #1233).
        links = (
            db_session.query(SpeakerCannotLink)
            .filter(SpeakerCannotLink.speaker_id == female_speaker.id)
            .count()
        )
        assert links == 3

    def test_unassign_all_deletes_cluster(self, db_session, cluster_with_members):
        """Unassigning all members deletes the cluster."""
        from app.services.speaker_clustering_service import SpeakerClusteringService

        data = cluster_with_members
        service = SpeakerClusteringService(db_session)
        all_uuids = [str(s.uuid) for s in data["speakers"]]

        service.unassign_speakers(
            str(data["cluster"].uuid),
            all_uuids,
            data["user"].id,
            blacklist=False,
        )

        # Cluster should be deleted
        cluster = (
            db_session.query(SpeakerCluster).filter(SpeakerCluster.id == data["cluster"].id).first()
        )
        assert cluster is None

    def test_unassign_wrong_user(self, db_session, cluster_with_members):
        """Unassigning from another user's cluster raises error."""
        from app.services.speaker_clustering_service import SpeakerClusteringService

        data = cluster_with_members
        service = SpeakerClusteringService(db_session)

        with pytest.raises(ValueError, match="Cluster not found"):
            service.unassign_speakers(
                str(data["cluster"].uuid),
                [str(data["speakers"][0].uuid)],
                999999,  # wrong user
                blacklist=False,
            )


class TestAnalyzeOutliersEndpoint:
    """Test the analyze-outliers API endpoint."""

    def test_analyze_outliers_returns_404_for_missing_cluster(self, client, user_token_headers):
        """Returns 404 for non-existent cluster."""
        fake_uuid = str(uuid_pkg.uuid4())
        response = client.post(
            f"/api/speaker-clusters/{fake_uuid}/analyze-outliers",
            headers=user_token_headers,
        )
        assert response.status_code == 404

    def test_analyze_outliers_requires_auth(self, client):
        """Returns 401 without authentication."""
        fake_uuid = str(uuid_pkg.uuid4())
        response = client.post(
            f"/api/speaker-clusters/{fake_uuid}/analyze-outliers",
        )
        assert response.status_code == 401


class TestUnassignEndpoint:
    """Test the unassign API endpoint."""

    def test_unassign_returns_404_for_missing_cluster(self, client, user_token_headers):
        """Returns 404 for non-existent cluster."""
        fake_uuid = str(uuid_pkg.uuid4())
        response = client.post(
            f"/api/speaker-clusters/{fake_uuid}/unassign",
            headers=user_token_headers,
            json={
                "speaker_uuids": [str(uuid_pkg.uuid4())],
                "blacklist": True,
            },
        )
        assert response.status_code == 404

    def test_unassign_requires_auth(self, client):
        """Returns 401 without authentication."""
        fake_uuid = str(uuid_pkg.uuid4())
        response = client.post(
            f"/api/speaker-clusters/{fake_uuid}/unassign",
            json={
                "speaker_uuids": [str(uuid_pkg.uuid4())],
                "blacklist": True,
            },
        )
        assert response.status_code == 401

    def test_unassign_validates_empty_list(self, client, user_token_headers):
        """Returns 422 with empty speaker list."""
        fake_uuid = str(uuid_pkg.uuid4())
        response = client.post(
            f"/api/speaker-clusters/{fake_uuid}/unassign",
            headers=user_token_headers,
            json={
                "speaker_uuids": [],
                "blacklist": True,
            },
        )
        assert response.status_code == 422


class TestMigrationDetection:
    """Test that the v310 migration is properly detected."""

    def test_speaker_cannot_link_table_exists(self, db_session):
        """The speaker_cannot_link table should exist after migration."""
        result = db_session.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                "WHERE table_name = 'speaker_cannot_link')"
            )
        )
        assert result.scalar() is True

    def test_speaker_profile_blacklist_table_exists(self, db_session):
        """The speaker_profile_blacklist table should exist after migration."""
        result = db_session.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                "WHERE table_name = 'speaker_profile_blacklist')"
            )
        )
        assert result.scalar() is True
