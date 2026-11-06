"""Full-pipeline live smoke test: upload -> real ASR/diarization -> search -> chat.

Every other e2e suite in this repo either checks an upload merely LANDS in the backend
(``test_upload.py``'s ``test_submit_creates_file_and_cleans_up`` polls 30s for the file
*record*, never for completion) or drives the UI against files that are ALREADY
``status == "completed"`` from an earlier session (``test_file_detail_transcript.py``,
``test_search.py``: both filter ``status == "completed"`` out of whatever already sits
in dev data). Nothing in the suite pushes one fixture through the REAL pipeline start to
finish and asserts on what came out the other end. This file is that missing test.

Stages, all against the live dev stack, no mocks:
  1. Upload ``backend/tests/fixtures/media/sample_short.wav`` (10s of real speech, cut
     from a real video — see ``fixtures/media/README.md``). Deliberately NOT
     ``e2e/fixtures/sample_audio.wav``: that fixture is a silent 440Hz sine tone built
     for UI/upload-flow tests that never touch ASR — fed through the real pipeline it
     produces zero segments and lands in ``status == "error"``, never ``"completed"``
     (the same trap ``test_gpu_scale_smoke_live.py`` documents and avoids).
  2. Poll until ``status == "completed"`` with real, non-empty transcript segments —
     i.e. real WhisperX + diarization actually ran, not just "the upload was accepted".
  3. Pull a distinctive word straight out of the REAL transcript text (the wav's
     dialogue is not scripted, so nothing here is hardcoded) and confirm search finds
     this file for it — proves OpenSearch indexing caught up.
  4. The real LLM is configured BEFORE the upload, so the post-processing that follows the
     transcript (summary, topic suggestions) runs against it too. Assert the file ends up with
     a schema-valid ``summary_data`` and at least one topic suggestion. Before this, the
     provider was configured only for the chat test, after the upload had finished, so the
     summary and the topics ran against whatever was active earlier and nothing asserted on
     them.
  5. Ask a REAL local LLM (``--with-llm-test``'s vLLM — not the deterministic
     ``--with-mock-llm`` the rest of the chat suite uses) a question scoped to just this
     file, through the actual chat UI, and assert on a genuine non-empty answer.

Strict opt-in, like the GPU-diarization and mutation-test phases in
``run-dev-tests.sh``: every test here self-skips unless ``RUN_PIPELINE_SMOKE=1`` is set.
Never runs under plain ``--full``/``--fast``.
``run-dev-tests.sh --with-pipeline-smoke`` sets the env var and starts/stops
``--with-llm-test`` for you — a real GPU-backed model, several minutes to become healthy
the first time it needs to download.

Run directly (stack + ``--with-llm-test`` already up):
    RUN_PIPELINE_SMOKE=1 pytest backend/tests/e2e/test_full_pipeline_smoke.py -v -s
"""

from __future__ import annotations

import os
import socket
import time
import uuid as uuid_pkg
from collections.abc import Callable
from pathlib import Path

import pytest
import requests
from conftest import TEST_ADMIN_EMAIL
from conftest import TEST_ADMIN_PASSWORD
from conftest import delete_media_file
from playwright.sync_api import Page
from playwright.sync_api import expect

pytestmark = pytest.mark.pipeline_smoke

SAMPLE_WAV = Path(__file__).resolve().parents[1] / "fixtures" / "media" / "sample_short.wav"

LLM_TEST_PORT = int(os.environ.get("LLM_TEST_PORT", "5195"))
# The vLLM --served-model-name (docker-compose.llm-test.yml), NOT the HF repo id
# (LLM_TEST_MODEL env var, if set, is that repo id — e.g. "Chunity/gemma-4-E4B-it-AWQ-4bit").
# A config naming the repo id passes creation/set-active (both 200) but the live
# /api/llm/status health check reports available: false against it — vLLM's
# /v1/chat/completions only recognizes the served name.
LLM_TEST_SERVED_MODEL_NAME = os.environ.get("LLM_TEST_SERVED_MODEL_NAME", "gemma-4-e4b")
LLM_TEST_URL_FOR_BACKEND = "http://llm-test-vllm:8000/v1"

