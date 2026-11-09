"""``speaker_read.iter_speaker_embeddings``: mget batching, scroll paging, failure handling."""

from unittest.mock import MagicMock
from unittest.mock import patch

from app.services.opensearch_service import speaker_read

_CLIENT_PATH = "app.services.opensearch_service.client.opensearch_client"
_INDEX_PATH = "app.services.opensearch_service.speaker_read.get_active_speaker_index"


def _src(uuid, **extra):
    return {"speaker_uuid": uuid, "embedding": [0.1], "speaker_id": 7, **extra}


def _run(client, **kwargs):
    with patch(_CLIENT_PATH, client), patch(_INDEX_PATH, return_value="speakers_v4"):
        return list(speaker_read.iter_speaker_embeddings(1, **kwargs))


def test_no_client_yields_nothing():
    assert _run(None) == []


def test_mget_batches_by_batch_size_and_keeps_only_complete_found_docs():
    client = MagicMock()
    client.mget.side_effect = [
        {
            "docs": [
                {"found": True, "_source": _src("a", display_name="A")},
                {"found": False},
                {"found": True, "_source": {"speaker_uuid": "no-embedding"}},
            ]
        },
        {"docs": [{"found": True, "_source": _src("c")}]},
    ]

    batches = _run(client, speaker_uuids=["a", "b", "c"], batch_size=2)

    assert [c.kwargs["body"] for c in client.mget.call_args_list] == [
        {"ids": ["a", "b"]},
        {"ids": ["c"]},
    ]
    assert batches == [
        [
            {
                "speaker_uuid": "a",
                "embedding": [0.1],
                "speaker_id": 7,
                "profile_id": None,
                "display_name": "A",
            }
        ],
        [
            {
                "speaker_uuid": "c",
                "embedding": [0.1],
                "speaker_id": 7,
                "profile_id": None,
                "display_name": None,
            }
        ],
    ]


def test_failed_mget_batch_is_skipped_and_empty_batch_not_yielded():
    client = MagicMock()
    client.mget.side_effect = [
        RuntimeError("boom"),
        {"docs": [{"found": False}]},
        {"docs": [{"found": True, "_source": _src("z")}]},
    ]

    batches = _run(client, speaker_uuids=["a", "b", "z"], batch_size=1)

    assert [b[0]["speaker_uuid"] for b in batches] == ["z"]


def test_scroll_pages_with_search_after_and_stops_on_a_short_page():
    client = MagicMock()
    client.search.side_effect = [
        {"hits": {"hits": [{"_source": _src("a"), "sort": ["a"]}, {"_source": {}, "sort": ["b"]}]}},
        {"hits": {"hits": [{"_source": _src("c"), "sort": ["c"]}]}},
    ]

    batches = _run(client, batch_size=2)

    assert [[d["speaker_uuid"] for d in b] for b in batches] == [["a"], ["c"]]
    bodies = [c.kwargs["body"] for c in client.search.call_args_list]
    assert "search_after" not in bodies[0]
    assert bodies[1]["search_after"] == ["b"]
    assert len(bodies) == 2  # the one-hit page was short, so no third query
    assert {"term": {"user_id": 1}} in bodies[0]["query"]["bool"]["filter"]


def test_scroll_failure_ends_the_iteration():
    client = MagicMock()
    client.search.side_effect = RuntimeError("down")

    assert _run(client) == []
