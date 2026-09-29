"""Browser playback renditions for originals no browser decodes.

DEFECT THIS CATCHES: AIFF, WMA, ALAC and AVI uploads transcribed fine but never played.
The player sat at 00:00, because Firefox and Chromium cannot decode them (issue #1044's
matrix). Nothing produced a playable copy. The only ffmpeg transcodes were download-only.

Every fixture is a real file that ffmpeg generates at test time. The playability rules are
exercised against ffprobe's real output, not hand-written dicts. Object storage is an
in-memory fake, and Postgres is the real savepoint session. The encode itself is real
ffmpeg, and the stored rendition is re-probed to check it is AAC/M4A.

Which formats play was measured with Playwright Firefox and Chromium. A file counts as
playable only if both browsers played it, with sound (and, for video, with a picture).
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import BinaryIO

import pytest
from fastapi import status

from app.models.media import MediaFile
from app.services import playback_rendition as pr
from app.services.playback_rendition import PlaybackNeed

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe are required to generate and probe the media fixtures",
)

_A = ["-f", "lavfi", "-i", "sine=frequency=440:duration=0.6:sample_rate=44100"]
_V = ["-f", "lavfi", "-i", "testsrc=size=64x48:rate=10:duration=0.6", *_A]
_H264 = ["-c:v", "libx264", "-pix_fmt", "yuv420p"]

#: (filename, ffmpeg output args, declared type, what it needs). Measured in Playwright
#: Firefox + Chromium: "none" played in both, the others failed in at least one.
FIXTURES: dict[str, tuple[list[str], str, PlaybackNeed]] = {
    # --- plays as uploaded: no rendition, no cost
    "a.mp3": ([*_A, "-c:a", "libmp3lame"], "audio/mpeg", PlaybackNeed.NONE),
    "a.wav": ([*_A, "-c:a", "pcm_s16le"], "audio/wav", PlaybackNeed.NONE),
    "a24.wav": ([*_A, "-c:a", "pcm_s24le"], "audio/wav", PlaybackNeed.NONE),
    "a.flac": ([*_A, "-c:a", "flac"], "audio/flac", PlaybackNeed.NONE),
    "a.ogg": ([*_A, "-c:a", "libvorbis"], "audio/ogg", PlaybackNeed.NONE),
    "a.opus": ([*_A, "-c:a", "libopus", "-ar", "48000"], "audio/ogg", PlaybackNeed.NONE),
    "a.aac": ([*_A, "-c:a", "aac", "-f", "adts"], "audio/aac", PlaybackNeed.NONE),
    "a.m4a": ([*_A, "-c:a", "aac"], "audio/mp4", PlaybackNeed.NONE),
    "v.mp4": ([*_V, *_H264, "-c:a", "aac"], "video/mp4", PlaybackNeed.NONE),
    "v.webm": ([*_V, "-c:v", "libvpx-vp9", "-c:a", "libopus"], "video/webm", PlaybackNeed.NONE),
    "v.mov": ([*_V, *_H264, "-c:a", "aac"], "video/quicktime", PlaybackNeed.NONE),
    "v.mkv": ([*_V, *_H264, "-c:a", "aac"], "video/x-matroska", PlaybackNeed.NONE),
    # --- audio no browser decodes: converted
    "a.aiff": ([*_A, "-c:a", "pcm_s16be"], "audio/x-aiff", PlaybackNeed.CONVERT_AUDIO),
    "a.wma": ([*_A, "-c:a", "wmav2"], "audio/x-ms-wma", PlaybackNeed.CONVERT_AUDIO),
    "alac.m4a": ([*_A, "-c:a", "alac"], "audio/mp4", PlaybackNeed.CONVERT_AUDIO),
    "adpcm.wav": ([*_A, "-c:a", "adpcm_ms"], "audio/wav", PlaybackNeed.CONVERT_AUDIO),
    "a.ac3": ([*_A, "-c:a", "ac3"], "audio/ac3", PlaybackNeed.CONVERT_AUDIO),
    "a.mp2": ([*_A, "-c:a", "mp2"], "audio/mpeg", PlaybackNeed.CONVERT_AUDIO),
    # --- video whose picture no browser shows: audio-only preview
    "v.avi": (
        [*_V, "-c:v", "mpeg4", "-c:a", "libmp3lame"],
        "video/x-msvideo",
        PlaybackNeed.AUDIO_ONLY_PREVIEW,
    ),
    "v.wmv": (
        [*_V, "-c:v", "wmv2", "-c:a", "wmav2"],
        "video/x-ms-wmv",
        PlaybackNeed.AUDIO_ONLY_PREVIEW,
    ),
    "v.mpg": (
        [*_V, "-c:v", "mpeg2video", "-c:a", "mp2"],
        "video/mpeg",
        PlaybackNeed.AUDIO_ONLY_PREVIEW,
    ),
    "v.ts": ([*_V, *_H264, "-c:a", "aac"], "video/mp2t", PlaybackNeed.AUDIO_ONLY_PREVIEW),
    "v.flv": ([*_V, *_H264, "-c:a", "aac"], "video/x-flv", PlaybackNeed.AUDIO_ONLY_PREVIEW),
    # MPEG-4 Part 2 inside MP4: both browsers play the sound with a black picture.
    "mpeg4.mp4": (
        [*_V, "-c:v", "mpeg4", "-c:a", "aac"],
        "video/mp4",
        PlaybackNeed.AUDIO_ONLY_PREVIEW,
    ),
    # H.264 in 3GP plays in Chromium; Firefox refuses the container.
    "v.3gp": ([*_V, *_H264, "-c:a", "aac"], "video/3gpp", PlaybackNeed.AUDIO_ONLY_PREVIEW),
}

_PLAYABLE = sorted(n for n, (_, _, need) in FIXTURES.items() if need is PlaybackNeed.NONE)
_UNPLAYABLE = sorted(n for n in FIXTURES if n not in _PLAYABLE)


@pytest.fixture(scope="module")
def media(tmp_path_factory) -> dict[str, Path]:
    out: Path = tmp_path_factory.mktemp("playback_media")
    paths: dict[str, Path] = {}
    for name, (args, _, _) in FIXTURES.items():
        path = out / name
        subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-y", *args, str(path)], check=True, timeout=60
        )
        paths[name] = path
    return paths


def _probe_json(data: bytes, tmp_path: Path) -> dict:
    path = tmp_path / "probe.bin"
    path.write_bytes(data)
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams"]
        + [str(path)],
        capture_output=True,
        check=True,
        timeout=30,
    )
    parsed: dict = json.loads(result.stdout)
    return parsed


class FakeStore:
    """The object store, in memory. Keys are object names, values their bytes + type."""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.tags: list[tuple[str, bool]] = []

    def download_file_to_path(self, object_name: str, file_path: str) -> None:
        Path(file_path).write_bytes(self.objects[object_name][0])

    def upload_file(
        self, file_content: BinaryIO, file_size: int, object_name: str, content_type: str
    ) -> str:
        data = file_content.read()
        assert len(data) == file_size
        self.objects[object_name] = (data, content_type)
        return object_name

    def delete_file(self, object_name: str) -> None:
        self.objects.pop(object_name, None)

    def set_object_quarantine_tag(self, object_name, quarantined, **_kw) -> bool:
        self.tags.append((object_name, quarantined))
        return object_name in self.objects


@pytest.fixture
def store(monkeypatch) -> FakeStore:
    from app.services import minio_service

    fake = FakeStore()
    for name in (
        "download_file_to_path",
        "upload_file",
        "delete_file",
        "set_object_quarantine_tag",
    ):
        monkeypatch.setattr(minio_service, name, getattr(fake, name))
    return fake


@pytest.fixture
def own_sessions(monkeypatch, db_session):
    """The pipeline opens its own sessions; point them at the test's savepoint."""
    from app.tasks import playback_rendition as task_module
    from app.tasks.transcription import preprocess

    @contextlib.contextmanager
    def _scope():
        yield db_session

    monkeypatch.setattr(task_module, "session_scope", _scope)
    monkeypatch.setattr(preprocess, "session_scope", _scope)
    return db_session


