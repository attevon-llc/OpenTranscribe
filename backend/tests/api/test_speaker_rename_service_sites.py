"""The service-layer speaker renames must reach the chunk plane too (issue #432).

``tests/api/test_rename_propagation_dispatch.py`` covers the three API paths #405
fixed. These cover the six writers it deliberately stopped short of — all of them
in services, all of them relabelling speakers whose chunks are **already
indexed**:

* ``SpeakerMatchingService._handle_speaker_match`` — an auto-accepted match. The
  cloud-ASR path extracts embeddings on the GPU worker *in parallel* with chunk
  indexing, and ``rediarize_task`` re-runs matching on a long-completed file.
* ``SpeakerClusteringService.find_or_create_cluster`` — a speaker joining a
  cluster that was already promoted to a profile inherits the profile's name.
* ``SpeakerClusteringService.promote_cluster_to_profile`` — renames every member,
  across every file the cluster spans.
* ``SpeakerClusteringService.batch_verify_speakers`` — accept / assign / name.

Each test asserts **both halves**: that Postgres really was renamed (otherwise a
path that silently did nothing would pass the dispatch assertion by dispatching
nothing) and that the propagation carried the name the chunks were indexed with.
The old name is the subtle part — after the commit Postgres cannot say what it
was, so a site that captures it too late propagates a name no chunk matches and
fails silently.

The rewrite itself runs against a real cluster in
``tests/integration/test_speaker_rename_service_chunks.py``.
"""

import uuid as uuid_mod
from unittest.mock import patch

import numpy as np
import pytest

from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import SpeakerCluster
from app.models.media import SpeakerClusterMember
from app.models.media import SpeakerProfile

_DELAY = "app.tasks.rename_propagation_task.propagate_speaker_rename.delay"


def _media_file(db_session, user, name: str) -> MediaFile:
    media_file = MediaFile(
        uuid=str(uuid_mod.uuid4()),
        user_id=user.id,
        filename=f"{name}.mp4",
        storage_path=f"test/{name}.mp4",
        content_type="video/mp4",
        file_size=1000,
    )
    db_session.add(media_file)
    db_session.flush()
    return media_file


def _speaker(db_session, user, media_file, name: str, **kwargs) -> Speaker:
    speaker = Speaker(
        uuid=str(uuid_mod.uuid4()),
        media_file_id=media_file.id,
        user_id=user.id,
        name=name,
        **kwargs,
    )
    db_session.add(speaker)
    db_session.flush()
    return speaker


def _profile(db_session, user, name: str) -> SpeakerProfile:
    profile = SpeakerProfile(uuid=str(uuid_mod.uuid4()), user_id=user.id, name=name)
    db_session.add(profile)
    db_session.flush()
    return profile


def _cluster(db_session, user, speakers, **kwargs) -> SpeakerCluster:
    cluster = SpeakerCluster(
        uuid=str(uuid_mod.uuid4()), user_id=user.id, member_count=len(speakers), **kwargs
    )
    db_session.add(cluster)
    db_session.flush()
    for speaker in speakers:
        db_session.add(
            SpeakerClusterMember(
                uuid=str(uuid_mod.uuid4()),
                cluster_id=cluster.id,
                speaker_id=speaker.id,
                confidence=0.9,
            )
        )
        speaker.cluster_id = cluster.id
    db_session.flush()
    return cluster


def _queued(delay_mock) -> dict[tuple[str, str], list[str]]:
    """``{(file_uuid, new_name): old_names}`` for every queued propagation."""
    return {
        (call.kwargs["file_uuid"], call.kwargs["new_name"]): sorted(call.kwargs["old_names"])
        for call in delay_mock.call_args_list
    }


