# Spec — summary search, display, and redaction

**Status:** shipped. Postgres FTS over `summary_data`, exposed as `result_type` on
`/api/search` (`services/search/summary_search.py`, frontend legs in
`routes/search/+page.svelte` + `stores/search.ts`). Tracked as #462 (search, open) and #465
(redaction, CLOSED) — **not** #89/#90, which are unrelated MERGED PRs from October 2025;
citing them here would resolve to the wrong issue, the same "citation that resolves and
misleads" defect as #875. Multi-select UI: #760. Uncached-redaction residual: #822.
**Decided:** 2026-08-14, with the owner.
**Supersedes:** the "add `doc_type: summary` to the v6 index" idea, rejected below.

---

## 1. What is true today (verified, not assumed)

| | |
|---|---|
| Summary text | `media_file.summary_data` (Postgres JSONB), **per file** — one column, not per user. 8 readers. Display works. |
| Corpus-wide summary search | **Never existed at the UI layer.** The retired `POST /api/files/search` had no frontend caller; `lib/types/summary.ts` records its types "had no importer outside this file". That is how the old service accumulated 6 uncalled methods unnoticed. |
| The retired index | Had real BM25 (`searchable_content` + `multi_match`, `bluf^3` / `key_decisions^2` / `action_items.text`, fuzziness) but **no embeddings** — `summary_content` was mapped `"enabled": False`. |
| Summary redaction | **None, anywhere.** No masking in the summarization endpoint, the summary schema, or `formatting_service`. |
| `SummarySearch.svelte` | Unrelated — it is *find-in-summary*, a presentational wrapper over `SearchBar` that highlights matches inside one open `SummaryModal`. |

So #67's retirement broke nothing. Summary search is a **new feature**, not a restoration.

## 2. Why summaries are NOT going into the chat retrieval plane

The owner's instinct — summaries are compressed, high-signal, fast to retrieve — is right, and
**that value is already delivered by the digest plane**: embedded, per-file, retrieved in chat as
the `<overview>` block, cited with `kind: "digest"` and *labelled as a summary in the UI*
(`ChatSources.digestKind.test.ts` pins it), driving the router's `summarize` / `temporal` paths.

Adding the LLM summary as a third `doc_type` was considered and rejected for four reasons:

1. **Grounding.** The digest is *verbatim transcript sentences*, so a citation points at words
   someone actually said. An LLM summary is interpretation — citing it cites what a model wrote
   about the meeting. For a product whose value is "here is the evidence", that distinction is
   the product.
2. **Instability.** Summaries vary by prompt and context, so an index entry whose *shape* changes
   on regeneration is unstable to rank against. This also rules out per-section indexing.
3. **D6 — the no-LLM deployment.** `LLM_PROVIDER` empty means no summary exists at all. The
   digest works everywhere. Retrieval built on summaries silently vanishes there.
4. **RRF competition.** Indexing both puts two representations of the same recording into one
   fused ranking — a results page that is all the same meeting.

**Keyword search over summaries is a different job from grounding a chat answer**, and that is the
job this spec builds.

## 3. The design

### Backend

- **Postgres full-text over `media_file.summary_data`.** No OpenSearch change, no reindex, no RRF
  participation. Survives a prompt change because it is text search over whatever the column holds.
- Exposed as a **filter / result-type on the existing search endpoint** — one search surface, not
  a new route. The old separate route is exactly what rotted.
- Consider a GIN index on a `to_tsvector` expression over the JSONB text. **Measure before adding**
  — the corpus is ~2.5k files, and an unused index is a write cost for nothing.

### Result shape

A summary hit returns **the matching section**, not the whole blob:

- The section is **clickable → opens the summary modal at that section**, exactly parallel to a
  transcript hit jumping the player to its timestamp.
- Search results gain **two sub-sections — transcripts and summaries — with a toggle** for
  all-or-individual.
- The rationale, which is the point of the feature: **a power user scans summaries faster; a
  journalist or investigator needs the full transcript evidence.** Those are different jobs and
  the toggle serves both without forcing a choice.

⚠️ Sections are **not a fixed schema** — summary generation varies by user prompt and context. The
result shape must key off whatever JSONB keys are present, never a hardcoded list of
BLUF/decisions/action-items.

### Frontend

- **Reuse the same filtering components the chat scope picker uses** (transcript / speaker /
  collection / tag) so search and chat share one filter vocabulary rather than growing two.
  Already present: `SearchableMultiSelect.svelte`, `CollectionsFilter.svelte`,
  `FilterSidebar.svelte`; chat's `FilePickerModal` composes `BaseModal` + `Tabs`.
- Advanced-search affordance when scoped to a collection or group.
- Light/dark parity; i18n keys across all 12 locales (`npm run check:i18n` enforces exact parity).

## 4. Redaction (#465) — shipped

`redaction/summary_redaction.py`'s `mask_summary` masks summary text at read time and is wired
through every surface that serves one: `summarization.py`, `search.py`, `summary_search.py`,
`summary_export_service.py`, `chat/export_redaction.py`, `aggregation_service.py`. The gap this
section originally described — a masked transcript sitting beside a completely unmasked summary
*of that same transcript*, which an abstractive model could restate the same PII into in its own
words — is closed.

**Why the cached spans do not help:** `redactions` is a column on `TranscriptSegment`, holding
char offsets into *segment* text. A summary is different, LLM-authored text; those offsets address
nothing in it. Masking a summary requires **detecting over it**, not applying stored spans. This is
what made it look expensive.

**What made it affordable:**

- `services/search/snippet_redaction.py` (`f02b3640`) already does this exact job: detect over
  arbitrary read-time text, gate on the user's enabled categories, fail closed.
- Presidio is warmed in the API process (`ce366efe`) — ~12.5 ms for a short text, vs a 9.9 s cold
  load before.
- ⚠️ **Detect per section, never batched.** `en_core_web_sm` reports each distinct `PERSON` **once
  per document**, so joining sections into one `analyze()` call leaks the name from every section
  after the first *while labelling the page as masked*. Measured on the snippet path: the batched
  version leaked in 31 of 32 snippets.

**Residual, tracked separately:** #822 — repeated per-read detection cost (not a correctness gap;
detection is not cached across reads of the same summary).

Display and search must not diverge — that divergence is precisely what made subtitle export a
separate defect from transcript display (#85 vs `0eecd839`).

## 5. Open questions

1. Does the summary result link to a section anchor in the modal, or does the modal open and
   scroll? The former needs stable section ids, which conflicts with §3's "sections are not a
   fixed schema". **Open.**
2. ~~Should summary search respect collection/group sharing the same way transcript search does?~~
   **Answered: yes.** `summary_search.py:20-22` reuses
   `PermissionService.get_accessible_file_ids_subquery` — the same sharing/tenant gate transcript
   search uses.
3. ~~Ranking between the two sub-sections when the toggle is on "all": interleaved by score, or
   grouped with transcripts first?~~ **Answered: grouped, not interleaved.** The `result_type`
   toggle at `search.py:147,204-210` decides which group is shown rather than fusing their scores.