@pytest.fixture
def queued(monkeypatch) -> list[dict]:
    """Capture ``media.create_playback_rendition`` dispatches instead of publishing them."""
    from app.tasks.playback_rendition import create_playback_rendition_task

    calls: list[dict] = []
    monkeypatch.setattr(create_playback_rendition_task, "delay", lambda **kw: calls.append(kw))
    return calls


def _row(db, user, name: str, store: FakeStore, media: dict[str, Path]) -> MediaFile:
    file_uuid = uuid.uuid4()
    storage_path = f"user_{user.id}/file_{file_uuid.hex[:8]}/{name}"
    store.objects[storage_path] = (media[name].read_bytes(), FIXTURES[name][1])
    row = MediaFile(
        uuid=file_uuid,
        filename=name,
        title=name,
        storage_path=storage_path,
        content_type=FIXTURES[name][1],
        file_size=media[name].stat().st_size,
        status="processing",
        is_public=False,
        user_id=user.id,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _stream_url(client, headers, file_uuid, monkeypatch) -> tuple[dict, list]:
    """GET /stream-url against a fake presign client; returns (body, [(object, headers)])."""
    from app.services import storage_presign_identity

    signed: list[tuple[str, dict | None]] = []

    class _FakePresignClient:
        def presigned_get_object(self, bucket_name, object_name, expires, response_headers=None):
            signed.append((object_name, response_headers))
            return f"https://storage.example.test/{bucket_name}/{object_name}?X-Amz-Signature=x"

    monkeypatch.setenv("SKIP_S3", "False")
    monkeypatch.setattr(storage_presign_identity, "presign_client", _FakePresignClient)
    response = client.get(
        f"/api/files/{file_uuid}/stream-url", params={"media_type": "video"}, headers=headers
    )
    assert response.status_code == status.HTTP_200_OK, response.text
    return response.json(), signed


# --------------------------------------------------------------------------- the matrix


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_measured_playability_matrix(media, name):
    """Each real file is classified the way both browsers were measured to treat it."""
    assert pr.classify_playback(pr.probe_media(str(media[name]))) is FIXTURES[name][2]


def test_cover_art_does_not_turn_audio_into_video(media, tmp_path):
    """An MP3 with embedded cover art has a video stream to ffprobe. It is still audio."""
    cover = tmp_path / "cover.png"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=red:s=32x32"]
        + ["-frames:v", "1", str(cover)],
        check=True,
        timeout=30,
    )
    tagged = tmp_path / "with_cover.mp3"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-i", str(media["a.mp3"]), "-i", str(cover)]
        + ["-map", "0:a", "-map", "1:v", "-c", "copy", "-disposition:v", "attached_pic"]
        + [str(tagged)],
        check=True,
        timeout=30,
    )
    probe = pr.probe_media(str(tagged))

    assert probe.video_codec is None
    assert pr.classify_playback(probe) is PlaybackNeed.NONE


