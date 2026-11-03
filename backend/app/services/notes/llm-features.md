# services — LLM features

Moved intact from `backend/app/services/CLAUDE.md`. Read before touching `llm_service.py`, `llm_reasoning.py`, `llm_context_window.py` or any summarization/topic/speaker-ID prompt path.

## LLM features (optional)

`llm_service.py` is a **synchronous** client on purpose (Celery tasks; no asyncio conflicts).
`LLMProvider` = openai · vllm · ollama · anthropic · openrouter · bedrock · custom (`claude` is a
deprecated alias for `anthropic`).

- **Resolution order**: `create_from_user_settings(user_id)` → falls back to
  `create_from_system_settings()` (env `LLM_PROVIDER` + provider keys/endpoints) **only when the
  user has NO active config**; an active-but-unusable one (bad pointer, dangling id, unknown
  provider, undecryptable key, Bedrock without region, vllm/custom without `base_url`) raises
  `LLMConfigurationError` and must never reach the system provider (masking was decided for the
  user's provider). `create_from_settings` returns `None`, `describe_for_user` returns `None`. Empty
  `LLM_PROVIDER` and no user config = transcription-only; `create_from_system_settings`
  returns `None` and callers must handle it. `custom` is **user-config only** — it always
  returns `None` from system settings.
- **"Is an LLM configured?" has ONE answer: `LLMService.is_configured_for_user(db, user_id)`**
  (a thin wrapper over `describe_for_user`, which also returns `(provider, model, base_url)` for
  the pipeline view's AI-features step).
  DB-only, no network, and built from the same helpers the factories use
  (`_resolve_user_llm_settings`, `_resolve_system_provider`, `_endpoint_resolves`), so it is
  true exactly when `create_from_settings` would return a service. Never re-derive it as
  `bool(settings.LLM_PROVIDER)`: a provider with no key or endpoint passes that check while
  every task skips, which made the post-transcription sweep re-dispatch topic extraction for
  every file every 10 minutes (issue #1017).
- **Automatic LLM steps have per-user opt-outs (#1148).** `auto_llm_speaker_identification` and
  `auto_topic_extraction` (default ON; `UserSetting` rows, system tier `auto_llm.speaker_identification`
  / `auto_llm.topic_extraction`) are resolved ONLY by `services/auto_llm_settings.py`
  (user > system > True). The AUTOMATIC dispatch sites consult it: `_dispatch_llm_speaker_identification`
  in `speaker_attribute_task.py` (every chain exit, `dispatch_speaker_attribute_pipeline`, and so
  postprocess/background/rediarize), `trigger_automatic_summarization` in `tasks/transcription/downstream.py`
  (only when `tasks_to_run is None`), `ai_insights_companion.queue_topic_extraction_with_summary`
  (a manual Generate/Regenerate summary also queues topic extraction for a file that never had a
  `TopicSuggestion`; Redis-guarded against double clicks, fails closed), and `TaskDetectionService.identify_incomplete_post_transcription_files`
  (the recovery sweep: a step the user turned off must not be flagged missing, or it is re-queued and
  re-detected every 10 minutes). Manual triggers and an explicit stage list (`speaker_llm` /
  `topic_extraction` via `dispatch_task_by_name` or `llm_requested=True`) never consult it. A new
  automatic dispatch of either step must go through the resolver.
- **`bedrock` is the one SDK-based provider** (`SDK_PROVIDERS`, `llm_bedrock.py`): boto3's
  Converse API, not an HTTP endpoint, no `base_url`. A configuration carries its OWN region and
  optional AWS credentials (access key id, secret, session token), stored as ONE encrypted JSON
  blob in the row's `api_key` column (no migration; `models.UserLLMSettings.has_api_key` is
  False for Bedrock and `aws_region` / `has_aws_credentials` are the only things a response
  sees). `services/bedrock_settings.py` is the ONLY place that decides a Bedrock region and
  builds boto3 client kwargs (`resolve_region`, `client_kwargs`, `merge_stored`, `scrub`,
  `describe_failure`); `LLMService.credentials_from_row` is the one decryption point and puts the
  result on `LLMConfig.bedrock` (`BedrockConnection`, secrets excluded from repr). Blank
  credentials mean the server's boto3 chain and `settings.BEDROCK_REGION` (which `Settings`
  already folded from `AWS_REGION` / `AWS_DEFAULT_REGION`); the environment-selected system
  fallback (`_get_provider_config`) uses `env_region()` and `resolve_default_model()`. Nothing
  in `bedrock_settings` touches the DB, so a Celery task resolves a config once, closes its
  session and calls Bedrock with none open. `tests/unit/test_bedrock_connection.py` pins it
  structurally: no `boto3.client("bedrock...")` outside `llm_bedrock._client`, no
  `settings.BEDROCK_*` read outside the resolver, no credential kwarg elsewhere. The credentials
  are write-only on every path: `GET /config/{uuid}/api-key` and the model-discovery key lookup
  both refuse a Bedrock row, error text is `scrub`bed, a connection test answers
  `FAILURE_MESSAGES` sentences only, and `core/validation_errors.py` drops `input` from a 422
  that concerns a sensitive field. `GET /llm-settings/bedrock/server-credentials` (admin) reports
  whether the server's own chain resolves, by source KIND. Reasoning-probe support is
  deliberately **not** wired (`llm_reasoning.PROBEABLE_PROVIDERS` excludes it) — see that section
  below for why a provider needs a live measurement first, not just an enum entry.
- Per-user keys are AES-encrypted (`utils/encryption.encrypt_api_key`) and never returned;
  edit mode reuses the stored key when the request omits `api_key`.
- Summarization: BLUF, speaker analysis with talk time, action items, decisions, follow-ups,
  multi-section stitching for long transcripts (`_chunk_transcript_intelligently` →
  `_summarize_section` → `_combine_sections`). Output languages: `core/languages.py:
  LLM_OUTPUT_LANGUAGES` (12: en es fr de it pt **nl** ru zh ja ko ar — no Hindi).
- **Never log prompt or model-output text at INFO or above (issue #1022).** Output is derived
  from the transcript, so it would copy transcript content into log aggregation. On a parse
  failure log `utils/llm_log_safety.describe_llm_text(text)` (`len=N sha256=<12 hex>`) and, if
  an excerpt helps debugging, `log_llm_text_excerpt` (DEBUG only). Watch for indirect leaks:
  a pydantic `ValidationError`'s message embeds its `input_value`, so log `type(e).__name__`.
  `tests/unit/test_llm_parse_failure_log_hygiene.py` drives each parse-failure path.
- **Speaker suggestions are never auto-applied.** `identify_speakers` returns confidence-scored
  predictions stored for manual verification (`tasks/speaker_identification_task.py`). Only
  tags/collections have an auto-apply path (`auto_label_service.auto_apply_suggestions`).

### Reasoning is a per-MODEL capability, and it is MEASURED (`llm_reasoning.py`, issue #64)

**A provider returning HTTP 200 for a "do not reason" parameter is not evidence the model
honoured it.** Measured against the live vLLM (`localhost:5195`) serving **gemma-4-e4b**, at
**temperature 0, max_tokens 1200**, summing **both** response spellings:

| arm | reasoning | content | tokens |
|---|---|---|---|
| `chat_template_kwargs={"enable_thinking": true}` | 1656 | 843 | 1123 |
| `chat_template_kwargs={"enable_thinking": false}` | **931** | 378 | 562 |
| kwarg omitted (the control) | **931** | 378 | 562 |

Three facts, all load-bearing:

1. **`false` is byte-identical to omitting the kwarg.** The chat template ignores the off value.
   vLLM answers **200** and silently does nothing — same shape as #437 and the
   `_neural_pipeline_verified` bool.
2. **Reasoning is produced in ALL THREE arms.** gemma-4-e4b has no off-switch at all.
3. **`true` does change behaviour**, which is why `_prepare_openai_payload` still sends
   `enable_thinking: true` unconditionally for vLLM.

The numbers are true of *that model on that server*. A different model is precisely the case
this design exists to serve, so nothing keys off the provider alone.

**⚠️ THE METHOD TRAP: sum BOTH `reasoning` AND `reasoning_content`.** vLLM 0.19 reports
`reasoning`; the parser convention and several gateways report `reasoning_content`. An earlier
probe read only one, measured ~0 on every arm, and reported the behaviour as *non-deterministic*
— it is perfectly deterministic. `llm_reasoning.reasoning_chars()` is the one reader; use it.

**⚠️ The probe is NON-streaming, deliberately.** vLLM's *streaming* reasoning parser only enters
reasoning mode on the template's opening token (that is #439), so a streaming probe measures the
**parser** and would report a working off-switch for every model. The non-streaming parser
separates the block in all three arms, which is what makes the comparison about *generation*.
`LLMService.chat_completion_raw` exists for this: `chat_completion` raises on empty content, and
the activated arm legitimately returns `content: null` with 1,656 characters of reasoning.

#### The pieces

| What | Where |
|---|---|
| Probe, verdict rule, storage helpers | `services/llm_reasoning.py` |
| Verdict enum (3 layers read it) | `core/enums.ReasoningOffSwitch` |
| Instrument settings (prompt, tolerance, floor, timeouts) | `core/llm_defaults.py`, `LLM_REASONING_*` |
| Recorded verdict | `SystemSettings`, key `llm.reasoning.off_switch.<fp>` |
| Run it | `POST /llm-settings/config/{uuid}/reasoning-probe` |
| Read it | `GET /llm-settings/config/{uuid}/reasoning`, and `reasoning_off_switch` on the configurations list |
| Applied to a turn | `endpoints/chat/messages.py` → `llm_reasoning.resolve_enable_thinking` |
| UI condition | `ChatControlsPanel.svelte`, renders only on `'works'` |

#### Adding a model (or a provider) — the repeatable procedure

1. Configure it as an LLM configuration and press the probe (or `POST .../reasoning-probe`).
   It runs three real generations at temperature 0 against **that** endpoint: `true`, then
   `false`, then the parameter omitted.
2. Read the verdict. `works` → the toggle appears. `absent` → probed, and the model ignored it.
   `no_reasoning` → nothing to switch off. `unsupported` → this build has no off-switch
   parameter for that provider. `unknown` → never probed, or the probe could not complete.
   Every value except `works` renders no control and leaves the request as it is today.
3. **A new PROVIDER needs two lines and a measurement**: an entry in
   `llm_reasoning.PROBEABLE_PROVIDERS`, and the parameter in the matching
   `LLMService._prepare_*_payload`. Ollama's native `think` and Anthropic's `thinking.type` are
   real mechanisms and are deliberately **not** wired up: adding one without measuring it is
   exactly the mistake this module exists to prevent. `custom` must never be added — those
   OpenAI-clones **400 on unknown payload keys** (the same reason they are excluded from
   `llm_stream.USAGE_OPTION_PROVIDERS`).

**The tolerance, and why.** `off` must be ≤ **10%** of the omitted control *and* ≤ 10% of the
activated arm; below `LLM_REASONING_PROBE_MIN_CHARS` (32) in every arm it is `no_reasoning`
instead. 90% suppression is the weakest threshold under which the word "off" is honest — a
switch that halves the thinking still leaves the user reading a false claim. Both conditions are
required: the first catches the measured failure (`off == omitted`), the second stops a
coincidentally-quiet control run from scoring a switch. The measured negative sits at 1.00 of
the control and 0.56 of the activated arm, i.e. **5.6× clear** of the boundary on the tighter
condition.

#### Where the verdict is recorded, and why there

`SystemSettings`, keyed by a **fingerprint of (provider, base_url, model)** — not a column on
`user_llm_settings`. The capability belongs to the software answering at that URL, not to a
config row: two users pointing at the same vLLM share one measurement instead of paying three
generations each, and editing a config's model produces a key that was never written, so the old
verdict is simply *unfindable* rather than stale. No migration, and no invalidation logic. The
URL is hashed rather than stored so the deployment-wide table does not become a directory of
everyone's private endpoints.

It is a **measurement, not a setting** — no coded default is editable into it, no admin panel
writes it, and the row's `description` says so, because an operator hand-editing it would be
asserting a capability nobody measured.

**When it runs: explicit user action only.** Three real generations is far too much in front of
a user's first chat turn, and a background sweep would dial every configured third-party
provider unprompted — an egress event, and a bill, nobody asked for. It sits beside "Test
connection", where the cost is visible and chosen.

#### ⚠️ #439's fix must stay, and a model with no off-switch must behave exactly as today

`_prepare_openai_payload` has three arms and the **default is unchanged**: absent or `True` →
`{"enable_thinking": True}` (the #439 fix), `False` → `{"enable_thinking": False}` (the measured
off arm), `None` → the key omitted (the probe's control arm, nothing on the chat path passes it).
Do not "clean up" that unconditional `true`: without it vLLM's streaming parser never engages and
the whole chain-of-thought is rendered as the answer, closed by a literal `<channel|>` token.

`resolve_enable_thinking` returns `None` — i.e. *change nothing* — unless the verdict is `works`.
So a stored "reasoning off" preference on a model whose off-switch was never measured is
deliberately **not applied**; it applies again if the conversation is later pointed at a model
that can honour it. `tests/unit/test_llm_reasoning_capability.py` pins that non-regression, with
a `works` control beside it so "always return None" cannot pass.

### The context window is MEASURED too (`llm_context_window.py`, issues #533 / #833)

`LLMConfig.max_tokens` **is** the context window (`LLMService.__init__`), and it used to be
whatever the user *declared* in `UserLLMSettings.max_tokens` — never checked against what the
endpoint actually serves. Issue #533 built the probe (a real, opt-in measurement, stored the
same way as the reasoning verdict above); issue #833 is what actually **applies** it on the
chat/summarization runtime path. Before #833, a declared value above the real ceiling produced
either a hard 400 (vLLM/OpenAI-compatible) or silent front-of-context truncation (Ollama sends
the declared value as `num_ctx`, which can drop the system prompt first) — and issue #873
raising the declared Ollama default to 128000 made an over-declared config the common case, not
the rare one.

| What | Where |
|---|---|
| Probe, storage helpers, the narrowing rule | `services/llm_context_window.py` |
| Status enum | `core/enums.ContextWindowStatus` |
| Recorded measurement | `SystemSettings`, key `llm.context_window.<fp>` |
| Run it | `POST /llm-settings/config/{uuid}/context-window-probe` |
| Read it | `GET /llm-settings/config/{uuid}/context-window` |
| Applied to a turn | `LLMService.create_from_*` → `llm_context_window.effective_window` |

**It only ever narrows.** `effective_window` mirrors `resolve_enable_thinking`'s "return
unchanged unless the verdict is definitively known" rule: no measurement record (the probe is
opt-in, so this is the common case), a non-`MEASURED` status, or a measured value >= the
declared one all return the declared value unchanged. Only a measured value *below* the declared
one is applied, with a warning log naming both numbers — a declared value below the measured
ceiling may be a deliberate VRAM/cost/shared-server decision, and raising it automatically would
override that decision and reintroduce the failure this exists to prevent.

**`LLMConfig.max_tokens` on a built service is therefore the EFFECTIVE window, while the
`user_llm_settings` row keeps the DECLARED one** — the same split issue #64 draws between a
stored preference and what the runtime actually honours. `LLMService.__init__` copies
`config.max_tokens` into `self.user_context_window`, so every downstream budget computation
(response token sizing, chunking, the Ollama `num_ctx` payload, `resolve_answer_tokens` /
`build_messages` in `chat/prompting.py`) is narrowed automatically without a second call site.
`chat/service.py` records the effective value actually used on `turn.metadata["context_window"]`
after `build_messages`, so a truncated turn is attributable rather than reading as an unexplained
retrieval failure.

No pre-flight token-count validator was added on top of this: once the budget is computed
against the real (possibly narrowed) window, prompt + answer fit by construction, and a second
token-counting check would duplicate logic `build_messages` already owns.
