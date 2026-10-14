"""Caption surfaces over HTTP: the player track and the SRT/WebVTT downloads.

``GET /files/{uuid}/captions`` is the file-detail player's WebVTT track (it used to be
assembled in the browser, one cue per diarized segment). The subtitle download and the
transcript export cut their SRT/WebVTT with the same segmenter, so all three are driven
here against one fabricated 90-second single-speaker segment: the case the owner
reported, where the whole segment covered the video at once.

Every assertion reads the bytes a client receives. Rows live on the savepoint-isolated
``db_session``.
"""

from __future__ import annotations

import re
import uuid

import pytest
from fastapi import status

from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import TranscriptSegment
from app.models.prompt import UserSetting

LONG_TURN = (
    "So what we found over the last quarter is that the retention numbers, which everyone "
    "was worried about, actually improved by 3.5 percent, and the analytics team thinks that "
    "is mostly because of the onboarding changes we shipped in March. But we still need to "
    "look at churn in the enterprise segment, because those accounts behave very differently "
    "and the sample is small. Damn it, I would like to propose that we run a proper cohort "
    "analysis before the board meeting next month and come back with a recommendation that "
    "everyone can sign off on without another round of debate about the methodology."
)
PROFANITY = "Damn"

_TIMING_RE = re.compile(r"^(\d\d:\d\d:\d\d[.,]\d{3}) --> (\d\d:\d\d:\d\d[.,]\d{3})$", re.M)


def _cues(body: str) -> list[tuple[str, str, list[str]]]:
    """``[(start, end, lines)]`` from a WebVTT or SRT body."""
    out = []
    blocks = re.split(r"\n\s*\n", body.strip())
    for block in blocks:
        lines = block.splitlines()
        for i, line in enumerate(lines):
            match = _TIMING_RE.match(line)
            if match:
                out.append(
                    (
                        match.group(1).replace(",", "."),
                        match.group(2).replace(",", "."),
                        lines[i + 1 :],
                    )
                )
                break
    return out


def _visible(line: str) -> str:
    """A WebVTT cue line as the viewer reads it (markup removed, entities decoded)."""
    text = re.sub(r"<[^>]+>", "", line)
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def _make_file(db_session, owner, *, redaction_status: str = "done") -> MediaFile:
    file_uuid = str(uuid.uuid4())
    media_file = MediaFile(
        uuid=file_uuid,
        filename="captions_test.mp4",
        title="captions_test",
        storage_path=f"media/test/{file_uuid}.mp4",
        content_type="video/mp4",
        file_size=4096,
        status="completed",
        is_public=False,
        user_id=owner.id,
        redaction_status=redaction_status,
    )
    db_session.add(media_file)
    db_session.commit()
    db_session.refresh(media_file)
    return media_file


def _add_speaker(db_session, media_file, **fields) -> Speaker:
    speaker = Speaker(
        uuid=str(uuid.uuid4()),
        media_file_id=media_file.id,
        user_id=media_file.user_id,
        **fields,
    )
    db_session.add(speaker)
    db_session.commit()
    db_session.refresh(speaker)
    return speaker


def _add_long_segment(db_session, media_file, speaker: Speaker | None) -> None:
    start = LONG_TURN.index(PROFANITY)
    db_session.add(
        TranscriptSegment(
            media_file_id=media_file.id,
            speaker_id=speaker.id if speaker else None,
            start_time=0.0,
            end_time=90.0,
            text=LONG_TURN,
            redactions=[
                {
                    "char_start": start,
                    "char_end": start + len(PROFANITY),
                    "category": "profanity",
                    "entity_type": "PROFANITY",
                    "detector": "wordlist",
                }
            ],
        )
    )
    db_session.commit()


def _prefs(db_session, user, **prefs: str) -> None:
    for key, value in prefs.items():
        db_session.add(UserSetting(user_id=user.id, setting_key=key, setting_value=value))
    db_session.commit()


@pytest.fixture
def long_file(db_session, normal_user):
    media_file = _make_file(db_session, normal_user)
    speaker = _add_speaker(db_session, media_file, name="SPEAKER_01", display_name="Alice")
    _add_long_segment(db_session, media_file, speaker)
    return media_file


def _get(client, headers, path: str, **params):
    return client.get(path, headers=headers, params=params)