# Real inference, not a canned mock response: model load + retrieval + real generation
# for a first message can genuinely take a couple of minutes on a cold vLLM.
UPLOAD_COMPLETE_TIMEOUT_S = 600
# Summary and topic suggestions are separate NLP tasks queued after the transcript.
POST_PROCESSING_TIMEOUT_S = 600
CHAT_STREAM_TIMEOUT_MS = 180_000


def _pipeline_smoke_requested() -> bool:
    return os.environ.get("RUN_PIPELINE_SMOKE", "").lower() in ("1", "true", "yes")


def _llm_test_running() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", LLM_TEST_PORT)) == 0


@pytest.fixture(scope="module", autouse=True)
def _require_opt_in():
    if not _pipeline_smoke_requested():
        pytest.skip(
            "Full-pipeline smoke requires RUN_PIPELINE_SMOKE=1 (strict opt-in — see this "
            "file's module docstring, or run-dev-tests.sh --with-pipeline-smoke)"
        )
    if not SAMPLE_WAV.is_file():
        pytest.skip(f"missing fixture: {SAMPLE_WAV}")
    if not _llm_test_running():
        pytest.skip(
            f"no LLM reachable on localhost:{LLM_TEST_PORT} — start it with "
            "'./opentr.sh start dev --with-llm-test'"
        )


@pytest.fixture(scope="module")
def api_session(backend_url: str) -> requests.Session:
    """An authenticated API session for the whole module's arrange/act/cleanup calls."""
    session = requests.Session()
    response = session.post(
        f"{backend_url}/api/auth/token",
        data={"username": TEST_ADMIN_EMAIL, "password": TEST_ADMIN_PASSWORD},
        timeout=30,
    )
    assert response.status_code == 200, f"Login failed: {response.status_code}"
    csrf_token = session.cookies.get("csrf_token")
    assert csrf_token, "login did not set a csrf_token cookie"
    session.headers["X-CSRF-Token"] = csrf_token
    return session


@pytest.fixture(scope="module")
def auto_labelling_off(api_session: requests.Session, backend_url: str):
    """Keep the real model's topic suggestions from creating tags and collections.

    With a real LLM configured before the upload, ``AutoLabelService`` applies every
    suggestion at or above the confidence threshold: tags and collections named by a language
    model, on the shared account, that outlive the file this run deletes. The suggestions are
    still stored (and asserted on) -- only the applying is switched off, and restored.
    """
    url = f"{backend_url}/api/user-settings/auto-label"
    before = api_session.get(url, timeout=30)
    assert before.ok, f"could not read auto-label settings: {before.status_code} {before.text}"
    previously_enabled = bool(before.json().get("enabled", True))
    off = api_session.put(url, json={"enabled": False}, timeout=30)
    assert off.ok, f"could not disable auto-labelling: {off.status_code} {off.text}"
    try:
        yield
    finally:
        try:
            api_session.put(url, json={"enabled": previously_enabled}, timeout=30)
        except requests.RequestException:
            pass


@pytest.fixture(scope="module")
def llm_config(api_session: requests.Session, backend_url: str):
    """Register + activate a real local vLLM config; restore whatever was active before.

    Same shape as test_chat_grounding.py's llm_config_factory, pointed at the real
    --with-llm-test model instead of the deterministic mock — this suite exists
    specifically to exercise real generation, not canned tokens.
    """
    unique_name = f"Pipeline smoke vLLM {uuid_pkg.uuid4().hex[:8]}"
    response = api_session.post(
        f"{backend_url}/api/llm-settings",
        json={
            "name": unique_name,
            "provider": "custom",
            "model_name": LLM_TEST_SERVED_MODEL_NAME,
            "base_url": LLM_TEST_URL_FOR_BACKEND,
            "api_key": "not-needed-local",
        },
        timeout=30,
    )
    assert response.ok, f"Could not create LLM config: {response.status_code} {response.text}"
    config_uuid = str(response.json()["uuid"])

    # Everything after the create sits inside the try: a failed status read or activation
    # must still delete the config and restore whatever was active.
    prior_active_uuid = None
    try:
        status = api_session.get(f"{backend_url}/api/llm-settings/status", timeout=30)
        assert status.ok, f"Could not read prior LLM status: {status.status_code} {status.text}"
        active = status.json().get("active_configuration")
        prior_active_uuid = active["uuid"] if active else None

        activate = api_session.post(
            f"{backend_url}/api/llm-settings/set-active",
            json={"configuration_id": config_uuid},
            timeout=30,
        )
        assert activate.ok, f"Could not activate LLM config: {activate.status_code} {activate.text}"

        yield config_uuid
    finally:
        try:
            api_session.delete(f"{backend_url}/api/llm-settings/config/{config_uuid}", timeout=30)
        except requests.RequestException:
            pass
        if prior_active_uuid:
            try:
                api_session.post(
                    f"{backend_url}/api/llm-settings/set-active",
                    json={"configuration_id": prior_active_uuid},
                    timeout=30,
                )
            except requests.RequestException:
                pass


