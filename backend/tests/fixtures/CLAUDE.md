# backend/tests/fixtures — chat suites, mock LLM, mock cloud ASR, document export fixtures

Moved intact from `backend/tests/CLAUDE.md`.

## Chat suites (issue #52)

| File | Needs |
|---|---|
| `unit/test_llm_streaming.py`, `unit/test_chat_{prompting,retrieval,citations,redactor,hooks,limits}.py` | nothing — mocks only |
| `unit/test_v374_migration_consistency.py`, `test_chat_{context_resolver,endpoints,user_settings,gdpr_erasure}.py` | Postgres |
| `e2e/test_chat.py` (marker `chat`) | full stack + an LLM provider + a completed transcript |
| `e2e/test_chat_grounding.py` (marker `chat`) | full stack + `--with-mock-llm` + a completed transcript |

`test_chat_grounding.py` covers the #384 invariant end to end: **the citations a
user can click are exactly the excerpts the model was given.** It registers its
own per-test LLM config rather than reusing the shared provider, because the
whole point is to control `max_tokens` — the user-declared context window the
excerpt budget is computed against. A 512-token window (the API minimum) makes
`budget_chars` floor at 0 against the ~2 KB base rules, so *no* excerpt fits and
the `context_dropped` warning fires deterministically. `ROOMY_CONTEXT_WINDOW` is
the control: same code path, opposite outcome, driven only by config — without it
the suite would still pass if the notice were rendered unconditionally.

**Two traps that read-order gets wrong** (both cost a debugging cycle):

- **Retrieval diagnostics are not on screen mid-stream.** A streaming message
  holds only what the SSE frames supplied; `msg_metadata.retrieved` /
  `chunks_used` live on the persisted row and arrive when the thread is fetched.
  Read the Details panel only after `_reload_thread()`, or it has no counts at all.
- **Persisted citations ≠ offered citations.** `_persist_reply` stores
  `used_citations` — the ones the answer actually referenced — so after a reload
  the source cards are a *subset* of what the `sources` frame offered. Count
  offered cards DURING the stream; compare against `chunks_used` read after.

`test_chat_endpoints.py` patches `app.db.session_utils.session_scope` to the test session.
The streaming service deliberately opens its **own** session (it outlives the request's
dependency scope), which under the savepoint harness cannot see uncommitted rows — without
the patch, persistence fails on a foreign key that is perfectly valid in production.

## Mock LLM provider (no GPU, no API key)

`scripts/mock-llm-server.py` is a real OpenAI-compatible server that cans only
token generation. Everything else stays real — retrieval, redaction masking,
citation assembly, SSE parsing, usage recording — which is why it is preferred
over monkeypatching `LLMService`: a patched client proves the mock behaves, not
that the app does.

```bash
./opentr.sh start dev --with-mock-llm     # http://mock-llm:5199/v1 in-network
```

Fixtures live in `tests/fixtures/mock_llm.py` (registered via `pytest_plugins`
in the root conftest, so no per-file import):

| Fixture | Use |
|---|---|
| `mock_llm_url` | URL the TEST process can reach. Reuses the container, else starts a subprocess — never skips |
| `mock_llm_base_url_for_backend` | URL the BACKEND CONTAINER can reach. **Skips** without the container: a host subprocess is invisible to it |
| `mock_llm_completion` | Call the mock directly; returns `{status_code, body}` |
| `register_mock_llm_provider` | Configure the app's `custom` provider at the mock, deleted on teardown |

Scenario models select behaviour by `model` name, so a test picks a failure mode
by *configuring a provider* and the app runs its REAL error handling:

| Model | Behaviour |
|---|---|
| `mock-gpt` | normal reply with `[1]`/`[2]` citations, markdown, code block |
| `mock-echo` | echoes the prompt received — assert what the app **sent** (masking applied? prompt layers ordered?) |
| `mock-empty` | completes with no content |
| `mock-error` | HTTP 500 before any token → `provider_error` frame |
| `mock-slow` | stalls past the first-token watchdog |
| `mock-reasoning` | a "thinking" phase before the same `[1]`/`[2]` answer as `mock-gpt`. ⚠️ **Separates it only when the request activated thinking** (`chat_template_kwargs={"enable_thinking": true}`); unasked it reproduces #439 — the thoughts arrive on `delta.content` with a bare `<channel|>` closer, exactly as vLLM does — so both branches are testable. The field is `delta.reasoning` on vLLM 0.19, not `reasoning_content`; our parser reads both |

**CI needs no setup**: the subprocess fallback means `tests/unit/test_mock_llm_fixture.py`
runs in the GitHub `backend-tests` job with no compose stack. Tests that need the
*backend* to reach the mock must use `mock_llm_base_url_for_backend` and will skip
in CI rather than fail.

