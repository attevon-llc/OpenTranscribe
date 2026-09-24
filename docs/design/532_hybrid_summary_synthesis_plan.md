# #532 follow-up: hybrid "summary + closing section" map tier — design and measurement plan

**Status:** PLAN ONLY. Nothing here is implemented.
**Written:** 2026-09-23, against `feat/v0.6.0-hybrid-summary-synthesis` @ `5d799a20`.
**Issue:** #532 (the synthesis gap: retrieval offers ~97% of scope, the answer cites ~73%).
**Preceding measurement:** arm (d), `chat.rag.map_tier_summaries`, run 2026-09-21 at `c9ec0380`
(#532 comment of that date; committed metrics in `backend/tests/eval/baselines/probe-532-{control,armd}/`).

This document is written for an implementation agent that has **no** conversation history.
Every file path is relative to the repository root. Re-verify every `path:LINE` before you edit;
line numbers drift.

---

## 0. TL;DR

Arm (d) swapped each file's `<overview>` entry from **three extractive digest sections** to **one
abstractive paragraph** and lost coverage. Re-reading that run's raw output (section 1) changes
what the follow-up has to be:

1. **The quote-fidelity loss was mostly a harness artefact.** With #976's fix plus two more
   normalisations (markdown `\_` escapes, `...` elisions), multi-file quote fidelity is 0.909 vs
   0.875 pooled, not 0.796 vs 0.519. Do not design around the −28-point number.
2. **The coverage loss is real but modest in evidence.** −17.0 points USED coverage, paired
   bootstrap 95% CI [−0.34, −0.01], sign test 5 better / 11 worse / 9 tied (p = 0.21). One run per
   arm, n = 25.
3. **The mechanism is visible.** Under arm (d) the model wrote **57%** of its answer sentences with
   no citation (control: 32%) and used 4.9 distinct citations per answer (control: 8.8). Base
   rule 12 tells it to "answer from the overview", and the overview is not citable. So it did.
4. **Content got worse too, not only citations.** AMI item recall fell from 47/443 to 26/443, and
   `decisions` recall from 0.16 to 0.03. Arm (d) rendered only `brief_summary or bluf`
   (`file_summaries.py:199-209`) and **never showed the model `key_decisions` or
   `action_items`**. The claim that "#464's abstractive map already carries decisions" was never
   actually tested.
5. **The extractive sections are time slices, not content types.** A digest is the transcript
   split into ≤8 contiguous parts, one TextRank-picked ~55-word section per part
   (`ingest_artifacts/digest.py:184-298`). The overview takes `sections[:k]`, which is the
   **opening** of each meeting. The chunks the model actually cites sit late: median relative
   position 0.76, 63% in the last 40% of the meeting, and 0.92 for action-item questions.

**The design:** for each file with a fresh LLM summary, the overview entry becomes
**[structured abstractive summary: lead paragraph + key decisions + action items] + [the CLOSING
digest section, verbatim]**. It **replaces** the control's `sections[:k]`; it is not added on
top. Its per-entry size ceiling is the one the control already has (`sections_budget(n) ×
DIGEST_SNIPPET_CHARS`). It sits behind one new experiment flag. A cheap offline check that
needs no GPU (Phase 0) confirms the section choice and the field choice before any GPU time is
spent. After that, one GPU window runs four arms (C1 → D → H → C2) on an expanded multi-file
question set with enough turns to detect a ~7-point effect. The pass and fail rules are written
down before the run (section 5).

---

## 1. What the existing evidence actually says (re-derived 2026-09-23)

### 1.1 Sources

- Committed, metrics only: `backend/tests/eval/baselines/probe-532-control/` and `probe-532-armd/`.
- Full-fidelity (contains prose, **gitignored, never commit**): preserved from `/tmp` to
  `.rag-403/probe-runs/532-control-c9ec0380/results.json` and
  `.rag-403/probe-runs/532-armd-c9ec0380/results.json` in the **main checkout**
  (`/mnt/nvm/repos/transcribe-app/.rag-403/`). The copies in `/tmp` will not survive a reboot.
- Both runs: gemma-4-e4b, `context_window` 60000, `budget_chars` ≈ 171k, temperature 0,
  `context_expansion_enabled` default ON (#523), 81 turns, `reducer: code` on every overview.

### 1.2 Findings

| # | Finding | Numbers | Consequence for the design |
|---|---|---|---|
| F1 | Arm (d)'s coverage regression is real but weakly evidenced | USED coverage (multi_file) C 0.767 → D 0.597; mean paired diff −0.170, bootstrap 95% CI [−0.340, −0.010]; 5 better / 11 worse / 9 tied, sign test p = 0.21. The #532 comment's "6 better / 9 worse / 10 same" does not reproduce from `metrics.md`. SD of the per-turn paired difference is 0.425, so **n = 25 detects only a difference of ≳17 points** | The decision set must be larger than 25 turns (section 4.2). Put numbers on noise (A/A, section 4.4) |
| F2 | The quote-fidelity "−27.7" is mostly grading artefacts | #976-fixed harness: C pooled 30/33 = **0.909** (turn mean 0.951), D 10/16 = **0.625** (0.519). Of D's 6 "unsupported" quotes, 2 are markdown escapes (`L\_C\_D\_`, `Triple R\_`), 2 are `...` elisions of text that is verbatim in the snippet, and 2 are genuine. With escape and ellipsis tolerance: C 0.909, **D 0.875** | Harness Unit U6 (section 3). Quote fidelity is a guard, not the target. D also made **half as many quotes** (33 → 16) |
| F3 | Mechanism: the model answered from the uncitable abstractive overview | Uncited-sentence fraction (sentences ≥6 words with no `[n]`): C **0.32** → D **0.57**. Distinct `[n]` per answer 8.8 → 4.9. Answer length unchanged (1,723 → 1,555 chars). Citation kinds: C 181 chunk + 40 digest, D 104 chunk + 18 digest | Rule 12 (`prompting.py:56`) says "answer from the overview … use the excerpts for specific quotes". An abstractive overview that is easy to answer from turns into uncited prose. This predicts that a hybrid which stays **uncitable** can raise *content* coverage without raising *citation* coverage. Hence the interpretation table in section 5.3 and the conditional H+a arm |
| F4 | The loss is not only citations: content also dropped | Citation-independent AMI item recall (`tests/eval/harness/ami_recall.py`, lexical floor): C 47/443 (turn mean 0.131) → D 26/443 (0.083); paired diff −0.048, CI [−0.100, −0.001]. Content coverage (recordings with ≥1 recalled item ÷ tagged recordings): C **0.367** → D **0.237**. By shape: decisions 0.16 → 0.03, problems 0.15 → 0.06, action_items 0.16 → 0.20, evolution 0.05 → 0.05 | The abstractive text that arm (d) used did not carry the answerable specifics |
| F5 | Arm (d) never rendered decisions or action items | `_summary_highlight_text` = `brief_summary or bluf` only (`backend/app/services/chat/mapreduce/file_summaries.py:199-209`). The summary's `key_decisions`, `action_items`, `follow_up_items` never reached the prompt | The hybrid's abstractive half is **structured**: lead + decisions + action items. Phase 0 gates this choice (rule R-items) |
| F6 | Digest "sections" are chronological partitions. The overview keeps the opening | `build_digest` partitions sentences into `ceil(words/1500)` contiguous groups, max 8 (`ingest_artifacts/sizing.py:section_count_for`), and TextRank-selects ~55-70 words **within each group** (`digest.py:212-237`). `scope_digest_hits` takes `sections[:sections_per_file]` (`file_summaries.py:382`) = the first ≤3 time slices. In the control, cited chunk start times relative to the recording: quartiles 0.40 / 0.76 / 0.94, 63% ≥ 0.6. By shape, the median is decisions 0.72, action_items 0.92, problems 0.78, evolution 0.71 | Keep the **closing** section `sections[-1]` (section 2.1). "Highest-ranked section" is not well defined, because TextRank scores are local to each partition |
| F7 | Arm (d)'s applied-check could not be observed from the run | `scope_digest_hits` computes `coverage["summary_hits"]` (`file_summaries.py:412-413`) but `_resolve_summary_tier` (`service.py:353-370`) drops it. No `results.json` row carries it, and no per-entry character volume was recorded "both ways" as revision 2 asked | Instrumentation Unit U4 is a **prerequisite** for any arm |
| F8 | `sections_budget` compares characters with a section count | `max(1, min(3, budget_chars // files))` with `DEFAULT_MAP_BUDGET_CHARS = 12000` (`mapreduce/overview.py:40,65`). `12000 // 25 = 480`, so it returns 3 at 25 files. The docstring promises that "a 25-file 'summarize everything' turn does not fetch three sections apiece", and the code does exactly that. It only drops below 3 above 4,000 files | Does **not** affect AMI (3-4 files). It matters for the hybrid's scale-aware ceiling at corpus scale. **Open question Q4** |
| F9 | Runs are not deterministic, and the retrieval cache leaks across arms | 1 of 22 lookup-route `single_specific` turns (untouched by the flag) flipped used coverage between runs. The control had `cache_hit: true` on 2 turns (`retrieved` 12 vs 48) | Flush or disable the retrieval cache per arm, and assert `cache_hit` false on every graded turn. Bracket the arms with two controls (A/A) |
| F10 | The baseline moved since `c9ec0380` | #975's pool sizing is a **no-op at 3-4 files** (`scope_aware_pool_size(48, per_file=12, scope=4)` = 48; `rerank_max_pairs` max(50, 48) = 50; the relevance floor applies only when the pool was widened). #976 changes grading only. **But** #963 (summary plane in index v6), `hybrid_search_service.py` (+417 lines) and `chat/retrieval.py` changed | **Do not reuse the old control or the old arm (d).** Re-run both at the implementation SHA |

How F1-F4 were computed, so that Unit U7 reproduces them. USED = |`files_consulted_uuids` ∩
`scope_file_uuids`| / |scope|. Quote fidelity = `traceability._quote_fidelity_counts` (post-#976).
The tolerant variant additionally strips `\` before `[_*`[]()#+-.!]` and splits the quote on
`...`/`…`, requiring each fragment (edge punctuation trimmed) to appear **in order** in the same
snippet. Uncited-sentence split: `(?<=[.!?])\s+|\n`. AMI recall = `ami_recall.score_answer(answer,
reference_answer)`. Bootstrap: 20,000 resamples of the per-turn paired differences, seed 0.

---

## 2. The hybrid design

### 2.1 Which extractive section to keep: the closing one, `sections[-1]`

The question "which of the 3 sections is most valuable" assumes the 3 sections are 3 different
*kinds* of content. They are not (F6). They are the first three time slices of the meeting. So the
choice is a **positional rule**, and the evidence points to the end of the meeting:

1. **Where synthesis actually draws its evidence (F6).** 63% of cited chunks start in the last 40%
   of their recording. Action-item questions draw from the last 10% (median 0.92). The control
   overview's `sections[:3]` covers the opening 3/N of a meeting with N sections, which is the
   *least*-cited region.
2. **What AMI's answerable content is.** The decisions, action items and wrap-up of a scenario
   meeting cluster at the close ("we have to take some decisions right now", verbatim in the
   IS1004b chunk the control cited). That is exactly what the due-outs slice
   (`test_shape_2_due_outs_specifically`) and `multi-024-IS1009-decisions` miss.
3. **It complements the other legs instead of duplicating them.** The query-relevant sections
   already reach the prompt **citably** through the ranked digest leg (`digests_retrieved: 6`,
   `service.py` `masked = digest_masked + masked`). A second relevance-picked section in the
   uncitable overview would duplicate that leg. A fixed positional slot adds material the ranker
   never guarantees.
4. **"Best-scored section" is not a well-defined alternative.** `rank` values are PageRank within
   one partition (`digest.py:217`), so they are not comparable across sections.

This is **pre-registered, not assumed**: Phase 0 (section 4.1) computes the offered-content AMI
recall of first / middle / last section, and rule R-sec can overturn this choice.

### 2.2 The abstractive half: structured, not the paragraph alone

Render the fresh `summary_data` as:

```
<lead>. Decisions: <d1>; <d2>; <d3>. Action items: <a1>; <a2>; <a3>.
```

- `<lead>` = the existing `_summary_highlight_text(summary_data)` (`brief_summary or bluf`), so the
  arm-(d) text is a strict subset of the hybrid's.
- Decisions: the first `HYBRID_MAX_ITEMS_PER_LEAF` (= 3) entries of `summary_data["key_decisions"]`.
  Actions: the first 3 of `summary_data["action_items"]`. Extract each item's text with
  **`app.services.chat.recurrence.normalize_leaf(raw, LEAF_KEY_DECISION | LEAF_ACTION_ITEM)`**
  (`recurrence.py:150`). It already handles the string shape and the dict shape
  (`decision`/`item`/`text` keys) and declines unrecognised shapes. **Do not write a second
  extractor.**
- Omit the `Decisions:` or `Action items:` clause entirely when it would be empty. A custom prompt
  with different field names therefore degrades to the lead paragraph, which is today's arm-(d)
  shape.
- `follow_up_items` / `major_topics` are **not** included. Keep the variable count down; Phase 0
  can motivate them later.
- Rule R-items (Phase 0) decides whether the items clause ships. If the items add no offered
  recall, the hybrid is the literal pre-registered "summary paragraph + 1 section".

### 2.3 The composition rule (exact)

Given a bounded scope of `n` files, a file `f` with digest sections `S` (list, may be empty) and a
**fresh** summary (`_summary_is_fresh`, unchanged):

```
entry_budget(n) = sections_budget(n) * DIGEST_SNIPPET_CHARS        # = 3 * 700 = 2100 at AMI scale
closing        = S[-1] if S else None                               # verbatim, NEVER truncated
abstractive    = structured_summary_text(summary_data,
                     budget = entry_budget(n) - len(closing.text or ""))
```

- `DIGEST_SNIPPET_CHARS` = `10 × ingest_artifacts.sizing.DIGEST_SECTION_MAX_WORDS` (700). Derive the
  budget from that constant exactly as `citations.py` does. If importing `citations` from
  `mapreduce` creates an import cycle, import `sizing` directly and add a unit test asserting the
  two stay equal. **Never write the literal 2100.**
- The closing section can never blow the budget. It is ≤ `DIGEST_SECTION_MAX_WORDS` (~70) words by
  construction (`digest.py:226`), about ≤ 500 chars, which is well under 700.
- `structured_summary_text` fills in the fixed order lead → decisions → actions. It cuts **at item
  boundaries**, drops whole trailing items, and never cuts inside an item. If the lead alone is
  over budget, cut the lead at the last sentence boundary inside the budget (reuse the
  sentence-boundary cut `prompting.format_excerpts` uses for `truncated="true"`; do not write a new
  splitter). If nothing fits, the abstractive half is `""`.
- **Fallbacks, each counted (Unit U4):**
  - No fresh summary, or the abstractive half renders to `""` → **exactly the control**:
    `S[:sections_per_file]`. Counter `entries_digest`.
  - Fresh summary and `S == []` → abstractive half only (the arm-(d) shape). Counter
    `entries_summary_only`. (With the hybrid flag **off** and `map_tier_summaries` on, which is
    arm D, every summary-represented file counts here too.)
  - Otherwise → hybrid. Counter `entries_hybrid`.
- **Replace, not add.** For a hybrid file the entry is abstractive + `S[-1]` **instead of**
  `S[:k]`. It is never `S[:k]` plus the summary. Worked example: a 4-section meeting in the
  control shows sections 0, 1, 2; in the hybrid it shows the summary plus section 3.
- **Duplicate avoidance is not attempted.** If `S[-1]` is also one of the ranked digest leg's
  citable excerpts this turn, it appears twice (overview + excerpt). That is harmless (the excerpt
  copy is the citable one) and it matches today's control, where overview sections can also be
  ranked excerpts. Record the overlap count (U4) so it is visible.

### 2.4 How it renders (CodeComposer)

Today, for each listed file (`reducers.py:196-212`):

```
- {title} ({date})
  {digest}
```

Hybrid entry (strings become module constants in `reducers.py`, passed through
`_sanitize_body_text` exactly like `summary.digest` today):

```
- {title} ({date})
  Summary (machine-generated): {abstractive}
  Closing discussion (verbatim): {closing}
```

- The **"machine-generated" label is required**, not decoration. It is the prompt-side form of #464
  constraint 4 / addendum G7: an LLM's interpretation must never be presented as something someone
  said. It also lets the model tell which text it may quote.
- A digest-only entry (the fallback) renders **byte-identically to today** (no labels). With the
  flag off, the block must be byte-identical to HEAD. Unit tests pin this.
- `BatchReducer._plain` (`reducers.py:321-330`) must render the same two labelled parts (through
  `_sanitize_attribute`, as today). The batch reducer never engages at 3-4 files, but a hybrid
  `FileSummary` must not silently lose half its content at corpus scale.
- **No base-rule change.** No rule 16, and rule 12 is not reworded. Both were rejected in #532
  (section 9 of the Sep 9 plan). The labels are data inside the block, not instructions.

### 2.5 Data model change: stop joining two different kinds of text

`build_file_summaries` joins every hit of a file into `FileSummary.digest` (`file_summaries.py:143`)
and sets `is_llm_summary = any(...)` (`:163-167`). Its own comment says that is safe **only
because a file is never mixed**. The hybrid breaks that invariant, so:

- Add `FileSummary.llm_summary: str = ""` (masked abstractive text).
- Add `FileSummary.digest_section_index: int | None`, `digest_start_time: float | None` and
  `digest_end_time: float | None`, populated only when `digest` came from exactly one section
  (the hybrid's closing section). U5 needs them for a real deep-link.
- `FileSummary.digest` stays **extractive-only** in every mode.
- `build_file_summaries` routes each masked hit by `hit.is_llm_summary`: summary hits go to
  `llm_summary`, section hits are joined into `digest` as today.
- `is_llm_summary` keeps its current meaning for the arm-(d) shape (`llm_summary != "" and digest
  == ""`). Add a property `is_hybrid` (both non-empty). Update the dataclass docstrings; the
  existing comment at `:163-166` becomes false and must be rewritten, not left.

### 2.6 Masking: unchanged paths, two hits per file

A hybrid file contributes **two** `ChunkHit`s to the **same** `mask_digests(...)` call
(`service.py:369`):

- the closing section as an ordinary digest hit (`chunk_index=-1-index`,
  `digest_section=index`, real `start_time`/`end_time`). It is provenance-masked per sentence,
  exactly like today's control sections;
- the abstractive text as today's arm-(d) summary hit (`chunk_index=-1`,
  `digest_section=len(sections)`, `is_llm_summary=True`). It goes through the out-of-range-index
  inline fallback that #464 validated.

⚠️ **Never route either hit through `mask_chunks`.** `test_chat_digest_masking.py::
test_the_chunk_path_over_discloses_a_digest` is the must-fire guard. The strictest-wins per-file
owner resolution and the `unmask_for_local` exemption apply unchanged, because they key on the
hit's file, not on its kind.

Hit order within a file: summary hit first, then the section. `build_file_summaries` must not
depend on this order (it routes by `is_llm_summary`), but deterministic order keeps
`results.json` diffs readable.

### 2.7 Citations: guard now, per-part citations only if measurement asks for them

- **Mandatory guard (U4).** When any `FileSummary.is_hybrid` is true and `overview_citable` is also
  on, `_overview_citation_start` (`service.py:1373`) returns `None` and sets
  `meta["overview_citable_suppressed"] = "hybrid"`. Without the guard, `build_overview_citations`
  (`citations.py:266-267`) would label the verbatim closing section `kind: "summary"` (or, after
  some future edit, the summary `kind: "digest"`). That is the #464/#532-arm(a) collision in a new
  form.
- **Conditional U5 (per-part overview citations).** Built only per the section 5.3 rule or Q2.
  Each hybrid entry gets **two** ids:
  `Summary (machine-generated) [s]: …` → `kind: "summary"`, `start_time: None`, snippet =
  `llm_summary`; `Closing discussion (verbatim) [d]: …` → `kind: "digest"`,
  `digest_section = digest_section_index`, real `start_time`/`end_time` (so the deep link is
  **not** `?t=0`), snippet = the section, capped at `DIGEST_SNIPPET_CHARS`. `Overview.cited_entries`
  becomes `(id, file_uuid, part)`. Update `build_overview_citations`, the `cited_entries` producer in
  `CodeComposer.reduce` (`reducers.py:186-202`), and the offered-citations append at
  `service.py:~2371` (leave its budget guard untouched). **Then delete the guard above.**

### 2.8 Token-budget impact

| Arm | Per-file overview content | 4-file overview (AMI) | Share of `budget_chars` ≈ 171k (60k window) | Share at an 8k window (`budget_chars` ≈ 15k) |
|---|---|---|---|---|
| C (control) | `S[:3]`, ≤ 3 × ~70 words | ≤ ~5.4k chars (measure: U4 `block_chars`) | ≤ 3% | ≤ 36% |
| D (arm d) | 1 paragraph | ~2-3k chars (unmeasured, F7) | ~1.5% | ~15-20% |
| H (hybrid) | abstractive (budget-filled) + `S[-1]` | ceiling 4 × 2,100 = 8.4k; expected 4-6k | ≤ 5% | ≤ 56% at the ceiling |

- The ceiling equals the control's **theoretical** ceiling (3 × `DIGEST_SNIPPET_CHARS` per
  file). In practice the hybrid will be **longer** than the control's realised size, because
  control sections run ~55-70 words, not 700 chars. So the hybrid is not volume-matched. It is
  **ceiling-matched**, and the realised size is **recorded per turn** (U4 `block_chars`, per-kind
  char sums) and reported beside every result. That way "more text" and "different text" can be
  told apart. If Phase 0 shows H's realised volume is > 1.5× C's, Q3 asks whether a
  volume-matched variant is needed.
- At a 60k window the budget does not bind (`chunks_dropped_for_budget = 0` on all 81 control
  turns). At an 8k window the overview comes off the top of the excerpt budget
  (`prompting._trim_evidence_blocks`). That is not measured here (CW-1 forces 60k). It is listed
  as a known limitation, not a claim.

### 2.9 Flag

One new experiment flag: **`chat.rag.map_tier_hybrid`** (bool, coded default `False`). It takes
effect only when `chat.rag.map_tier_summaries` is also `True`. Hybrid off plus summaries on = arm
(d) exactly as shipped. Both off = the control. The speaker-scoped map
(`scope_speaker_digest_hits`, `map_tier_speaker_summaries`) is **out of scope** and unchanged.

The flag's own lifecycle contract is the same as the arms'. After measurement it is **deleted**
either way (section 6).

---

## 3. Implementation units

The order is the dependency order. Each unit gets a red-first test: watch the new test fail
against HEAD using the `git archive HEAD` recipe in the root `CLAUDE.md`, never by swapping files
in the checkout. `cd backend && python3 ../scripts/audit-tests.py tests` must end at 0 open
findings before any commit. One writer commits (root `CLAUDE.md`).

### U1 — Flag plumbing (four coordinated edits + registry test)

1. `backend/app/core/constants.py`: add `DEFAULT_CHAT_MAP_TIER_HYBRID = False  #
   chat.rag.map_tier_hybrid` next to `DEFAULT_CHAT_MAP_TIER_SUMMARIES` (~`:1335`), with a
   comment naming #532, this plan, and "delete after measurement".
2. `backend/app/core/chat_flag_registry.py`: add a `ChatFlagSpec(field="map_tier_hybrid", …)`
   immediately after `map_tier_summaries` (~`:215`). Set `experimental=` **per Q5**.
3. `backend/app/services/chat/settings.py`: add a `map_tier_hybrid: bool =
   C.DEFAULT_CHAT_MAP_TIER_HYBRID` field after `map_tier_summaries` (`:71`), same field order as
   the registry.
4. `backend/app/schemas/chat.py`: `ChatAdminSettings` (`map_tier_hybrid: bool = False` after
   `:403`) **and** `ChatAdminSettingsUpdate` (`bool | None = None` after `:433`).
5. Tests: `backend/tests/unit/test_chat_flag_registry.py` must pass unchanged, since it enumerates
   the registry. Check whether `frontend/src/components/settings/ChatAdminSettings.svelte` /
   `ChatAdminSettings.experimental.test.ts` enumerate fields. If they do, update them and add the
   i18n key to **all 12 locales** (`npm run check:i18n`). If the panel is registry-driven, record
   that no frontend change is needed.

### U2 — Map step: `scope_digest_hits(..., hybrid: bool = False)`

File: `backend/app/services/chat/mapreduce/file_summaries.py`.

- Add a pure `structured_summary_text(summary_data: dict, budget_chars: int) -> str` implementing
  section 2.2 and the section 2.3 fill order. It lives next to `_summary_highlight_text` and
  imports `normalize_leaf`, `LEAF_KEY_DECISION`, `LEAF_ACTION_ITEM` from
  `app.services.chat.recurrence`. Check for an import cycle; if there is one, move `normalize_leaf`
  plus the leaf constants into a small shared module and re-export them from `recurrence`. Do not
  duplicate them.
- Add a `hybrid` kwarg and an `entry_budget_chars` kwarg (the caller computes it; see U4) to
  `scope_digest_hits`. In the fresh-summary branch (`:359-376`), when `hybrid` is true:
  append the summary hit with `content=structured_summary_text(...)`, then (if `sections`) the
  `sections[-1]` hit built exactly like the loop at `:382-394`, then `continue`. Handle the
  empty-render and no-section fallbacks per section 2.3.
- Coverage dict additions (present only when `use_summaries`): `summary_hits` (unchanged
  meaning: files represented by a summary), `hybrid_entries`, `summary_only_entries`,
  `summary_chars`, `closing_section_chars`.
- Flag off (`hybrid=False`): query and output **byte-identical** to HEAD. Pin it with a test that
  compares against the existing `test_chat_map_tier_summaries.py` fixtures.
- Tests (new `backend/tests/unit/test_chat_map_tier_hybrid.py`), each positive paired with its
  control:
  - hybrid emits exactly `[summary_hit, sections[-1] hit]` for a fresh 4-section file;
    **control:** the same fixture with `hybrid=False` emits one summary hit (arm d), and with
    `use_summaries=False` emits `sections[:3]`;
  - a stale or absent fingerprint → `sections[:k]` (hybrid never bypasses `_summary_is_fresh`);
  - a 1-section file → summary + that section; a 0-section file → summary only, counted;
  - `structured_summary_text`: fill order, item-boundary truncation, lead sentence-cut,
    string-shaped and dict-shaped items, an unrecognised shape → lead only, an empty-everything
    shape → `""` → digest fallback;
  - the section hit carries its **real** `digest_section`, `start_time` and `end_time`, and the
    summary hit carries `digest_section == len(sections)`.

### U3 — Summaries and rendering

Files: `mapreduce/file_summaries.py` (`FileSummary`, `build_file_summaries`),
`mapreduce/reducers.py` (`CodeComposer.reduce`, `BatchReducer._plain`).

- Make the section 2.5 dataclass changes and the section 2.4 rendering. The label strings are
  module constants.
- Tests:
  - a hybrid `FileSummary` has `digest` == the section text only and `llm_summary` == the summary
    only. **Control:** a digest-only file's `digest` is unchanged from HEAD (the join is preserved);
  - the CodeComposer block for a digest-only scope is **byte-identical to HEAD**. Build a golden
    string from HEAD's output with the `git archive` recipe. This is the must-stay-clean case;
  - a hybrid entry contains both labels, in order, and the machine-generated label precedes the
    abstractive text;
  - `BatchReducer._plain` includes both parts for a hybrid summary;
  - `_sanitize_body_text` still defuses `</overview>` / `<excerpt` inside the summary text (the
    summary is LLM output over untrusted transcript: treat it as untrusted too).

### U4 — Wiring, instrumentation, citation guard

File: `backend/app/services/chat/service.py`.

- `_resolve_summary_tier` (`:353-370`): pass `hybrid=settings.map_tier_hybrid and
  settings.map_tier_summaries` and `entry_budget_chars=sections_budget(len(file_uuids)) *
  <DIGEST_SNIPPET_CHARS>`. Return `map_hits.coverage` (or the needed counts) so that
  `_prepare_context` can fold it into `meta`. Today it is dropped (F7).
- `meta["overview"]` gains content-free counts (never text, same rule as `as_metadata`):
  `entries_digest`, `entries_summary_only`, `entries_hybrid`, `block_chars` (=
  `len(overview.block)`), `summary_chars`, `digest_chars`, and `closing_in_ranked_digests` (how
  many hybrid closing sections are also among this turn's ranked digest excerpts, matched by
  `(file_uuid, digest_section)`). Put the counts on `Overview.diagnostics`, so they flow through
  `as_metadata()` with no second path. Update `Overview.as_metadata`'s docstring.
- `_overview_citation_start`: the section 2.7 guard, plus
  `meta["overview_citable_suppressed"]`.
- `scripts/probe_chat_rag.py` already copies `msg_metadata` whole, so no probe change is needed
  for these. Confirm it by grepping one smoke-turn `results.json`.
- Tests: `backend/tests/unit/test_chat_session_phases.py` must still pass (no new session held
  across phases). Add assertions that a hybrid turn's `meta["overview"]` carries the counts and
  that a flag-off turn carries `entries_hybrid == 0`. Add a guard test: hybrid +
  `overview_citable` → `cited_entries == ()` and the suppression key is set. **Control:**
  `overview_citable` with a digest-only map still assigns ids.

### U5 — Per-part overview citations (CONDITIONAL; see section 5.3 and Q2)

As specified in section 2.7. It touches `mapreduce/overview.py` (`cited_entries` shape),
`reducers.py`, `citations.build_overview_citations`, and the offered-citations append in
`service.py`. Tests:

- a hybrid entry yields one `summary` and one `digest` payload with a non-`None` `start_time` on
  the digest one;
- a digest-only entry yields one `digest` payload exactly as today;
- `schemas.chat.Citation` round-trips both (a field not declared there is stripped on reload);
- `ChatSources.svelte` renders the summary badge for `kind: "summary"` and a deep link for the
  section. **Open the page in a browser** (root and global `CLAUDE.md`).

Delete the U4 guard when U5 lands.

### U6 — Quote-fidelity harness: two more normalisations

File: `backend/tests/eval/harness/traceability.py` (`_normalise`, `_quote_fidelity_counts`).

- **(a) Markdown escapes. Unambiguous; do it.** Strip a backslash before
  `` _ * ` [ ] ( ) # + - . ! `` on the quote side before comparison. The model's markdown
  rendering (`L\_C\_D\_`) is not a change in content.
- **(b) Ellipsis elision. Loosens a veto metric; needs Q1 sign-off.** Split the quote on
  `...` / `…`, trim edge punctuation, and require every fragment to occur **in order,
  non-overlapping, within the one cited snippet**.
- Must-fire cases (currently scored unsupported, must become supported): `"dump the L\_C\_D\_
  screen"` vs a snippet containing `dump the L_C_D_ screen`; `"big buttons... that's easier to use
  than"` vs a snippet containing both fragments in order.
- Must-stay-clean controls (must remain unsupported): fragments present but **out of order**
  (the real `multi-002` quote: `"… going to want... to do"` against `"going to... want it to do
  most"`); one fragment absent; fragments split across **two different** citations.
- Report quote fidelity **both ways** (strict post-#976, and tolerant) in U7 until Q1 is decided.

### U7 — Arm comparison tool

New: `backend/tests/eval/harness/arm_compare.py` (pure functions) plus a thin
`scripts/compare_probe_arms.py` CLI.
Input: N `results.json` paths with arm labels. Output: a **metrics-only** JSON/MD (no question,
answer, reference or snippet text), so it is safe to commit under `backend/tests/eval/baselines/`.

For each arm and category, it computes the section 5.1 metrics (M1-M6, G1-G8). For each pair of
arms it computes the paired per-turn difference over the matched labels, a bootstrap 95% CI
(20,000 resamples, fixed seed), and a two-sided sign test. It also runs the applied-checks
(section 4.3) and **refuses** (non-zero exit) when:

- a graded turn has `cache_hit` true;
- `min(budget_chars) ≤ 100_000` (CW-1);
- an arm's composition counters contradict its declared flags (e.g. arm H with
  `entries_hybrid == 0`, or arm C with `entries_hybrid > 0`);
- a turn errored or has `provider_error` in its warnings (T2 of the Sep 9 plan).

Tests: synthetic `results.json` fixtures, each check with a must-fire and a must-stay-clean
case, **including a fixture where every turn is identical across arms**. That fixture must give a
CI of exactly [0, 0] and p = 1 (the "cannot pass vacuously" control). Acceptance: when run on
`.rag-403/probe-runs/532-{control,armd}-c9ec0380/results.json` it reproduces F1-F4 to 3 decimal
places. Those arms predate U4, so run it with `--no-composition-check`.

### U8 — Expanded multi-file question set

File: `scripts/build_probe_question_set.py` (+ `backend/tests/eval/test_build_probe_question_set.py`).

Add `--multi-file-expanded`. It emits **only** `multi_file` entries, for **every** series with
≥3 usable meetings × **every** `MULTI_FILE_SHAPES` shape. It keeps only grounded entries
(`series_reference(...)` non-empty) and uses labels `multi-<series>-<kind>`, which are stable and
seed-independent. Expected size ≈ 34 series × 4 shapes ≈ 130; the builder logs the exact count
and the report states it. The AMI-81 set is untouched (`--multi-file-expanded` is additive).
Test: the expanded set is a superset of the AMI-81 set's multi-file (series, shape) pairs for the
same corpus.

### U9 — Phase 0 offline content oracle (no GPU, no LLM)

New: `scripts/overview_content_oracle.py`.
It reads `media_file` (`uuid`, `title`, `summary_status`, `summary_data`) and `file_facts`
(`digest`, `source_fingerprint`) from the measurement stack's Postgres (the same
`--pg-container` convention `build_probe_question_set.py` uses) and the AMI references via
`build_probe_question_set.series_reference`. For every (series, shape) question and every file
in the series it builds each **candidate entry text**:

| Code | Entry text |
|---|---|
| C | `" ".join(S[:3])` (the control) |
| S0 / Smid / Slast | one section: first, `S[len(S)//2]`, last |
| P | `_summary_highlight_text` (arm d) |
| PI | `structured_summary_text(..., budget=∞)` |
| H1 | P + Slast, budget-capped per section 2.3 |
| H2 | PI + Slast, budget-capped per section 2.3 (the proposed hybrid) |

It then scores `ami_recall.score_answer(entry_text, <reference items tagged with this file>)`.
Output (metrics only): per composition, pooled items recalled / total, per-file coverage (≥1 item
recalled), and mean/median chars; broken down by shape. It also reports the corpus
preconditions: files with a fresh summary (fingerprint match), the distribution of `len(S)`, and
the distribution of `len(key_decisions)` / `len(action_items)`.
**Import** `structured_summary_text` and `_summary_highlight_text` from the app code so the
oracle scores exactly what ships. Unit-test the scoring glue on a synthetic fixture.

---

## 4. Measurement protocol

### 4.1 Phase 0 — offline (no GPU). Decides section and fields; can stop the plan.

1. Stack: reuse the surviving corpus. The `otfresh-v060synth_*` volumes still exist (137 QMSum
   Product meetings, summaries generated 2026-09-21). Their `.fresh/v060synth.*` overlay files are
   gone. `./opentr.sh start dev --fresh v060synth --port-offset 900 --with-llm-test` regenerates
   the overlay and reattaches the named volumes. Run `./opentr.sh fresh-list` and
   `./opentr.sh data-paths` first. **Never run against the live NAS stack.** Check
   `nvidia-smi`: GPU 1 for the app, GPU 2 for `llm-test-vllm` only if idle, **never GPU 0**. If
   the volumes are unusable, re-inject with `./scripts/inject-eval-corpus.sh --fresh <name>
   --corpus qmsum` and regenerate summaries (`POST /api/files/{uuid}/summarize` per file, with the
   answering model gemma-4-e4b), then verify fingerprints as in step 2.
2. Preconditions (SQL, read-only; record the answers in the report):
   ```sql
   -- fresh summaries (the arm is a no-op for any file failing this)
   SELECT count(*) FILTER (WHERE mf.summary_status='completed'
            AND mf.summary_data->'metadata'->>'source_fingerprint' = ff.source_fingerprint) AS fresh,
          count(*) AS total
   FROM media_file mf JOIN file_facts ff ON ff.media_file_id = mf.id
   WHERE mf.title LIKE 'QMSum Product%';
   -- sections per file
   SELECT jsonb_array_length(ff.digest->'sections') AS n, count(*) FROM file_facts ff GROUP BY 1 ORDER BY 1;
   ```
   **Gate P0:** fresh ≥ 95% of the files in scope. Otherwise regenerate before anything else,
   because arm D/H silently degrades to C on a stale file (T9).
3. Build both question sets at the stack's SHA: AMI-81 (seed `20260820`, `--per-stratum 25`) and
   the U8 expanded set. File uuids are uuid5 (deterministic per meeting), so compare AMI-81's
   labels and uuids against `.rag-403/probe-runs/532-control-c9ec0380/results.json`. They must
   match.
4. Run U9 over the expanded set. Apply the pre-registered rules:
   - **R-sec:** keep `Slast` unless `S0` or `Smid` beats it on pooled recall by ≥ 20% relative
     **and** ≥ 5 items. In that case use that position, and record why.
   - **R-items:** ship PI (items included) if `PI` pooled recall > `P` pooled recall by ≥ 5
     items. Otherwise ship P, i.e. the literal pre-registered "paragraph + 1 section".
   - **Kill switch K0:** if the chosen hybrid's (H2 or H1) per-file coverage is < 75% of C's
     **and** its pooled recall < 75% of C's, the hybrid offers materially less answerable content
     than the control. **STOP**, report to David, spend no GPU time.
   - Caveat for the report: `ami_recall` is a lexical **floor**, and paraphrase scores as a miss,
     so it is biased *against* abstractive text. A hybrid that clears K0 despite that bias is
     strong evidence. A marginal fail is not proof of failure; escalate it rather than decide.
5. Commit U9's metrics-only output as `backend/tests/eval/baselines/probe-532-oracle/`.

### 4.2 Phase 1 — the GPU window

**Decision set:** the U8 expanded multi-file set (n ≈ 130). The SDs measured in F1 give 95% CI
half-widths of about **±0.073** on USED coverage and **±0.050** on content coverage. At n = 25
those are ±0.167 and ±0.115, too wide to decide anything below a 17-point effect.
**Guard set:** full AMI-81 (81 turns) for arms C1 and H only. It feeds the acceptance suite and
the single-file and negative-control guards.

**Arms, run in this order, one stack, one SHA, nothing edited in between (T12/T13):**

| Arm | `map_tier_summaries` | `map_tier_hybrid` | `overview_citable` | Set(s) |
|---|---|---|---|---|
| **C1** control | off | off | off | expanded + AMI-81 |
| **D** arm (d), same SHA | on | off | off | expanded |
| **H** hybrid | on | on | off | expanded + AMI-81 |
| **C2** control replicate (A/A + drift) | off | off | off | expanded |
| *(H+a, conditional)* | on | on | **on** (needs U5) | expanded |

- Per-turn comparisons use **C̄ = mean(C1, C2)** per turn, which halves control noise.
  **A/A rule:** if the C1−C2 USED-coverage CI excludes 0, there is drift or nondeterminism large
  enough to invalidate the window. Stop and investigate, and do not grade.
- **D is re-run** because "H beats the failed arm" is only meaningful at the same SHA (F10).
- Wall-clock (from the 2026-09-21 latencies, ~74 s per multi-file turn at `--concurrency 1`):
  about 2.7 h per expanded arm, about 1 h per AMI-81 arm, so **≈ 13 h** for C1/D/H/C2. `--concurrency
  2` roughly halves it and is acceptable for quality metrics, but it makes latency
  non-comparable (T15). If it is used, take the latency guard from the AMI-81 legs run at
  `--concurrency 1`. **Q6.**

**Per-arm procedure:**

1. Rebuild and `--force-recreate` the stack. `opentr.sh`'s `fresh_verify_baked_git_sha` must agree
   across HEAD, image, backend and worker. Record the SHA.
2. Set **all three** flags explicitly for the arm (even the ones that are off) via the admin
   settings API or UI. **Read them back** from `system_settings` (`SELECT key, value FROM
   system_settings WHERE key LIKE 'chat.rag.%'`) and paste the read-back into the arm's notes.
   Chat flags have no read-back guard (Sep 9 plan, section 5.0).
3. Flush the chat retrieval cache (the settings revision changes when a flag changes, but flush
   anyway). Assert `cache_hit` is false on every graded turn (F9).
4. **Smoke turn** (one multi-file question). Check the applied-check counters (section 4.3) and
   **dump the assembled prompt once**. For H it must show the two labelled lines per entry. For
   C it must show no labels and a byte-identical entry format.
5. Run:
   ```bash
   python3 scripts/probe_chat_rag.py --port <5174+offset> --question-set <set.json> \
     --llm-model gemma-4-e4b --llm-max-tokens 60000 --llm-temperature 0.0 \
     --out .rag-403/probe-runs/532h-<arm>-<set>-<sha> \
     --metrics-out backend/tests/eval/baselines/probe-532h-<arm>-<set> --run-name 532h-<arm>-<set>
   ```
   ⚠️ `--llm-max-tokens` defaults to **8192** (the CW-1 trap). Pass 60000.
6. Tee the log. Capture once; never re-run a slow suite for counts.

**After all arms:**

7. `scripts/compare_probe_arms.py` (U7) over expanded C1, C2, D, H (+H+a). Commit its
   metrics-only output as `backend/tests/eval/baselines/probe-532h-compare/`.
8. Judge the AMI-81 legs: `scripts/judge_chat_answers.py judge --results … --out
   .rag-403/labels/532h-<arm>-judgements.jsonl --judge-base-url … --judge-model <NOT gemma-4-e4b>`.
   `answer_judge.JudgeEndpoint` refuses a judge that is the answering model.
9. Acceptance suite against each AMI-81 leg:
   `RAG_ACCEPTANCE_RUN=.rag-403/probe-runs/532h-<arm>-ami81-<sha>
   RAG_ACCEPTANCE_JUDGEMENTS=.rag-403/labels/532h-<arm>-judgements.jsonl pytest
   backend/tests/eval/test_acceptance_query_shapes.py -v`. **Check the test count**, not only the
   exit code: the suite skips silently when artifacts are missing (T6).
10. Restore every flag to its coded default and read them back.

### 4.3 Applied-checks: the run is void if any fails

| Arm | Must hold on every multi-file turn |
|---|---|
| C1, C2 | `overview.entries_hybrid == 0` and `entries_summary_only == 0`; `entries_digest == files_listed` |
| D | `entries_hybrid == 0` and `entries_summary_only ≥ 1` on every multi-file turn; median `entries_summary_only / files_listed ≥ 0.95` |
| H | `entries_hybrid ≥ 1` on every multi-file turn; median `entries_hybrid / files_listed ≥ 0.95` (Gate P0 makes this expectable) |
| H+a | the H conditions, plus `len(offered_citations) > chunks_used`, and `prompt_membership_matches` **expected false** (do NOT "fix" it by suppressing payloads) |
| all | `min(budget_chars) > 100_000`; `cache_hit` false; no `provider_error`; `overview.truncated` false; `reducer == "code"` |

---

## 5. Success and failure criteria (pre-registered)

### 5.1 Metrics (multi-file, per turn; U7 computes them)

- **M1 USED coverage** (the #532 headline): |cited files ∩ scope| / |scope|. Denominator is
  **`files_in_scope`**, never offered.
- **M2 Content coverage** (independent of citations): recordings with ≥1 AMI item recalled ÷
  recordings tagged in the reference (`ami_recall.score_answer(...).recordings_covered()`).
- **M3 AMI item recall**, pooled and turn-mean (a lexical floor, used to rank arms, never as an
  absolute).
- **M4 Quote fidelity**, pooled, strict (post-#976) **and** tolerant (U6b). Reported both ways
  until Q1.
- **M5 Uncited-sentence fraction** (the mechanism metric from F3).
- **M6 Judge non-none fraction** on AMI-81 multi_file and on the due-outs slice.
- Guards: **G1** single_specific M1 (lookup route, must not move); **G2** all 6 negative controls
  declined and cite nothing from scope; **G3** `citation_resolution_rate` min 1.0; **G4**
  `citations_leaked` 0; **G5** latency median < 120 s; **G6** acceptance suite passes with its
  full test count; **G7** OFFERED coverage unchanged vs C̄ (H does not touch retrieval, so any
  move is a bug); **G8 (H+a only)** zero quoted spans (`"…" [n]`) attached to a `kind: "summary"`
  citation. Quoting machine-generated text as speech is the G7-addendum violation.

### 5.2 "H beats both" — the numeric bar (expanded set, n ≈ 130, paired vs C̄ and D)

**Beats D (sanity bar; failing it means the implementation or the instrument is broken):**
H − D on M1 **and** on M2, each with the 95% CI lower bound > 0.

**Beats C (the real bar). All must hold:**

| | Criterion |
|---|---|
| Primary | **M1:** H − C̄ ≥ +0.05, with the 95% CI lower bound > 0 |
| Content | **M2:** H − C̄ 95% CI lower bound > −0.03 (non-inferior); **M3** pooled H ≥ C̄ pooled − 2% absolute |
| Grounding | **M4** pooled (strict) H ≥ C̄ − 0.05; **M5** H ≤ C̄ + 0.10 |
| Guards | G1-G7 all pass; G1: single_specific M1 H − C1 within ±0.04 (the 1/22 flip rate seen in F9) |
| Judge | M6 due-outs slice ≥ the current floor 0.50 on the H AMI-81 leg (`test_acceptance_query_shapes.py:203`) |

For reference, arm (d) at `c9ec0380` scored M1 −0.170 and M2 −0.130 vs its control. H must turn
both around.

### 5.3 Interpretation table (decided now, not after the numbers)

| M2 (content) H − C̄ | M1 (citation) H − C̄ | Reading | Action |
|---|---|---|---|
| CI lower > 0 | meets section 5.2 primary | **WIN** | Promote (section 6.1) |
| CI lower > 0 | CI includes 0, or below | **Content-only win.** Richer overview content is being used but cannot be cited (F3's mechanism) | Build U5; run **H+a vs H** as a single-variable arm (citability only). Promote H+a if it meets section 5.2 vs C̄ and passes G8 |
| CI includes 0 | CI includes 0 | **Null** | Do not promote. Delete `map_tier_hybrid` (section 6.2). Post the table on #532 |
| CI upper < 0 **or** | CI upper < 0 | **Loss** | Delete `map_tier_hybrid`. Post the table on #532 |

A result that is significant on M1 but negative on M2 (more citations, less content) is **not** a
win. Report it as a divergence and escalate it to David.

**Prediction on record (so the result can surprise us):** from F3, the most likely outcome of H
is the **content-only win** row, and the most likely overall winner is H+a.

### 5.4 What RED looks like (void runs, not results)

- `entries_hybrid == 0` under H, or `summary_hits` absent: the arm never applied.
- `budget_chars` ≈ 15k: you measured an 8k model.
- The A/A CI excludes 0: the window is noisy or drifting.
- A coverage drop with `provider_error` in `warnings`: a crash, not a tuning result.
- The acceptance suite reports "8 skipped": the artifacts are missing, not passing.
- `results.json` test labels that do not match the question set: a stash-window or wrong-set run.

---

## 6. After the measurement

### 6.1 If H (or H+a) wins

- Flip `DEFAULT_CHAT_MAP_TIER_SUMMARIES` to `True` with a derivation comment naming the
  committed compare artifact. Make the hybrid **the** summary shape and **delete** the
  paragraph-only arm-(d) branch in `scope_digest_hits`. The repo rule is one path per job, so a
  losing shape does not stay behind a flag.
- Delete `map_tier_hybrid` (all four U1 edits plus any frontend and i18n). If H+a won, also flip
  `overview_citable` default on and delete that flag; rewrite `build_overview_citations`'s
  "EXPERIMENT support — delete with the arm" docstring (`citations.py:222`) into a supported
  path; and update `OVERVIEW_SNIPPET_CHARS`'s docstring, whose "3 sections" premise no longer
  holds.
- **J9:** the same PR raises `test_acceptance_query_shapes.py` floors (due-outs `:203`,
  multi_file `:191`, and offered `:164` if H+a moved it) to just below the measured values, and
  names the basis run in the docstrings.
- Docs: `backend/app/services/chat/CLAUDE.md` (the overview section and the citation-kinds table).
  Also update `docs-site/docs/developer-guide/rag-design-and-validation.md` and the relevant
  `rag-evaluation.md` section with the result.

### 6.2 If H loses or is null

Delete `map_tier_hybrid` and every U2-U4 branch it gates. **Keep** U4's instrumentation
counters, U6, U7, U8 and U9: they are instrument improvements that stand on their own. Post the
arm table on #532. Whether `map_tier_summaries` itself (#464) is deleted is **Q7**.

---

## 7. Traps specific to this work (on top of the Sep 9 plan's T1-T15)

- **H1.** `build_file_summaries`'s "`any()` and `all()` agree" comment (`file_summaries.py:163-166`)
  becomes false the moment a file carries two hit kinds. If the join is left in place, the hybrid
  ships verbatim text labelled as a summary (and vice versa via `build_overview_citations`).
  Section 2.5 is not optional.
- **H2.** The flag-off block must be byte-identical. Any whitespace drift in `CodeComposer`
  changes the control and invalidates the comparison against the committed baselines.
- **H3.** `structured_summary_text` output is **LLM-generated text over an untrusted
  transcript**. It goes through `_sanitize_body_text` like any digest, and through
  `mask_digests` (inline fallback) like arm (d)'s paragraph. Never through `mask_chunks`.
- **H4.** The oracle (U9) and the grader (M2/M3) share the AMI references. The oracle only picks
  among two or three **generic positional/field rules**, which limits the overfitting, but say so
  in the report.
- **H5.** At n ≈ 130, a single noisy run can still land on the wrong side of a threshold. The A/A
  pair is the defence. Do not replace it with a repeat of H.
- **H6.** `sections_budget` (F8) returns 3 at every realistic scope. The hybrid's
  `entry_budget(n)` inherits that. The results here say nothing about scopes where the formula
  would bite (> 4,000 files) or where it *should* bite (≈ 10-30 files). J14 (no corpus-scale
  multi-file arm) still applies.
- **H7.** Summaries were generated by the answering model itself (gemma-4-e4b). A stronger
  summarizer could change the answer. That is out of scope; record the summarizer model in the
  report.

---

## 8. Open questions for David

- **Q1 — Ellipsis-tolerant quote fidelity (U6b).** It loosens a veto metric: a quote `"A... B"`
  counts as supported when A and B appear in order in the cited snippet. Its must-stay-clean
  controls are written, and on the existing runs it moves the control by 0 quotes and arm (d) by 2.
  Adopt it as the reported number, or keep it as a secondary column only? (The markdown-escape
  fix U6a is unambiguous and proceeds regardless.)
- **Q2 — Build U5 (per-part overview citations) up front and run H+a in the same GPU window?**
  The mechanism analysis (F3) predicts the "content-only win" row. Building U5 up front saves a
  second stack setup (≈ half a day) at the risk of building U5 for nothing. Recommendation:
  **yes, build it up front and run H+a as a fifth arm**, still graded as H → H+a (one variable).
- **Q3 — Ceiling-matched vs volume-matched.** The hybrid is capped at the control's *ceiling*
  but will usually carry more text than the control's *realised* entries. If Phase 0 shows H's
  realised size > 1.5× C's, add a volume-matched variant (cap = the control's realised per-file
  chars), or accept "more, differently composed text" as the thing being tested?
- **Q4 — Fix `sections_budget`'s units defect (F8) in this branch?** It is a real defect whose
  docstring promises behaviour the code does not have. Fixing it (e.g. divide by a derived
  per-section char size) changes the shipped control **only above ≈ 10 files**, so it needs its
  own before/after on the #829 corpus-scale fixture. Options: fold it in as a separate commit
  plus a corpus-scale re-measure (the repo's "fix, don't defer" norm), or file it as its own
  `epic:rag-quality` issue.
- **Q5 — Mark `map_tier_hybrid` `experimental=True`** (J8)? It is the textbook case: a
  delete-after-measurement flag. But the field also moves it into the admin UI's
  "requires an LLM provider" subsection, and this flag genuinely does need a summary-capable
  provider. Recommendation: **yes**, which also makes it the first real user of that field.
- **Q6 — GPU budget:** ≈ 13 h at `--concurrency 1` (C1/D/H/C2, expanded + two AMI-81 legs), plus
  ≈ 2.7 h for H+a. Accept `--concurrency 2` for the expanded legs (latency taken from the AMI-81
  legs only)?
- **Q7 — If the hybrid loses:** keep `map_tier_summaries` (#464) as a default-off flag, or delete
  the arm-(d) branch too? By the flag contract it should go, since arm (d) itself has now
  measurably lost twice (at `c9ec0380`, and presumably again as D here).

---

## 9. Provenance

- Code read at `5d799a20`: `backend/app/services/chat/mapreduce/{file_summaries,overview,reducers}.py`,
  `chat/citations.py`, `chat/service.py` (`_resolve_summary_tier`, `_overview_citation_start`,
  `_finalize_overview_citations`, `_prepare_context` phase 4-5), `chat/prompting.py:56`,
  `chat/recurrence.py:150`, `ingest_artifacts/{digest,sizing}.py`,
  `search/chunk_retrieval.py:204` (`scope_aware_pool_size`), `core/chat_flag_registry.py`,
  `scripts/{probe_chat_rag,build_probe_question_set,judge_chat_answers}.py`,
  `backend/tests/eval/harness/{traceability,ami_recall}.py`,
  `backend/tests/eval/test_acceptance_query_shapes.py`.
- Issues read in full: #532 (all comments through 2026-09-21), #975, #976 (with #977's fix
  comment). #829's relevant finding is restated in #532's 2026-09-21 comment and in #975.
- Every number in section 1 was computed read-only from the two preserved `results.json` files
  with the repo's own harness functions (`backend/venv`). No stack was started, no LLM was
  called, and no production code was changed.
