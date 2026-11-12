"""The demo chat generator's file matching, prompt filtering and 429 handling (#628).

It is a standalone script (it runs against a throwaway stack over HTTP), so it is loaded by
path. Failures this pins: a normal upload never carries the manifest title, so title-only
matching made "upload, then generate" fail; a prompt naming an item the bundle dropped made
the run die with a KeyError; and the hourly chat limit (429) killed a whole run instead of
being waited out.
"""

from __future__ import annotations

import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler
from http.server import HTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "demo" / "generate_demo_conversations.py"


@pytest.fixture(scope="module")
def gen():
    spec = importlib.util.spec_from_file_location("generate_demo_conversations", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ITEMS = {
    "a": {"id": "a", "title": "Manifest Title A", "media": "media/a.mp4"},
    "b": {"id": "b", "title": "Manifest Title B", "media": "media/b.mp4"},
    "c": {"id": "c", "title": "Manifest Title C", "media": "media/c.mp3"},
}


def test_a_seeded_file_is_matched_on_the_key_the_seeder_records_not_its_title(gen):
    files = [{"uuid": "u-a", "filename": "demo__a.transcript", "title": "Edited by an admin"}]

    found, missing = gen.match_files(files, {"a": ITEMS["a"]})

    assert found == {"a": "u-a"}
    assert missing == []


def test_an_uploaded_file_is_matched_on_its_media_filename(gen):
    files = [{"uuid": "u-b", "filename": "b.mp4", "title": "Title from the media metadata"}]

    found, missing = gen.match_files(files, {"b": ITEMS["b"]})

    assert found == {"b": "u-b"}
    assert missing == []


def test_the_manifest_title_is_the_last_resort_and_unmatched_items_are_reported(gen):
    files = [{"uuid": "u-c", "filename": "other.mp3", "title": "Manifest Title C"}]

    found, missing = gen.match_files(files, ITEMS)

    assert found == {"c": "u-c"}
    assert missing == ["a", "b"]


def test_the_seeded_filename_matches_what_the_seeder_writes(gen):
    seeder = (REPO_ROOT / "backend/app/scripts/demo_seed/__main__.py").read_text(encoding="utf-8")
    injector = (REPO_ROOT / "backend/app/scripts/corpus_injection/injector.py").read_text(
        encoding="utf-8"
    )

    assert f'corpus="{gen.SEEDED_CORPUS}"' in seeder
    assert 'f"{doc.corpus}__{doc.meeting_id}.transcript"' in injector
    assert gen.seeded_filename("x-1") == "demo__x-1.transcript"


def test_prompts_naming_items_outside_the_bundle_are_split_out_not_fatal(gen):
    prompts = [
        {"id": "ok", "items": ["a", "b"]},
        {"id": "gone", "items": ["a", "nasa-edu-black-hole-simulation"]},
    ]

    runnable, skipped = gen.split_prompts(prompts, {"a", "b"})

    assert [p["id"] for p in runnable] == ["ok"]
    assert skipped == [("gone", ["nasa-edu-black-hole-simulation"])]


def test_the_shipped_prompts_name_no_item_that_was_dropped(gen):
    prompts = json.loads((REPO_ROOT / "demo" / "conversation-prompts.json").read_text("utf-8"))
    named = {i for conv in prompts["conversations"] for i in conv["items"]}

    dropped = {"nasa-edu-black-hole-simulation", "nasa-earth-from-orbit-2013"}

    assert named & dropped == set()


def _limited_handler(counter: list[int]) -> type[BaseHTTPRequestHandler]:
    """A server that answers the first two POSTs 429 with Retry-After, then 200."""

    def handle_post(self: BaseHTTPRequestHandler) -> None:
        counter[0] += 1
        if counter[0] <= 2:
            self.send_response(429)
            self.send_header("Retry-After", "1")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def quiet(self: BaseHTTPRequestHandler, *_args: object) -> None:
        return None

    # http.server dispatches on the method name `do_POST`.
    return type(
        "Limited", (BaseHTTPRequestHandler,), {"do_POST": handle_post, "log_message": quiet}
    )


def test_a_chat_429_is_waited_out_and_the_message_is_sent(gen):
    hits = [0]
    server = HTTPServer(("127.0.0.1", 0), _limited_handler(hits))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        import requests
        from requests.adapters import HTTPAdapter

        s = requests.Session()
        s.mount(base, HTTPAdapter(max_retries=gen.RETRY))

        gen._ask(s, base, "conv-1", "a question")

        assert hits[0] == 3
    finally:
        server.shutdown()


def test_a_read_timeout_on_the_streamed_post_is_not_replayed(gen):
    from http.server import ThreadingHTTPServer

    import requests
    from requests.adapters import HTTPAdapter

    hits = [0]
    release = threading.Event()

    def slow_post(self: BaseHTTPRequestHandler) -> None:
        hits[0] += 1
        release.wait(timeout=5)

    handler = type(
        "Slow", (BaseHTTPRequestHandler,), {"do_POST": slow_post, "log_message": lambda *a: None}
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        s = requests.Session()
        s.mount(base, HTTPAdapter(max_retries=gen.RETRY))

        with pytest.raises(requests.exceptions.ConnectionError, match="Read timed out"):
            s.post(f"{base}/chat", json={}, timeout=0.5)

        assert hits[0] == 1
    finally:
        release.set()
        server.shutdown()
