"""``demo_seed export`` writes transcripts that ``validate`` accepts and ``import`` reads back (#628)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest.mock import create_autospec

import pytest
from fixtures.speaker_cleanup_seams import make_file
from fixtures.speaker_cleanup_seams import make_speaker
from sqlalchemy.orm import Session

from app.models.media import TranscriptSegment
from app.scripts.demo_seed.bundle import BundleError
from app.scripts.demo_seed.bundle import load_bundle
from app.scripts.demo_seed.export import export_transcripts


def _bundle(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    (src / "media").mkdir(parents=True)
    media = src / "media" / "a.mp3"
    media.write_bytes(b"audio")
    import hashlib

    manifest = {
        "schema": 1,
        "bundle_version": "t.1",
        "items": [
            {
                "id": "a",
                "title": "Talk A",
                "language": "en",
                "collection": "news",
                "media": "media/a.mp3",
                "media_sha256": hashlib.sha256(b"audio").hexdigest(),
                "transcript": "transcripts/a.json",
                "source_url": "https://example.gov",
                "license": "US-GOV-PD",
                "license_url": "https://example.gov/l",
                "attribution": "Gov",
                "contains_private_individuals": False,
                "synthetic_pii": False,
                "license_checked_on": "2026-11-08",
            }
        ],
    }
    (src / "manifest.json").write_text(json.dumps(manifest))
    return src


def test_export_round_trips_through_validate_and_import(db_session, normal_user, tmp_path):
    src = _bundle(tmp_path)
    media_file = make_file(db_session, normal_user)
    media_file.title = "Talk A"
    host = make_speaker(db_session, normal_user, media_file, "SPEAKER_00", display_name="Host")
    guest = make_speaker(db_session, normal_user, media_file, "SPEAKER_01")
    for start, end, who, text in [(2.0, 4.0, guest, "Thanks."), (0.0, 2.0, host, "Welcome.")]:
        db_session.add(
            TranscriptSegment(
                media_file_id=media_file.id,
                speaker_id=who.id,
                start_time=start,
                end_time=end,
                text=text,
            )
        )
    db_session.commit()

    out = tmp_path / "out"
    out.mkdir()
    shutil.copytree(src / "media", out / "media")
    export_transcripts(db_session, out, src / "manifest.json")

    version, items = load_bundle(out)  # the validate path
    assert version == "t.1"
    segments = items[0].segments()  # what import reads
    assert [(s.start, s.speaker, s.text) for s in segments] == [
        (0.0, "SPEAKER_00", "Welcome."),
        (2.0, "SPEAKER_01", "Thanks."),
    ]
    assert items[0].speaker_names() == {"SPEAKER_00": "Host"}


def test_export_names_every_item_with_no_finished_file(db_session, tmp_path):
    src = _bundle(tmp_path)
    with pytest.raises(BundleError) as err:
        export_transcripts(db_session, tmp_path / "out", src / "manifest.json")
    assert "a: no completed file titled 'Talk A'" in err.value.problems[0]


@pytest.mark.parametrize("kind", ["parent", "absolute"])
def test_export_refuses_a_transcript_path_outside_the_bundle(tmp_path, kind):
    src = _bundle(tmp_path)
    escape = "../outside.json" if kind == "parent" else str(tmp_path / "outside.json")
    manifest = json.loads((src / "manifest.json").read_text())
    manifest["items"][0]["transcript"] = escape
    (src / "manifest.json").write_text(json.dumps(manifest))
    bundle = tmp_path / "out"
    bundle.mkdir()

    with pytest.raises(BundleError, match="escapes the bundle"):
        export_transcripts(create_autospec(Session), bundle, src / "manifest.json")

    assert not (tmp_path / "outside.json").exists()
