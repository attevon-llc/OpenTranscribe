# chat — redaction and masking (input side, output side)

Moved intact from `backend/app/services/chat/CLAUDE.md`. Read before touching `redactor*.py`, `masking.py`, `output_redactor.py` or `evidence.py`.

## ⚠️ The chunk index stores transcript text UNREDACTED

That is correct for search — you should find your own words in your own
recordings — but it means `retrieve_chunks()` hands back raw text. **Every path
that sends chunk content to an LLM must go through `redactor.mask_chunks()`
first.** The gate *condition* is identical to summarization's
(`tasks/summarization.py`): apply when `cfg.enabled and cfg.redact_before_llm`,
with the admin force floor already folded in by `resolve_effective_config`.

**The subject is STRICTEST-WINS, resolved PER FILE (task #40) — superseding both
earlier answers below.** The plan said the *file owner's* config
(`redaction/llm_guard.py` reads `media_file.user_id`, matching summarization);
issue #402 shipped the *requesting user's*, because one turn retrieves across a
library of shared recordings with no single owner. **Both are wrong in one
direction**: requester-subject (#402, what shipped) lets a sharee whose own
policy is permissive read PII the owner meant to hide; owner-subject (the plan)
ignores a stricter requester-side mandate. `redaction/config.py`'s
`union_effective_config(requester_cfg, owner_cfg)` masks if EITHER side's policy
says to, with the union of what each masks — enabled_categories/detectors/
pii_entities/custom_words unioned, `allowlist` **intersected** (an allowlist
exempts, so unioning it would be the opposite of strictest-wins), style/
toxicity_threshold take the more protective value, and the admin force floor
(`locked_categories`, `redact_before_llm_locked`) is OR'd so neither side can
shed it. `chat/redactor_chunk_plans._effective_cfg_for_owner` resolves this **per chunk /
per digest section**, not once for the whole turn — a single global union
across a many-file scope would over-mask every file to the strictest owner in
the set, which per-file resolution avoids
(`tests/unit/test_chat_redactor_strictest_wins.py::test_multi_file_scope_resolves_strictest_wins_per_file_not_globally`
is the test that would catch a regression back to a global union). Unresolvable
owner (missing scan row, missing `user_id`, or their own policy read raising)
unions in `redaction/config.py`'s `most_restrictive_config()` — fails closed,
never silently falls through to the requester's policy alone, which would look
identical to "the owner permits it". Anything layering summaries onto chat
retrieval (#383) inherits this per-file union rather than picking a subject of
its own.

**This composes with, and does not replace, the local-provider exemption below.**
`unmask_for_local` is decided from the REQUESTER's config alone, before any
per-file owner lookup runs at all (`chat/redactor._gather`) — the provider rule
answers "does this leave the deployment", which strictest-wins does not change:
if nothing egresses, there is no "either policy" question left to ask, for any
file's owner. It is also safe to read off the requester alone mechanically:
`redact_before_llm_locked` is derived purely from the deployment-wide admin
floor, identical for every user by construction, so re-deriving it from an
owner would answer the same question at the cost of a lookup that must not
happen at all for a fully-exempted turn — see
`test_local_provider_skips_masking_when_the_policy_would_otherwise_apply`,
which asserts zero `db.query()` calls for exactly that case.

Chat's digest/summary tiers (`mask_digests`, `mapreduce.build_file_summaries`)
take the SAME per-file strictest-wins union as `mask_chunks` — unlike a turn's
chunk excerpts, which pool candidates from every file in scope with no single
owner, a digest section is always drawn from exactly **one** file, so "whose
content is this" was always answerable; task #40 is what actually wired that
answer in, closing the gap this section used to flag as "recorded, not built".
The **reader's own** policy still governs what the model *writes* about that
material, unconditionally, via `OutputRedactor` — egress subject and display
subject are never the same question. Whether `OutputRedactor` should also move
to a per-file strictest-wins union is an open question, deliberately not
decided here: it masks the model's own generated prose after the fact, which
has no single "file" to resolve an owner from in the same sense an excerpt
does, and changing it needs its own deliberate decision, not inheritance from
this one.

Masking **fails closed**. If the policy cannot be resolved, or a chunk cannot be
masked, the chunk's content becomes `""` and contributes nothing — never the raw
text. Tests in `tests/unit/test_chat_redactor.py` pin this; do not "fix" them by
falling back to the original content.

### Masking is conditional on WHERE the model runs — BUILT

Owner decision, 2026-08-13, landed. A **local** model — a `vllm`, `ollama`, or `custom`
OpenAI-compatible config whose `base_url` resolves to loopback / RFC1918 /
link-local / IPv6 ULA / a bare docker-compose hostname — receives excerpt text unmasked:
the text never leaves the machine, so masking costs recall and buys nothing. A **remote or
cloud provider** still receives masked text, because sending unredacted PII to a third party
is a data-egress event in a way a local inference call is not. **`provider` name alone is
NOT sufficient for `vllm`/`ollama`**: both are genuinely self-hostable, so their `base_url` is
free-form operator input and gets the identical loopback/RFC1918/link-local/dotless-hostname
check a `custom` endpoint gets — a `vllm` config pointed at a hosted vLLM SaaS is remote.
`redaction.llm_guard.is_local_provider(llm.config)` is the ONE place this classification
happens, and it **fails closed** — any ambiguity (an unresolvable hostname, a mixed
public/private DNS answer, a provider it does not recognise, a `vllm`/`ollama` config with no
`base_url` at all) reads as remote, so masking still applies.

`chat/context_prep._mask_evidence` (called from `_prepare_context`) resolves `is_local_provider(llm.config)` exactly once per
turn (pure — reads only the LLM config, no I/O) and threads it as `unmask_for_local` to all
**three** masking call sites: the chunk leg (`mask_chunks`) and both digest calls
(`mask_digests`, for the retrieved digests and for the scope-wide summary map). `_gather` in
`redactor.py` is where the exemption actually applies: it only skips masking when the policy
would otherwise mask (`cfg.enabled and cfg.redact_before_llm`) **and** the admin has not
forced it (`cfg.redact_before_llm_locked` is False). **The admin force floor always wins the
local exemption** — an operator who ticks "mandate masked text before every LLM provider"
gets that mandate for their own vLLM too, not just external ones. That is why
`EffectiveRedactionConfig` carries `redact_before_llm_locked` alongside `redact_before_llm`:
the exemption needs to know not just THAT masking would apply, but whether an admin decision
specifically overrides skipping it.

The admin string (`redaction.force_redact_before_llm`, both the audit description in
`api/endpoints/redaction_settings.py` and the two i18n strings in
`frontend/src/lib/i18n/locales/*.json`) used to describe itself as **external**-only. That
was accurate before this landed and became a trap after: an admin who read "external
providers" and left the floor off, believing their own vLLM was covered by the exemption
alone, would be right — but the string no longer explained that ticking the floor closes
that exemption too. The i18n text (`settings.redactionPolicy.forceRedactBeforeLlm{,Help}`)
now says the floor covers every provider **including** self-hosted ones; the Python-side
audit description in `api/endpoints/redaction_settings.py` still needs the matching edit —
it lives outside this package.

### Output redaction: masking what the model WRITES (`output_redactor.py`)

Offset-based redaction masks known spans in *stored* text. It cannot catch a model that reads
"John Smith, SSN 123-45-6789" and writes *"the number he gave was 123-45-6789"* in its own words:
there is no span to mask, at offsets that exist in no stored record, so **every cached-span masker
in this codebase renders it clean**. Measured on HEAD before the fix, through the real
`stream_reply`: the client received `'The number he gave was 123-45-6789. That is all.'`

**`redactor.py` is the egress control; `output_redactor.py` is the display control.** They are not
alternatives and neither substitutes for the other.

- **Sentence-buffered.** Deltas accumulate; only text up to the last completed sentence boundary is
  emitted, and the detector runs over that span first. The alternatives were rejected: masking at
  persist-and-reload is cheapest and wrong (the user already read it), and refusing to stream is
  honest and a large UX regression. A newline counts as a boundary (markdown lists carry no
  periods), honorifics and initials do not (splitting `Mr. Smith` hands the detector `Smith`
  alone), and an over-long boundary-free buffer force-flushes while holding a tail so an entity
  straddling the cut is not halved.
- **The gate is `cfg.enabled and cfg.enabled_categories` — NOT `redact_before_llm`.** Same
  `resolve_effective_config` call, same subject, same admin floor; a different field, deliberately.
  `redact_before_llm` is an *egress* control. A user with redaction on and `redact_before_llm` off
  has a masked transcript view and would still get an unmasked SSN rendered into the chat answer —
  precisely the hole this closes. The narrower gate is the plausible-looking wrong choice and has
  been proposed once already.
- **Fail closed, per sentence.** `detect_segment_spans` *swallows* a PII-detector failure and
  returns the spans it did get, so "found nothing" and "could not look" are the same value — the
  `failures` sink (issue #324) is the only thing that separates them. A failure of a detector
  feeding an **enabled category** replaces the sentence with `REDACTION_LLM_FAILSAFE_TEXT`; a
  failure outside the user's categories does not, or every CPU-only deployment without Presidio
  would lose its answers to a category it never asked for. An unresolvable *policy* activates
  masking with every category on, rather than passing text through.
- **The persisted answer is the masked one.** Storing the raw generation would create an unmasked
  PII store in `chat_message.content` that no read path masks, and reload would then show more than
  the stream did.
- **Reasoning gets its own buffer.** The thinking block is collapsed, not hidden — it is a display
  surface.
- **The watchdog measures the PROVIDER's first token, not the first emission.** Keying it off
  emission would read a buffered first sentence as a stalled model and kill it as a timeout.
- **Redaction off is a pure pass-through** — no buffering, no detector, byte-identical stream.

Measured cost (gemma-4-e4b on the local vLLM, 400 deltas / 1,677 chars / 23.9 tok/s, real
Presidio): **+549 ms to first visible token**, 12.7 ms of detector time per sentence, +318 ms
over the whole 16.7 s answer. The one-off cost that used to hurt was the **~10 s Presidio cold
load** on the first masked answer in a fresh API process. That is **done** (issue #74):
`redaction/warmup.py` builds the analyzer on a daemon thread at startup when the deployment
actually redacts, taking the first `_mask_inline` from **9.927 s to 0.016 s**. It is a warm-up,
not a dependency — an absent Presidio still fails closed exactly as before. Read that package's
CLAUDE.md before touching it: the warm-up is only safe because `_get_analyzer` now holds a build
lock, without which a request arriving mid-warm-up built a *second* engine.

Pinned by `tests/unit/test_chat_output_redaction.py`, whose `models`-marked test is the only one
that proves the *detector* — not the plumbing — catches a paraphrase.

### ⚠️ There are TWO maskers on the INPUT side and they are not interchangeable

(Three in the package overall — `OutputRedactor` above is the third, and it addresses text that
has no stored form at all. These two are the ones that can be confused for each other.)

| Function | For | Addresses text by | Fails closed |
|---|---|---|---|
| `mask_chunks()` | transcript chunks | **time range** — every segment overlapping the hit | per chunk, whole |
| `mask_digests()` | `doc_type: digest` documents | **provenance** — each sentence's own `segment_ids` | per **sentence** |
| `OutputRedactor` | model-generated text | **detection at emit time** — no stored spans exist | per **sentence** |

A digest sent through `mask_chunks()` **over-discloses**. A chunk *is* contiguous turns, so
rebuilding it from its time range reproduces it. A digest is a handful of non-contiguous *selected*
sentences spanning an entire recording, so the same rebuild returns the **whole recording
verbatim** — strictly more text than the digest held, from a function whose name asserts it masked
it. Nothing about a call to `mask_*` invites a second look in review, and under the current
redaction policy this is the path that egresses to a third-party provider.

`tests/unit/test_chat_digest_masking.py::test_the_chunk_path_over_discloses_a_digest` is a
**must-fire** guard: it asserts the wrong path still produces *more* text than the digest. If
someone folds `mask_digests` into `mask_chunks`, that test goes red while its twin goes green.