@pytest.fixture(scope="module")
def transcribed_file(
    api_session: requests.Session,
    backend_url: str,
    admin_token: str,
    llm_config: str,
    auto_labelling_off: None,
):
    """Upload the real-speech fixture and wait for the REAL pipeline to finish.

    ``llm_config`` and ``auto_labelling_off`` are requested here, not just by the tests that
    use the model: they must be in force BEFORE the upload, or the file's own summary and
    topic suggestions are generated by whatever provider was active earlier.

    Module-scoped: the three stages (transcribe, search, chat) are one pipeline run,
    not three independent arrangements — re-uploading per test would triple the GPU
    time for no extra coverage.
    """
    with open(SAMPLE_WAV, "rb") as fh:
        response = api_session.post(
            f"{backend_url}/api/files",
            files={"file": (f"pipeline-smoke-{uuid_pkg.uuid4().hex[:8]}.wav", fh, "audio/wav")},
            data={"title": "pipeline-smoke-test"},
            timeout=60,
        )
    assert response.ok, f"upload was rejected: {response.status_code} {response.text[:300]}"
    file_uuid = str(response.json()["uuid"])

    deadline = time.time() + UPLOAD_COMPLETE_TIMEOUT_S
    status = None
    try:
        while time.time() < deadline:
            resp = api_session.get(f"{backend_url}/api/files/{file_uuid}", timeout=15)
            if resp.ok:
                status = resp.json().get("status")
                if status == "completed":
                    break
                if status == "error":
                    pytest.fail(
                        f"real pipeline run landed in status=error for {file_uuid}: "
                        f"{resp.json().get('error_message')}"
                    )
            time.sleep(5)
        assert status == "completed", (
            f"upload never reached completed within {UPLOAD_COMPLETE_TIMEOUT_S}s "
            f"(last observed status: {status!r})"
        )

        # status == "completed" only means transcription/diarization finished — the
        # segments endpoint separately withholds transcript_segments (returns [] with
        # redaction_pending: true) until the independent async redaction step also
        # finishes, even though the segments already exist in the DB. Poll past that
        # too, or this reads a real, fully-transcribed file as if ASR produced nothing.
        segments: list[dict] = []
        redaction_deadline = time.time() + 120
        while time.time() < redaction_deadline:
            segments_resp = api_session.get(
                f"{backend_url}/api/files/{file_uuid}/segments",
                params={"page_size": 500},
                timeout=30,
            )
            assert segments_resp.ok, (
                f"could not fetch segments: {segments_resp.status_code} {segments_resp.text[:300]}"
            )
            body = segments_resp.json()
            if not body.get("redaction_pending"):
                segments = body.get("transcript_segments", [])
                break
            time.sleep(3)
        assert segments, (
            "real ASR produced zero transcript segments for a 10s real-speech clip "
            "(or redaction never finished within 120s) — the pipeline ran but this "
            "test never saw usable output, which is itself a real failure"
        )

        yield file_uuid, segments
    finally:
        # Not a single unchecked DELETE: a file whose follow-on stages are still running
        # answers 409, which used to leave this upload in the library. The shared helper
        # waits those out, retries, and falls back to /force.
        delete_media_file(backend_url, admin_token, file_uuid)