def test_a_surround_hi_res_original_becomes_a_stereo_48k_rendition(tmp_path):
    source = tmp_path / "surround.aiff"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi"]
        + ["-i", "sine=frequency=440:duration=0.5:sample_rate=96000"]
        + ["-ac", "6", "-c:a", "pcm_s24be", str(source)],
        check=True,
        timeout=30,
    )
    output = tmp_path / "out.m4a"

    pr.encode_audio_rendition(str(source), str(output), pr.probe_media(str(source)))

    rendition = pr.probe_media(str(output))
    assert (rendition.audio_codec, rendition.channels, rendition.sample_rate) == ("aac", 2, 48000)


# ------------------------------------------------------------------ AIFF: upload → play


def test_aiff_upload_gets_a_rendition_that_stream_url_serves(
    client,
    user_token_headers,
    normal_user,
    db_session,
    own_sessions,
    store,
    queued,
    media,
    monkeypatch,
    tmp_path,
):
    """The reported case, end to end: upload an AIFF, preprocess it, run the queued
    task, and the player is handed an AAC/M4A rendition, not the undecodable original."""
    from app.services import minio_service
    from app.tasks.playback_rendition import create_playback_rendition_task
    from app.tasks.transcription import preprocess

    def _to_store(content, size, path, content_type):
        store.objects[path] = (
            content.read() if hasattr(content, "read") else content,
            content_type,
        )

    monkeypatch.setattr("app.api.endpoints.files.upload.upload_file_to_storage", _to_store)
    upload = client.post(
        "/api/files",
        headers=user_token_headers,
        files={"file": ("meeting.aiff", io.BytesIO(media["a.aiff"].read_bytes()), "audio/x-aiff")},
    )
    assert upload.status_code == status.HTTP_200_OK, upload.text
    file_uuid = upload.json()["uuid"]
    row = db_session.query(MediaFile).filter(MediaFile.uuid == file_uuid).one()
    original = str(row.storage_path)
    assert original in store.objects

    # Preprocess, for real: download, metadata, WAV conversion, then the probe.
    monkeypatch.setattr(preprocess, "superseded_result", lambda *a, **k: None)
    monkeypatch.setattr(preprocess, "stage_engine_shared_volume_wav", lambda *a, **k: "")
    monkeypatch.setattr(minio_service, "upload_temp_audio", lambda uuid_, path: f"temp/{uuid_}")
    result = preprocess.preprocess_for_transcription.run(
        file_uuid=file_uuid, task_id=f"t-{uuid.uuid4().hex[:8]}", diarization_source="off"
    )
    assert result["file_uuid"] == file_uuid
    assert queued == [{"file_uuid": file_uuid}]

    outcome = create_playback_rendition_task.run(file_uuid=file_uuid)

    assert outcome["status"] == "created", outcome
    db_session.expire_all()
    row = db_session.query(MediaFile).filter(MediaFile.uuid == file_uuid).one()
    assert row.playback_path == pr.rendition_object_name(original)
    data, stored_type = store.objects[row.playback_path]
    assert stored_type == "audio/mp4"
    probed = pr.parse_ffprobe(_probe_json(data, tmp_path))
    assert probed.audio_codec == "aac" and pr.classify_playback(probed) is PlaybackNeed.NONE
    assert original in store.objects, "the original must stay, it is the download"

    body, signed = _stream_url(client, user_token_headers, file_uuid, monkeypatch)
    assert body["playback"] == "converted"
    assert body["content_type"] == "audio/mp4"
    assert signed == [(row.playback_path, {"response-content-type": "audio/mp4"})]