class TestMatchingServiceAutoAccept:
    """``_handle_speaker_match`` — the ≥ auto-accept tier writes ``display_name``."""

    @pytest.fixture
    def matching_service(self, db_session):
        """The real service with only its OpenSearch writes stubbed.

        ``add_speaker_embedding`` and ``find_and_store_speaker_matches`` are
        voiceprint-plane side effects; the chunk-plane dispatch under test is
        neither, and is left alone.
        """
        from app.services.speaker_matching_service import SpeakerMatchingService

        service = SpeakerMatchingService(db_session, embedding_service=None)
        with (
            patch("app.services.speaker_matching_service.add_speaker_embedding"),
            patch.object(SpeakerMatchingService, "find_and_store_speaker_matches"),
        ):
            yield service

    @staticmethod
    def _match(name: str, auto_accept: bool):
        return {
            "confidence": 0.93 if auto_accept else 0.6,
            "suggested_name": name,
            "auto_accept": auto_accept,
            "profile_id": None,
        }

    def test_auto_accepted_match_queues_the_label_the_chunks_hold(
        self, db_session, normal_user, matching_service
    ):
        media_file = _media_file(db_session, normal_user, "auto-accept")
        speaker = _speaker(db_session, normal_user, media_file, "SPEAKER_00")

        with (
            patch.object(
                type(matching_service),
                "match_speaker_to_known_speakers",
                return_value=self._match("Dana", auto_accept=True),
            ),
            patch(_DELAY) as delay_mock,
        ):
            matching_service.process_speaker_embeddings_native(
                media_file_id=int(media_file.id),
                user_id=int(normal_user.id),
                native_embeddings={int(speaker.id): np.ones(256, dtype=np.float32) / 16.0},
            )

        db_session.refresh(speaker)
        assert speaker.display_name == "Dana", "control: the rename really happened"
        assert _queued(delay_mock) == {(str(media_file.uuid), "Dana"): ["SPEAKER_00"]}

    def test_relabelling_queues_the_previous_display_name(
        self, db_session, normal_user, matching_service
    ):
        """Once a speaker has a display name, that — not ``name`` — is what is indexed."""
        media_file = _media_file(db_session, normal_user, "relabel")
        speaker = _speaker(db_session, normal_user, media_file, "SPEAKER_00", display_name="Dana")

        with (
            patch.object(
                type(matching_service),
                "match_speaker_to_known_speakers",
                return_value=self._match("Dana Whitfield", auto_accept=True),
            ),
            patch(_DELAY) as delay_mock,
        ):
            matching_service.process_speaker_embeddings_native(
                media_file_id=int(media_file.id),
                user_id=int(normal_user.id),
                native_embeddings={int(speaker.id): np.ones(256, dtype=np.float32) / 16.0},
            )

        db_session.refresh(speaker)
        assert speaker.display_name == "Dana Whitfield"
        assert _queued(delay_mock) == {(str(media_file.uuid), "Dana Whitfield"): ["Dana"]}

    def test_a_suggestion_below_auto_accept_queues_nothing(
        self, db_session, normal_user, matching_service
    ):
        """A suggestion leaves ``display_name`` alone, so the chunks are still right."""
        media_file = _media_file(db_session, normal_user, "suggest-only")
        speaker = _speaker(db_session, normal_user, media_file, "SPEAKER_00")

        with (
            patch.object(
                type(matching_service),
                "match_speaker_to_known_speakers",
                return_value=self._match("Dana", auto_accept=False),
            ),
            patch(_DELAY) as delay_mock,
        ):
            matching_service.process_speaker_embeddings_native(
                media_file_id=int(media_file.id),
                user_id=int(normal_user.id),
                native_embeddings={int(speaker.id): np.ones(256, dtype=np.float32) / 16.0},
            )

        db_session.refresh(speaker)
        assert speaker.display_name is None, "control: nothing was renamed"
        delay_mock.assert_not_called()