def _distinctive_word(segments: list[dict]) -> str:
    """Pick a real word (>=5 chars, alphabetic) out of the actual transcript.

    The dialogue is real, unscripted speech (see fixtures/media/README.md) — there is
    no fixed ground-truth word to search for, so this test derives one from whatever
    ASR actually produced, exactly like a real user searching for something they heard.
    """
    for segment in segments:
        for word in segment.get("text", "").split():
            cleaned = "".join(ch for ch in word if ch.isalpha())
            if len(cleaned) >= 5:
                return cleaned
    raise AssertionError(f"no word >=5 chars found across {len(segments)} segments")


def test_real_transcription_produces_segments(transcribed_file):
    """The pipeline stage itself: real WhisperX + diarization ran to completion."""
    _file_uuid, segments = transcribed_file
    assert all("text" in s and s["text"].strip() for s in segments), (
        "at least one segment has empty text — ASR ran but produced nothing usable"
    )


def test_search_finds_the_freshly_transcribed_file(
    transcribed_file, api_session: requests.Session, backend_url: str
):
    """Search indexing caught up with a file this run transcribed for real."""
    file_uuid, segments = transcribed_file
    word = _distinctive_word(segments)

    body: dict = {}
    deadline = time.time() + 60
    while time.time() < deadline:
        resp = api_session.get(
            f"{backend_url}/api/search",
            params={"q": word, "file_uuid": file_uuid},
            timeout=20,
        )
        assert resp.ok, f"search request failed: {resp.status_code} {resp.text[:300]}"
        body = resp.json()
        if body.get("total_results", 0) > 0:
            break
        time.sleep(3)
        continue

    assert body.get("total_results", 0) > 0, (
        f"search never found {file_uuid} for its own transcript word {word!r} "
        f"within 60s (last response: {body})"
    )
    result_uuids = [hit.get("file_uuid") for hit in body.get("results", [])]
    assert file_uuid in result_uuids, (
        f"search matched {body.get('total_results')} result(s) for {word!r} scoped to "
        f"{file_uuid}, but the returned hits name a different file: {result_uuids}"
    )


def _poll_json(
    api_session: requests.Session,
    url: str,
    *,
    ready: Callable[[requests.Response], dict | None],
    what: str,
    timeout_s: int,
) -> dict:
    """GET ``url`` until ``ready(response)`` returns a body, else fail naming the last answer."""
    deadline = time.time() + timeout_s
    last = "no response"
    while time.time() < deadline:
        resp = api_session.get(url, timeout=30)
        last = f"{resp.status_code} {resp.text[:200]}"
        body = ready(resp)
        if body is not None:
            return body
        time.sleep(5)
    pytest.fail(f"{what} did not appear within {timeout_s}s (last answer: {last})")


def test_the_real_llm_summary_is_stored_and_schema_valid(
    transcribed_file, api_session: requests.Session, backend_url: str
):
    """Post-processing summarized the file with the real model, and the result is usable."""
    from app.schemas.summary import SummaryData

    file_uuid, _segments = transcribed_file
    # Asked for explicitly as well: the automatic summary depends on a user preference, and
    # this test is about what the model produced, not about that preference. A summary that
    # already exists makes the request a no-op.
    requested = api_session.post(
        f"{backend_url}/api/files/management/bulk-action",
        json={"file_uuids": [file_uuid], "action": "summarize"},
        timeout=30,
    )
    assert requested.ok, f"summarize was rejected: {requested.status_code} {requested.text[:300]}"

    def stored_summary(resp: requests.Response) -> dict | None:
        if resp.status_code != 200:
            return None
        data = resp.json()["summary_data"]
        return dict(data) if isinstance(data, dict) and data.get("bluf") else None

    data = _poll_json(
        api_session,
        f"{backend_url}/api/files/{file_uuid}/summary",
        ready=stored_summary,
        what="a stored summary with a BLUF",
        timeout_s=POST_PROCESSING_TIMEOUT_S,
    )

    parsed = SummaryData.model_validate(data)
    assert isinstance(parsed.bluf, str) and len(parsed.bluf.strip()) > 10, parsed.bluf
    assert isinstance(parsed.brief_summary, str) and parsed.brief_summary.strip()
    assert isinstance(parsed.major_topics, list)
    assert "error" not in data, f"the summary is the parse-failure placeholder: {data.get('error')}"
    metadata = data["metadata"]
    assert metadata["model"] == LLM_TEST_SERVED_MODEL_NAME, (
        f"summarized by {metadata.get('model')!r}, not the model this run configured"
    )