**Each new migration breaks the previous suite's detection assertion.** `_detect_schema_version`
returns the newest matching revision, so when you add vNNN, widen the vNNN-1 test to accept
either value and pin the exact one in your own suite.

## Mock cloud ASR provider (no GPU, no vendor account)

`scripts/mock-asr-server.py` is a stdlib Gladia API v2 stand-in
(`GET /v2/live`, `POST /v2/upload`, `POST /v2/transcription`,
`GET /v2/transcription/<id>`). Only the returned transcript is canned — upload
validates real RIFF/WAVE structure, and the submitted diarization/language/
vocabulary fields are recorded verbatim and inspectable via
`GET /_mock/last-request`, the same "prove the app's real request shape"
role `mock_llm_completion` plays for the LLM mock.

```bash
./opentr.sh start dev --with-mock-asr     # http://mock-asr:5198 in-network
```

✅ `GladiaProvider._resolve_base_url` (`backend/app/services/asr/gladia_provider.py`)
now honours a per-config `base_url` — fixed by issue #594, which also added an SSRF
guard (`ASR_ALLOW_PRIVATE_ENDPOINTS`, mirroring `LLM_ALLOW_PRIVATE_ENDPOINTS`) that
refuses a private-network base URL, env-var- or config-supplied, unless allowed.
`GLADIA_API_BASE_URL` is still read as a fallback for flows that never save a
config; `docker-compose.mock-asr.yml` sets it (plus `ASR_ALLOW_PRIVATE_ENDPOINTS=true`,
since `mock-asr` is a private compose hostname) on `backend` and
`celery-cloud-asr-worker` — without both, `--with-mock-asr` doesn't route ASR
traffic anywhere.

Fixtures live in `tests/fixtures/mock_asr.py`:

| Fixture | Use |
|---|---|
| `mock_asr_url` | URL the TEST process can reach (`http://127.0.0.1:5198`) |
| `mock_asr_base_url_for_backend` | URL the BACKEND CONTAINER can reach (`http://mock-asr:5198`) |
| `register_mock_gladia_asr_config` | Configure the app's `gladia` provider at the mock, deleted on teardown |

Scenario selected via `?scenario=` on the transcription POST, **and** via the
`MOCK_ASR_SCENARIO` env var as the server-start default (there is no per-job
selection hook on the client side — see the gap note below):

| Scenario | Behaviour |
|---|---|
| `ok` (default) | canned transcript from `sample_transcript.json`, reshaped with a distinctive `"Zylofenix"` token appended |
| `error` | transcription job ends in a Gladia-shaped error payload |
| `malformed` | response body doesn't match the expected schema |
| `upload-reject` | `POST /v2/upload` itself rejects the file |

**Known, documented gap**: `backend/tests/integration/test_lite_mode_mocked_providers.py`
covers only the `ok` happy path end to end through the real pipeline. The 3
negative-scenario tests (`error`/`malformed`/`upload-reject`) were **not**
implemented there — the mock selects scenario via `?scenario=` on the request
the *app* sends, and `GladiaProvider` has no per-job scenario-selection hook to
drive that; only the server-start `MOCK_ASR_SCENARIO` default exists, which
can't be changed mid-pytest-module without breaking that module's own `ok`
happy-path tests (all tests in the module share one mock-asr container,
serialized via `xdist_group`). The mock's own scenario contract IS covered at
the unit level against `GladiaProvider` directly, in
`backend/tests/unit/test_gladia_provider.py`. A bash-driven harness (unlike a
shared pytest module) can restart the container between phases —
`scripts/release-tests/test-lite-mode.sh`'s negative-path phase does exactly
that. Fixing the live-pipeline pytest gap remains follow-on work.

## Document export fixtures (issue #1278)

`fixtures/document_export.py` (a `pytest_plugins` entry) is the harness for the DOCX/PDF export:
`DocumentExportHarness` posts the request, then runs `prepare_transcript_document_task` in process
with the test's own session and recorders for storage, presigning and Redis, so a test reads the
BYTES a user would download (`docx_text`, `pdf_text`, `document_text`), never an internal flag.
`fixtures/redaction_export_world.py` is the shared profane, redactable file and policy toggles
used by both the sync and the document gate suites. `fixtures/fonts/` holds pinned Noto test
fonts (OFL); layout tests use `unit/_monospace_measurer.py` so they never depend on host fonts.
Red-checks of the layout, policy and write-back tests were done by mutation on a `git archive`
tree; the PDF tests need `pypdf` (`requirements-test.txt`).
