# chat — RAG and chat are ENGLISH-ONLY (transcription is not)

Moved intact from `backend/app/services/chat/CLAUDE.md`. Read before touching `language.py` or any retrieval/rerank/summary language handling.

## ⚠️ RAG and chat are ENGLISH-ONLY. Transcription is not.

**Multilingual is the GOAL. This section tracks the distance to it — it is not a
policy of English-only**, and the heading above is kept only because the shape of
the warning still carries that name. WhisperX transcribes 100+ languages and that
must keep working; what varies is how much the *question-answering* path on top can
do with the result, and that depends on **what the deployment has configured**.

⚠️ **`SUPPORTED_RAG_LANGUAGES` used to be a hardcoded `frozenset({"en"})` and that
was a bug.** A deployment that had selected a multilingual embedding model — and was
therefore genuinely serving Spanish — still displayed "Spanish is unsupported" on
every turn. Support is now DERIVED, by `supported_rag_languages(db)`, from the model
that actually produces the vectors. The constant survives only as the fallback, and
`ALL_LANGUAGES` (`None`) is the open set. **Do not widen the constant by hand** —
that would assert support on a deployment still running English-only embeddings.

| Stage | State |
|---|---|
| Embeddings | **The one that matters, and it is configurable.** Default `all-MiniLM-L6-v2` is `"languages": ["en"]`. Measured by `scripts/verify-embedding-models.py`, cosine of a translation against its English original: **0.10 (es) / 0.01 (zh)** for the default vs **0.98 / 0.95** for `paraphrase-multilingual-MiniLM-L12-v2` — also 384d, so adopting it is a re-embed and **not** an index recreation. Selecting it is what makes a deployment multilingual |
| BM25 | Still `english_stop` + `english_snowball` (`services/search/index_schema.py`). **Partly mitigated already**: `content.exact` (standard analyzer) is queried alongside the stemmed leg, so non-English keyword matching degrades rather than vanishing. Dropping the stemmed leg query-side for non-English is #453 step 6 and is measurement-gated |
| Reranking | `ms-marco-MiniLM-L-6-v2` is English MS MARCO and is now **skipped when the candidate head is predominantly non-English** (`reranker._is_mostly_non_english`). Not an optimisation: `rerank` **overwrites** `hit.score`, so letting it run destroys a correct retrieval order using a model that cannot read the text. Distinct from the #363 question of whether it helps on *English* — that stays open, blocked on #463 |
| ~~Prompting~~ | **FIXED (#453).** Both prompts are authored in English but now instruct in it: answer in the question's language, quote in the original and never translate a quotation (a translated quote is a paraphrase wearing quote marks), and never translate the rewritten query. `tests/unit/test_chat_answer_language.py` |

⚠️ **A multilingual embedding model does not make BM25 multilingual.** The warning is
therefore not simply switched off — the point is to stop asserting things that are
not true in *either* direction. `tests/unit/test_chat_multilingual_stack.py` pins
both halves, each with its opposite-outcome control, and both fail **closed to
English**: a warning that wrongly appears is a nuisance; one that wrongly vanishes
hides the silent failure it exists to surface.

Chunking used to be listed here as "the exception — chunk boundaries are fine."
**That was wrong, and only true for Latin and Cyrillic scripts.**
`_PUNKT_LANG_MAP` covers 18 European languages, and everything else was handed to
the ENGLISH punkt model, which returned a whole Chinese transcript as one
sentence — while `str.split()` reported it as one *word*, so no size check fired
either. A 10,500-character Chinese recording became **one chunk**. Fixed in issue
#448; `tests/unit/test_chunking_scripts.py` covers zh/ja/ko/ar/hi/th with Latin
controls. Chunk boundaries are now correct for every script; what remains
English-only is everything that RANKS or READS them.

The failure this produced was **silent**: a non-English recording is not retrieved
for an English question, so the model answered confidently from whatever English
material remained and nothing said a recording had been effectively invisible.

**It warns, it does not refuse — deliberately.** A transcript library is normally
mixed, so refusing every question because one recording is Spanish would be worse
than useless. The turn answers from what it can and emits a `warning` frame
`{"code": "unsupported_language", "languages": [...], "files": N, "unknown_files": N,
"supported": ["en"]}` plus `msg_metadata.unsupported_language` — **the exact
mechanism `context_dropped` already uses**, so there is one render path and the
notice survives a reload. Do not invent a second one.

**Three buckets, because unknown is its own answer.** `MediaFile.language` is
nullable. An undetected language is *not* counted as English (that would hide a
real Spanish recording) and *not* as non-English (that would fire on every library
recorded before language detection existed). It is reported as
`context_languages.unknown_files` on every turn and warns on nothing by itself.

**What is judged, and what deliberately is not.** Languages come from the union of
the *resolved scope* and the files the retrieved excerpts came from. The scope half
is the important one: a scoped Spanish file that retrieval never surfaces would
otherwise never warn, because failing to retrieve it is the whole defect. But
`file_uuids is None` ("all accessible") is **never enumerated** — one foreign
recording anywhere in a library would put the warning on nearly every turn, and a
warning that is always on is one nobody reads.

Language is read from **Postgres, not the chunk document** — the index carries a
`language` keyword field, but for the same reason scope resolution avoids it (see
below). The read reapplies no permission filter because both inputs are already
authorized (`context_resolver` for the scope, `accessible_user_ids` for the hits).
The lookup fails **open**: a diagnostic must never break a chat turn, and a warning
invented from a failed read would be worse than none.

**Not admin-tunable, on purpose.** An operator can select a multilingual embedding
model, but that repairs one of the three remaining stages; a `SystemSettings` row
letting them declare "Spanish is supported" would be dishonest about the other two.
`SUPPORTED_RAG_LANGUAGES` widens in code, alongside the pipeline that earns it —
and per language, only as each clears the MIRACL benchmark (#453 step 9). Since
#453 the settings UI badges each model `Multilingual` / `English only` and says the
switch changes the semantic embeddings **only**, so choosing one is at least no
longer a blind decision.

Known gap: the *question's* language is not detected — asking in Spanish about an
English transcript is not flagged. That needs a language detector and is part of
the multilingual-RAG backlog, not this notice.

`tests/test_chat_language_scope.py` pins all of it, and every "it fires" test is
paired with an all-English **control** asserting silence.

**The client renders it** (`ChatMessage.svelte`, testid `chat-unsupported-language`),
via `stores/chat.ts` folding the frame into `msg_metadata` exactly as
`context_dropped` does. Three things had to move together and a fourth is a trap:

- `ChatWarningCode` in `lib/types/chat.ts` — a code missing from that union is
  **silently discarded**, so the server can emit a warning nobody ever sees.
- `stores/chat.ts`'s `warning` case, now a code→patch map rather than an
  `if` chain, so an unhandled code cannot fall through to nothing.
- `chatStream.ts`'s `known` list keys on the **event name** (`warning`), not the
  code, so it needed no change — but a genuinely new frame type would.
- The notice **names the languages** through `formatLanguageNames`
  (`Intl.DisplayNames`, reader's locale, raw code as fallback). A language
  silently dropped there would understate the warning — the user would be told
  fewer of their recordings were unsupported than actually were.

`chat.message.unsupportedLanguage` exists in all 12 locales (`npm run check:i18n`
enforces exact parity). Guards: `ChatMessage.unsupportedLanguage.test.ts`,
`chat.reducer.test.ts`, and `formatting.test.ts` — the naming assertion lives in
the last of those because vitest loads no locale bundle, so `$t` returns the raw
key and any assertion on the rendered sentence in a component test is vacuous.