def test_the_real_llm_produced_at_least_one_topic_suggestion(
    transcribed_file, api_session: requests.Session, backend_url: str
):
    """Topic extraction ran against the real model and stored a usable suggestion."""
    file_uuid, _segments = transcribed_file
    requested = api_session.post(f"{backend_url}/api/files/{file_uuid}/extract", timeout=30)
    assert requested.status_code == 202, (
        f"topic extraction was rejected: {requested.status_code} {requested.text[:300]}"
    )

    def stored_suggestion(resp: requests.Response) -> dict | None:
        if resp.status_code != 200:  # 204 until the extraction has written its row
            return None
        body = dict(resp.json())
        return body if body["suggested_tags"] or body["suggested_collections"] else None

    body = _poll_json(
        api_session,
        f"{backend_url}/api/files/{file_uuid}/suggestions",
        ready=stored_suggestion,
        what="a topic suggestion with at least one tag or collection",
        timeout_s=POST_PROCESSING_TIMEOUT_S,
    )

    items = body["suggested_tags"] + body["suggested_collections"]
    assert items, body
    for item in items:
        assert isinstance(item["name"], str) and item["name"].strip(), item
        assert 0.0 <= item["confidence"] <= 1.0, item
    assert body["auto_applied_tags"] == [] and body["auto_applied_collections"] == [], (
        "auto-labelling was meant to be off for this run, so nothing may have been applied"
    )


@pytest.fixture
def file_scoped_conversation(
    transcribed_file, llm_config: str, api_session: requests.Session, backend_url: str
):
    """Create a conversation pinned to the real model + the file this run made; delete after."""
    file_uuid, _segments = transcribed_file
    conv_resp = api_session.post(
        f"{backend_url}/api/chat/conversations",
        json={
            "llm_config_uuid": llm_config,
            "scope": {
                "file_uuids": [file_uuid],
                "collection_uuids": [],
                "tag_names": [],
                "speakers": [],
            },
            "settings": {"use_context": True},
        },
        timeout=30,
    )
    assert conv_resp.ok, f"could not create conversation: {conv_resp.status_code} {conv_resp.text}"
    conversation_uuid = str(conv_resp.json()["uuid"])

    yield conversation_uuid

    try:
        api_session.delete(f"{backend_url}/api/chat/conversations/{conversation_uuid}", timeout=15)
    except requests.RequestException:
        pass


def test_real_llm_answers_a_question_about_the_file(
    file_scoped_conversation: str,
    gallery_page: Page,
    base_url: str,
):
    """A real local model, asked a real question, scoped to the file this run made."""
    conversation_uuid = file_scoped_conversation

    page = gallery_page
    page.goto(f"{base_url}/chat/{conversation_uuid}")
    composer = page.locator('[data-testid="chat-composer-input"]')
    expect(composer).to_be_visible(timeout=30_000)
    page.wait_for_load_state("networkidle")

    composer.fill("What is discussed in this recording? Answer in one sentence.")
    page.locator('[data-testid="chat-send"]').click()

    completed = page.locator('[data-testid="chat-message-assistant"][data-status="complete"]')
    expect(completed.last).to_be_visible(timeout=CHAT_STREAM_TIMEOUT_MS)
    assert conversation_uuid in page.url, (
        f"turn landed on {page.url}, not conversation {conversation_uuid} — the "
        "route had not finished loading before the message was sent"
    )

    answer_text = completed.last.inner_text().strip()
    assert answer_text, "the real LLM produced an empty answer"
    assert len(answer_text) > 10, f"suspiciously short real answer: {answer_text!r}"