@pytest.mark.parametrize("name", _PLAYABLE)
def test_playable_originals_cost_nothing(
    normal_user, db_session, own_sessions, store, queued, media, name
):
    """No task is queued for a playable original, and even a stray task stores nothing."""
    from app.tasks.playback_rendition import create_playback_rendition_task
    from app.tasks.transcription import preprocess

    row = _row(db_session, normal_user, name, store, media)
    preprocess._dispatch_playback_rendition_if_needed(int(row.id), str(row.uuid), str(media[name]))
    assert queued == []

    before = set(store.objects)
    assert create_playback_rendition_task.run(file_uuid=str(row.uuid)) == {"status": "not_needed"}
    assert set(store.objects) == before
    db_session.refresh(row)
    assert row.playback_path is None


@pytest.mark.parametrize("name", _UNPLAYABLE)
def test_unplayable_originals_queue_a_rendition(
    normal_user, db_session, own_sessions, store, queued, media, name
):
    from app.tasks.transcription import preprocess

    row = _row(db_session, normal_user, name, store, media)
    preprocess._dispatch_playback_rendition_if_needed(int(row.id), str(row.uuid), str(media[name]))
    assert queued == [{"file_uuid": str(row.uuid)}]


def test_a_file_that_already_has_a_rendition_is_not_encoded_again(
    normal_user, db_session, own_sessions, store, queued, media
):
    """Reprocessing an AIFF reuses its rendition rather than paying for the encode twice."""
    from app.tasks.playback_rendition import create_playback_rendition_task
    from app.tasks.transcription import preprocess

    row = _row(db_session, normal_user, "a.aiff", store, media)
    assert create_playback_rendition_task.run(file_uuid=str(row.uuid))["status"] == "created"
    objects = dict(store.objects)

    preprocess._dispatch_playback_rendition_if_needed(
        int(row.id), str(row.uuid), str(media["a.aiff"])
    )
    assert queued == []
    assert create_playback_rendition_task.run(file_uuid=str(row.uuid)) == {"status": "exists"}
    assert store.objects == objects


# --------------------------------------------------------------- video: audio-only preview


def test_avi_plays_its_audio_track_as_an_audio_only_preview(
    client, user_token_headers, normal_user, db_session, own_sessions, store, media, monkeypatch
):
    from app.tasks.playback_rendition import create_playback_rendition_task

    row = _row(db_session, normal_user, "v.avi", store, media)
    assert create_playback_rendition_task.run(file_uuid=str(row.uuid))["need"] == (
        "audio_only_preview"
    )

    body, signed = _stream_url(client, user_token_headers, str(row.uuid), monkeypatch)

    assert body["playback"] == "audio_only"
    assert body["content_type"] == "audio/mp4"
    assert signed[0][0] == pr.rendition_object_name(str(row.storage_path))


def test_a_playable_original_is_served_as_itself(
    client, user_token_headers, normal_user, db_session, store, media, monkeypatch
):
    row = _row(db_session, normal_user, "v.mp4", store, media)

    body, signed = _stream_url(client, user_token_headers, str(row.uuid), monkeypatch)

    assert body["playback"] == "original"
    assert body["content_type"] == "video/mp4"
    assert signed == [(row.storage_path, {"response-content-type": "video/mp4"})]


# ----------------------------------------------------------------- accepted uploads


# AMR is built by hand: common ffmpeg builds ship its decoder but not its encoder. The
# upload check reads only the magic bytes; the frames are one silent AMR-NB 12.2k frame.
_AMR_NB = b"#!AMR\n" + bytes([0x3C]) + bytes(31)