class TestClusterPromotion:
    """``promote_cluster_to_profile`` renames every member of the cluster."""

    @pytest.fixture(autouse=True)
    def _quiet_profile_embedding(self):
        with patch(
            "app.services.profile_embedding_service.ProfileEmbeddingService.update_profile_embedding"
        ):
            yield

    def test_promotion_queues_one_task_per_file_with_each_files_own_stale_name(
        self, db_session, normal_user
    ):
        """A cluster spans files, and each file was indexed with its own label."""
        from app.services.speaker_clustering_service import SpeakerClusteringService

        first = _media_file(db_session, normal_user, "promo-a")
        second = _media_file(db_session, normal_user, "promo-b")
        alpha = _speaker(db_session, normal_user, first, "SPEAKER_00")
        beta = _speaker(db_session, normal_user, second, "SPEAKER_03", display_name="Unknown 3")
        cluster = _cluster(db_session, normal_user, [alpha, beta])

        with patch(_DELAY) as delay_mock:
            profile = SpeakerClusteringService(db_session).promote_cluster_to_profile(
                str(cluster.uuid), "Dana", int(normal_user.id)
            )

        assert profile is not None
        db_session.refresh(alpha)
        db_session.refresh(beta)
        assert (alpha.display_name, beta.display_name) == ("Dana", "Dana")
        assert _queued(delay_mock) == {
            (str(first.uuid), "Dana"): ["SPEAKER_00"],
            (str(second.uuid), "Dana"): ["Unknown 3"],
        }

    def test_two_members_in_one_file_are_coalesced_into_one_task(self, db_session, normal_user):
        """Two tasks would each rewrite the same ``speakers`` array and race."""
        from app.services.speaker_clustering_service import SpeakerClusteringService

        media_file = _media_file(db_session, normal_user, "promo-same-file")
        alpha = _speaker(db_session, normal_user, media_file, "SPEAKER_00")
        beta = _speaker(db_session, normal_user, media_file, "SPEAKER_01")
        cluster = _cluster(db_session, normal_user, [alpha, beta])

        with patch(_DELAY) as delay_mock:
            SpeakerClusteringService(db_session).promote_cluster_to_profile(
                str(cluster.uuid), "Dana", int(normal_user.id)
            )

        delay_mock.assert_called_once()
        assert _queued(delay_mock) == {(str(media_file.uuid), "Dana"): ["SPEAKER_00", "SPEAKER_01"]}

    def test_a_member_already_carrying_the_name_is_not_queued(self, db_session, normal_user):
        """Its chunks already say "Dana" — rewriting them bumps the cache for nothing."""
        from app.services.speaker_clustering_service import SpeakerClusteringService

        first = _media_file(db_session, normal_user, "promo-noop")
        second = _media_file(db_session, normal_user, "promo-real")
        already = _speaker(db_session, normal_user, first, "SPEAKER_00", display_name="Dana")
        stale = _speaker(db_session, normal_user, second, "SPEAKER_01")
        cluster = _cluster(db_session, normal_user, [already, stale])

        with patch(_DELAY) as delay_mock:
            SpeakerClusteringService(db_session).promote_cluster_to_profile(
                str(cluster.uuid), "Dana", int(normal_user.id)
            )

        assert _queued(delay_mock) == {(str(second.uuid), "Dana"): ["SPEAKER_01"]}