class TestPlayerTrack:
    def test_a_long_segment_is_served_as_short_inline_cues(
        self, client, user_token_headers, long_file
    ):
        response = _get(client, user_token_headers, f"/api/files/{long_file.uuid}/captions")

        assert response.status_code == status.HTTP_200_OK
        assert response.headers["content-type"].startswith("text/vtt")
        assert "attachment" not in response.headers.get("content-disposition", "")
        assert response.headers["cache-control"] == "no-store"
        cues = _cues(response.text)
        assert len(cues) >= 10, f"a 90 s segment produced {len(cues)} cue(s)"
        for _start, _end, lines in cues:
            assert 1 <= len(lines) <= 2, lines
            assert all(len(_visible(line)) <= 42 for line in lines), lines
        assert cues[0][2][0].startswith("<v Alice><c.speaker>Alice</c>: ")
        assert sum("<c.speaker>" in line for _, _, lines in cues for line in lines) == 1

    def test_the_label_is_the_canonical_one_never_an_llm_guess(
        self, client, user_token_headers, db_session, normal_user
    ):
        media_file = _make_file(db_session, normal_user)
        speaker = _add_speaker(
            db_session,
            media_file,
            name="SPEAKER_01",
            suggested_name="Robert Guess",
            suggestion_source="llm_analysis",
            confidence=0.95,
        )
        _add_long_segment(db_session, media_file, speaker)

        response = _get(
            client,
            user_token_headers,
            f"/api/files/{media_file.uuid}/captions",
            speaker_placeholder="Speaker {number}",
        )

        assert response.status_code == status.HTTP_200_OK
        assert "Robert Guess" not in response.text
        assert "<c.speaker>Speaker 01</c>:" in response.text

    def test_the_readers_redaction_policy_masks_the_track(
        self, client, user_token_headers, db_session, normal_user, long_file
    ):
        _prefs(
            db_session, normal_user, redaction_enabled="true", redaction_categories='["profanity"]'
        )
        response = _get(client, user_token_headers, f"/api/files/{long_file.uuid}/captions")

        assert response.status_code == status.HTTP_200_OK
        assert PROFANITY not in response.text
        assert "[PROFANITY]" in response.text

    def test_the_track_is_withheld_while_the_scan_is_unfinished(
        self, client, user_token_headers, db_session, normal_user
    ):
        _prefs(
            db_session, normal_user, redaction_enabled="true", redaction_categories='["profanity"]'
        )
        media_file = _make_file(db_session, normal_user, redaction_status="processing")
        _add_long_segment(db_session, media_file, None)

        response = _get(client, user_token_headers, f"/api/files/{media_file.uuid}/captions")

        assert response.status_code == status.HTTP_409_CONFLICT
        assert PROFANITY not in response.text

    def test_another_users_file_is_not_served(self, client, other_user_auth_headers, long_file):
        response = _get(client, other_user_auth_headers, f"/api/files/{long_file.uuid}/captions")
        assert response.status_code == status.HTTP_403_FORBIDDEN  # as GET /files/{uuid}
        assert "Alice" not in response.text


class TestDownloadsAgreeWithThePlayer:
    def test_subtitle_and_export_files_use_the_same_cues(
        self, client, user_token_headers, long_file
    ):
        player = _cues(
            _get(client, user_token_headers, f"/api/files/{long_file.uuid}/captions").text
        )
        subtitles_vtt = _cues(
            _get(
                client,
                user_token_headers,
                f"/api/files/{long_file.uuid}/subtitles",
                subtitle_format="webvtt",
            ).text
        )
        subtitles_srt = _cues(
            _get(client, user_token_headers, f"/api/files/{long_file.uuid}/subtitles").text
        )
        export_vtt = _cues(
            _get(
                client, user_token_headers, f"/api/files/{long_file.uuid}/export", format="vtt"
            ).text
        )
        export_srt = _cues(
            _get(
                client, user_token_headers, f"/api/files/{long_file.uuid}/export", format="srt"
            ).text
        )

        timings = [(s, e) for s, e, _ in player]
        assert len(timings) >= 10
        for other in (subtitles_vtt, subtitles_srt, export_vtt, export_srt):
            assert [(s, e) for s, e, _ in other] == timings
        assert export_srt[0][2][0] == "Alice: " + _visible(player[0][2][0]).split(": ", 1)[1]

    @pytest.mark.parametrize(
        ("path", "params"),
        [
            ("captions", {}),
            ("subtitles", {"subtitle_format": "srt"}),
            ("subtitles", {"subtitle_format": "txt"}),
            ("export", {"format": "vtt"}),
            ("export", {"format": "txt"}),
            ("export", {"format": "json"}),
            ("export", {"format": "csv"}),
        ],
    )
    def test_the_blur_style_never_writes_the_original_into_text(
        self, client, user_token_headers, db_session, normal_user, long_file, path, params
    ):
        """``blur``'s placeholder wraps the ORIGINAL word for the page to blur with CSS.

        A file or a caption cue has no CSS, so that span is the original in plain text.
        """
        _prefs(
            db_session,
            normal_user,
            redaction_enabled="true",
            redaction_categories='["profanity"]',
            redaction_style="blur",
        )
        response = _get(client, user_token_headers, f"/api/files/{long_file.uuid}/{path}", **params)

        assert response.status_code == status.HTTP_200_OK
        assert "the board meeting" in response.text  # the transcript is there...
        assert PROFANITY not in response.text  # ...the masked word is not
        assert "[PROFANITY]" in response.text