@pytest.mark.parametrize(
    ("filename", "declared"),
    [
        ("v.flv", "video/x-flv"),
        ("memo.amr", "audio/amr"),
        ("a.ac3", "audio/ac3"),
        ("a.mp2", "audio/mpeg"),
    ],
)
def test_formats_advertised_as_converted_or_previewed_are_accepted(
    client, user_token_headers, media, monkeypatch, filename, declared
):
    """Each used to fail the magic-byte check as "not a valid audio/video file", though
    ffmpeg decodes them all and the upload dialog offered FLV."""
    monkeypatch.setattr("app.api.endpoints.files.upload.upload_file_to_storage", lambda *a: None)
    data = _AMR_NB if filename.endswith(".amr") else media[filename].read_bytes()

    response = client.post(
        "/api/files",
        headers=user_token_headers,
        files={"file": (filename, io.BytesIO(data), declared)},
    )

    assert response.status_code == status.HTTP_200_OK, response.text


# ----------------------------------------------------------------- deletion and takedown


def test_deleting_the_file_deletes_its_rendition(
    normal_user, db_session, own_sessions, store, media, monkeypatch
):
    """The canonical purge (single delete, bulk, retention, GDPR) takes the copy too."""
    from app.services import file_cleanup_service as fcs
    from app.services import minio_service
    from app.services import video_processing_service
    from app.tasks.playback_rendition import create_playback_rendition_task

    class _NoDerivedCache:
        """The subtitle/extracted-audio cache is a separate store, out of scope here."""

        def __init__(self, *_args) -> None:
            pass

        def clear_derived_cache(self, *_args) -> None:
            pass

    monkeypatch.setattr(fcs, "_cleanup_opensearch_for_file", lambda target, file_uuid: [])
    monkeypatch.setattr(fcs, "_cleanup_empty_clusters", lambda db, owner_id: None)
    monkeypatch.setattr(minio_service, "MinIOService", lambda: None)
    monkeypatch.setattr(video_processing_service, "VideoProcessingService", _NoDerivedCache)
    row = _row(db_session, normal_user, "a.aiff", store, media)
    create_playback_rendition_task.run(file_uuid=str(row.uuid))
    db_session.refresh(row)
    rendition, original = str(row.playback_path), str(row.storage_path)
    assert rendition in store.objects

    result = fcs.purge_media_file(db_session, row)

    assert result["deleted"] is True, result
    assert result["residual_errors"] == []
    assert original not in store.objects
    assert rendition not in store.objects


def test_account_purge_plan_names_the_rendition(normal_user, db_session):
    from app.services import file_cleanup_service as fcs

    db_session.add(
        MediaFile(
            filename="a.aiff",
            storage_path=f"user_{normal_user.id}/f/a.aiff",
            playback_path=f"user_{normal_user.id}/f/a.aiff.playback.m4a",
            content_type="audio/x-aiff",
            file_size=10,
            user_id=normal_user.id,
        )
    )
    db_session.commit()

    plan = fcs.load_account_purge_plans(db_session, normal_user.id)

    assert [f["playback_path"] for f in plan.files] == [
        f"user_{normal_user.id}/f/a.aiff.playback.m4a"
    ]


def test_takedown_revokes_and_release_restores_the_rendition_url(admin_user, db_session, store):
    from app.services.takedown_service import quarantine_file
    from app.services.takedown_service import release_file

    row = MediaFile(
        filename="a.aiff",
        storage_path=f"user_{admin_user.id}/f/a.aiff",
        playback_path=f"user_{admin_user.id}/f/a.aiff.playback.m4a",
        content_type="audio/x-aiff",
        file_size=10,
        user_id=admin_user.id,
        status="completed",
    )
    db_session.add(row)
    db_session.commit()
    store.objects[row.storage_path] = (b"x", "audio/x-aiff")
    store.objects[row.playback_path] = (b"x", "audio/mp4")

    quarantine_file(db_session, row, admin=admin_user, reason="dmca", legal_hold=False)
    assert (row.playback_path, True) in store.tags

    release_file(db_session, row, admin=admin_user, clear_legal_hold=False)
    assert (row.playback_path, False) in store.tags


def test_a_rendition_finished_after_a_takedown_is_tagged_too(
    admin_user, db_session, own_sessions, store, media
):
    """The encode can outlast a takedown; the late object must not be a way around it."""
    from app.tasks.playback_rendition import create_playback_rendition_task

    row = _row(db_session, admin_user, "a.aiff", store, media)
    row.is_quarantined = True
    db_session.commit()

    create_playback_rendition_task.run(file_uuid=str(row.uuid))

    db_session.refresh(row)
    assert (row.playback_path, True) in store.tags