class TestBatchVerifySpeakers:
    """``batch_verify_speakers`` — three actions, three ``display_name`` writes."""

    def test_accept_keeps_each_speakers_own_suggestion(self, db_session, normal_user):
        """Two people in one file: the rename must not collapse them onto one name."""
        from app.services.speaker_clustering_service import SpeakerClusteringService

        media_file = _media_file(db_session, normal_user, "batch-accept")
        alpha = _speaker(db_session, normal_user, media_file, "SPEAKER_00", suggested_name="Dana")
        beta = _speaker(db_session, normal_user, media_file, "SPEAKER_01", suggested_name="Ravi")

        with patch(_DELAY) as delay_mock:
            result = SpeakerClusteringService(db_session).batch_verify_speakers(
                [str(alpha.uuid), str(beta.uuid)], int(normal_user.id), action="accept"
            )

        assert result["updated_count"] == 2
        db_session.refresh(alpha)
        db_session.refresh(beta)
        assert (alpha.display_name, beta.display_name) == ("Dana", "Ravi")
        assert _queued(delay_mock) == {
            (str(media_file.uuid), "Dana"): ["SPEAKER_00"],
            (str(media_file.uuid), "Ravi"): ["SPEAKER_01"],
        }

    def test_assign_to_a_profile_queues_the_pre_assignment_names(self, db_session, normal_user):
        from app.services.speaker_clustering_service import SpeakerClusteringService

        media_file = _media_file(db_session, normal_user, "batch-assign")
        speaker = _speaker(db_session, normal_user, media_file, "SPEAKER_02")
        profile = _profile(db_session, normal_user, "Dana")

        with patch(_DELAY) as delay_mock:
            result = SpeakerClusteringService(db_session).batch_verify_speakers(
                [str(speaker.uuid)],
                int(normal_user.id),
                action="assign",
                profile_uuid=str(profile.uuid),
            )

        assert result["updated_count"] == 1
        db_session.refresh(speaker)
        assert speaker.display_name == "Dana"
        assert _queued(delay_mock) == {(str(media_file.uuid), "Dana"): ["SPEAKER_02"]}

    def test_naming_queues_the_previous_display_name(self, db_session, normal_user):
        from app.services.speaker_clustering_service import SpeakerClusteringService

        media_file = _media_file(db_session, normal_user, "batch-name")
        speaker = _speaker(db_session, normal_user, media_file, "SPEAKER_00", display_name="Dana")

        with patch(_DELAY) as delay_mock:
            SpeakerClusteringService(db_session).batch_verify_speakers(
                [str(speaker.uuid)],
                int(normal_user.id),
                action="name",
                display_name="Dana Whitfield",
            )

        db_session.refresh(speaker)
        assert speaker.display_name == "Dana Whitfield"
        assert _queued(delay_mock) == {(str(media_file.uuid), "Dana Whitfield"): ["Dana"]}

    def test_skip_renames_nothing_and_queues_nothing(self, db_session, normal_user):
        """ "Reviewed, leave it alone" must not touch the index."""
        from app.services.speaker_clustering_service import SpeakerClusteringService

        media_file = _media_file(db_session, normal_user, "batch-skip")
        speaker = _speaker(db_session, normal_user, media_file, "SPEAKER_00", suggested_name="Dana")

        with patch(_DELAY) as delay_mock:
            result = SpeakerClusteringService(db_session).batch_verify_speakers(
                [str(speaker.uuid)], int(normal_user.id), action="skip"
            )

        assert result["updated_count"] == 1
        db_session.refresh(speaker)
        assert speaker.display_name is None, "control: skip marks reviewed, it does not name"
        delay_mock.assert_not_called()


class TestJoiningAPromotedCluster:
    """``find_or_create_cluster`` — a promoted cluster relabels whoever joins it."""

    def test_the_inherited_profile_name_is_propagated_after_the_commit(
        self, db_session, normal_user
    ):
        from app.services.speaker_clustering_service import SpeakerClusteringService

        media_file = _media_file(db_session, normal_user, "join-promoted")
        speaker = _speaker(db_session, normal_user, media_file, "SPEAKER_04")
        profile = _profile(db_session, normal_user, "Dana")
        cluster = _cluster(db_session, normal_user, [], promoted_to_profile_id=profile.id)

        service = SpeakerClusteringService(db_session)
        embedding = (np.ones(256, dtype=np.float32) / 16.0).tolist()

        with (
            patch(
                "app.services.opensearch_service.find_matching_clusters",
                return_value=[{"cluster_uuid": str(cluster.uuid), "similarity": 0.91}],
            ),
            patch.object(type(service), "_get_speaker_embedding", return_value=embedding),
            patch.object(type(service), "_update_cluster_centroid"),
            patch(_DELAY) as delay_mock,
        ):
            clusters = service.cluster_speakers_for_file(int(media_file.id), int(normal_user.id))

        assert len(clusters) == 1
        db_session.refresh(speaker)
        assert speaker.display_name == "Dana", "control: joining a promoted cluster renames"
        assert _queued(delay_mock) == {(str(media_file.uuid), "Dana"): ["SPEAKER_04"]}
