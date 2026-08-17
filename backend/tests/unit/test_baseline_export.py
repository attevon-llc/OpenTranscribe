"""``app/tasks/baseline_export.py`` -- transcript baseline snapshot + comparison,
the two Celery tasks behind the pipeline-benchmarking workflow
(``export_transcript_baseline`` / ``compare_transcript_baseline``, registered in
``app/core/celery.py``).

Both are driven via ``.run(...)`` (bypassing the broker, same convention as
``test_retention_cleanup_task.py``) against a real Postgres transaction and real
JSON files on disk (redirected into ``tmp_path`` by monkeypatching the module's
``BENCHMARKS_DIR`` constant), and against the REAL ``export_baseline`` /
``compare_transcripts`` implementations in ``app/utils/transcript_comparison.py``
-- not mocks, per this repo's "no mocking in production code paths" convention.
"""

from __future__ import annotations

import json
import uuid

import pytest

from app.core.enums import FileStatus
from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import TranscriptSegment
from app.tasks import baseline_export


@pytest.fixture(autouse=True)
def _redirect_benchmarks_dir(tmp_path, monkeypatch):
    """Every test gets its own throwaway BENCHMARKS_DIR instead of touching the
    real /tmp/benchmarks (or clobbering another test's files)."""
    monkeypatch.setattr(baseline_export, "BENCHMARKS_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def _route_session_scope_at_the_test_transaction(db_session, monkeypatch):
    """``export_baseline_task`` opens its own ``session_scope()``; point it at the
    savepoint-rolled-back test session so it can see fixture data (same pattern
    as ``test_retention_cleanup_task.py``)."""
    import contextlib

    monkeypatch.setattr(
        baseline_export, "session_scope", lambda: contextlib.nullcontext(db_session), raising=True
    )


@pytest.fixture
def transcribed_file(db_session, normal_user):
    """A completed file with two ordered segments split across one named speaker."""
    mf = MediaFile(
        user_id=normal_user.id,
        filename=f"baseline-{uuid.uuid4().hex[:8]}.mp3",
        storage_path=f"baseline/{uuid.uuid4().hex}.mp3",
        file_size=4096,
        content_type="audio/mpeg",
        status=FileStatus.COMPLETED,
    )
    db_session.add(mf)
    db_session.commit()
    db_session.refresh(mf)

    speaker = Speaker(
        user_id=normal_user.id, media_file_id=mf.id, name="SPEAKER_00", display_name="Alice"
    )
    db_session.add(speaker)
    db_session.commit()
    db_session.refresh(speaker)

    seg1 = TranscriptSegment(
        media_file_id=mf.id,
        speaker_id=speaker.id,
        start_time=0.0,
        end_time=2.5,
        text="Hello there",
        is_overlap=False,
    )
    seg2 = TranscriptSegment(
        media_file_id=mf.id,
        speaker_id=speaker.id,
        start_time=2.5,
        end_time=5.0,
        text="General Kenobi",
        is_overlap=False,
    )
    db_session.add_all([seg1, seg2])
    db_session.commit()

    return mf


class TestExportBaselineTask:
    def test_reports_an_error_for_a_nonexistent_file_without_raising(self):
        missing_uuid = str(uuid.uuid4())

        result = baseline_export.export_baseline_task.run(missing_uuid)

        assert result == {"status": "error", "message": f"File {missing_uuid} not found"}

    def test_reports_an_error_for_a_malformed_uuid_without_raising(self):
        """Same fix as the not-found case: get_by_uuid_optional returns None for a
        string that doesn't even parse as a UUID, same as for a well-formed one
        that matches no row -- get_file_by_uuid raised HTTPException(400) for this
        instead, straight out of the task."""
        result = baseline_export.export_baseline_task.run("definitely-not-a-uuid")

        assert result == {
            "status": "error",
            "message": "File definitely-not-a-uuid not found",
        }

    def test_exports_the_real_segments_and_speakers_to_a_json_snapshot(
        self, transcribed_file, tmp_path
    ):
        result = baseline_export.export_baseline_task.run(str(transcribed_file.uuid), label="v1")

        assert result["status"] == "success"
        assert result["segment_count"] == 2

        written = json.loads((tmp_path / result["path"].rsplit("/", 1)[-1]).read_text())
        assert written["segment_count"] == 2
        assert written["speaker_count"] == 1
        texts = [seg["text"] for seg in written["segments"]]
        assert texts == ["Hello there", "General Kenobi"]
        assert all(seg["speaker_name"] == "SPEAKER_00" for seg in written["segments"])

    def test_output_filename_uses_the_short_uuid_and_the_given_label(
        self, transcribed_file, tmp_path
    ):
        file_uuid = str(transcribed_file.uuid)

        result = baseline_export.export_baseline_task.run(file_uuid, label="pre-upgrade")

        expected_name = f"{file_uuid[:8]}_pre-upgrade.json"
        assert result["path"].endswith(expected_name)
        assert (tmp_path / expected_name).exists()

    def test_creates_the_benchmarks_directory_when_it_does_not_exist_yet(
        self, transcribed_file, tmp_path, monkeypatch
    ):
        nested = tmp_path / "does" / "not" / "exist"
        assert not nested.exists()
        monkeypatch.setattr(baseline_export, "BENCHMARKS_DIR", str(nested))

        result = baseline_export.export_baseline_task.run(str(transcribed_file.uuid), label="v1")

        assert result["status"] == "success"
        assert nested.is_dir()

    def test_a_file_with_no_segments_exports_a_zero_count_snapshot(self, db_session, normal_user):
        mf = MediaFile(
            user_id=normal_user.id,
            filename="empty.mp3",
            storage_path=f"baseline/{uuid.uuid4().hex}.mp3",
            file_size=10,
            content_type="audio/mpeg",
            status=FileStatus.COMPLETED,
        )
        db_session.add(mf)
        db_session.commit()
        db_session.refresh(mf)

        result = baseline_export.export_baseline_task.run(str(mf.uuid), label="empty")

        assert result["status"] == "success"
        assert result["segment_count"] == 0


def _write_baseline(tmp_path, filename: str, segments: list[dict]) -> None:
    payload = {
        "file_id": 1,
        "segment_count": len(segments),
        "speaker_count": len({s["speaker_name"] for s in segments}),
        "segments": segments,
        "speakers": [],
    }
    (tmp_path / filename).write_text(json.dumps(payload))


class TestCompareBaselineTask:
    def test_reports_an_error_when_the_baseline_snapshot_is_missing(self):
        file_uuid = str(uuid.uuid4())

        result = baseline_export.compare_baseline_task.run(file_uuid, "before", "after")

        assert result["status"] == "error"
        assert "Baseline" in result["message"]

    def test_reports_an_error_when_the_current_snapshot_is_missing(self, tmp_path):
        file_uuid = str(uuid.uuid4())
        _write_baseline(
            tmp_path,
            f"{file_uuid[:8]}_before.json",
            [{"start": 0.0, "end": 1.0, "text": "hi", "speaker_name": "A"}],
        )

        result = baseline_export.compare_baseline_task.run(file_uuid, "before", "after")

        assert result["status"] == "error"
        assert "Current" in result["message"]

    def test_identical_snapshots_compare_as_a_full_pass(self, tmp_path):
        file_uuid = str(uuid.uuid4())
        segments = [
            {"start": 0.0, "end": 2.5, "text": "Hello there", "speaker_name": "Alice"},
            {"start": 2.5, "end": 5.0, "text": "General Kenobi", "speaker_name": "Alice"},
        ]
        _write_baseline(tmp_path, f"{file_uuid[:8]}_before.json", segments)
        _write_baseline(tmp_path, f"{file_uuid[:8]}_after.json", segments)

        result = baseline_export.compare_baseline_task.run(file_uuid, "before", "after")

        assert result["status"] == "success"
        comparison = result["comparison"]
        assert comparison["pass_overall"] is True
        assert comparison["text_exact_match_pct"] == 100.0
        assert comparison["timestamp_start_mae_seconds"] == 0.0

    def test_a_materially_different_current_snapshot_fails_the_comparison(self, tmp_path):
        file_uuid = str(uuid.uuid4())
        _write_baseline(
            tmp_path,
            f"{file_uuid[:8]}_before.json",
            [{"start": 0.0, "end": 2.5, "text": "Hello there", "speaker_name": "Alice"}],
        )
        _write_baseline(
            tmp_path,
            f"{file_uuid[:8]}_after.json",
            # Completely different text, wildly different timing, different speaker.
            [
                {
                    "start": 40.0,
                    "end": 42.5,
                    "text": "something unrelated entirely",
                    "speaker_name": "Bob",
                }
            ],
        )

        result = baseline_export.compare_baseline_task.run(file_uuid, "before", "after")

        assert result["status"] == "success"
        comparison = result["comparison"]
        assert comparison["pass_overall"] is False
        assert comparison["pass_text"] is False
        assert comparison["pass_timestamps"] is False

    def test_writes_a_comparison_report_file_matching_the_returned_metrics(self, tmp_path):
        file_uuid = str(uuid.uuid4())
        segments = [{"start": 0.0, "end": 1.0, "text": "hi", "speaker_name": "A"}]
        _write_baseline(tmp_path, f"{file_uuid[:8]}_before.json", segments)
        _write_baseline(tmp_path, f"{file_uuid[:8]}_after.json", segments)

        result = baseline_export.compare_baseline_task.run(file_uuid, "before", "after")

        report_path = tmp_path / "comparisons" / "after_vs_before.json"
        assert report_path.exists()
        on_disk = json.loads(report_path.read_text())
        assert on_disk == result["comparison"]
