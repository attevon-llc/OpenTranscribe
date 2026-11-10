"""Speaker list, picker and occurrence endpoints commit nothing, as before the query move."""

from __future__ import annotations

import pytest

from tests.commit_counter import endpoint_commits
from tests.fixtures.speaker_query_rows import committed_file_with_speaker

pytestmark = pytest.mark.usefixtures("speaker_os_seams")


@pytest.mark.parametrize(
    "path",
    [
        "/api/speakers",
        "/api/speakers?for_filter=true",
        "/api/speaker-profiles/profiles",
        "/api/speaker-profiles/collections",
        "/api/speaker-clusters/stats",
    ],
)
def test_speaker_read_endpoints_do_not_commit(
    client, user_token_headers, normal_user, db_session, path
):
    committed_file_with_speaker(db_session, normal_user)
    assert endpoint_commits(client, db_session, "GET", path, headers=user_token_headers) == 0


def test_cross_media_occurrences_do_not_commit(client, user_token_headers, normal_user, db_session):
    _, spk = committed_file_with_speaker(db_session, normal_user)
    url = f"/api/speakers/{spk.uuid}/cross-media"
    assert endpoint_commits(client, db_session, "GET", url, headers=user_token_headers) == 0
