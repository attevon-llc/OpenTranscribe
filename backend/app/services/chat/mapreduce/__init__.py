"""Map-reduce over digests — the "1000 transcripts" path (#403 Stage 4, Phase 4).

The problem in the owner's words: *"If I have 1000 transcripts and someone says
'give me a summary of all the transcripts', that is impossible to feed to an
LLM."* And the shape of the answer, also his: *"it's very rarely that a Claude or
ChatGPT chat session is one single large chat session — it's multiple small fast
quick calls combined into one master result."*

This is the industry-standard **map-reduce / `tree_summarize`** pattern, and the
digest plane it maps over is a **DocumentSummaryIndex**. Named here so the names
travel with the code.

## Two levels, and the first one is already paid for

```
transcript chunks  ──(TextRank, at ingest, NO LLM)──▶  file digest      ← the MAP
file digests       ──(code, or N small bounded calls)─▶  collection view ← the REDUCE
```

Level 1 is `services/ingest_artifacts` and it ran when the file was ingested, so
a summary over 1,000 recordings costs **zero** map-time work. That is the whole
reason the extractive digest was built deterministically: a map step that needed
an LLM would make the corpus-scale case exactly as impossible as it was before.

**Not a recursive tree.** Two levels only, deliberately — RAPTOR-style recursive
clustering is interesting for a corpus with no natural structure, and ours has
one: real meeting and speaker-turn boundaries. Recording it as measured future
work rather than scope.

## Two reducers, one interface, and the no-LLM one is FIRST CLASS

:class:`~app.services.chat.mapreduce.reducers.CodeComposer` renders the
collection view in code — N recordings, date span, total duration, speaker
roster, recurring keyphrases, then per recording a title, date and its
extractive digest. It is not a degraded fallback: **D6** makes the
`LLM_PROVIDER`-empty deployment first class, and this is what gives it an
answer to "summarize this collection" at all.

:class:`~app.services.chat.mapreduce.reducers.BatchReducer` spends an LLM, in
**many small bounded calls** rather than one impossible one — the owner's
framing, implemented literally.

## What it produces, and why that is not a third summarization path

Both reducers return an ``<overview>`` **context block**, not an answer. The turn's
existing streaming call is the final reduce, which means:

* one summarization path, not three — `media_file.summary_data` (the file page)
  is untouched, and the retired `transcript_summaries` index (#67) is not read or
  written here;
* the answer still streams, and `[n]` citations still resolve, because the
  reduced chunk leg runs alongside;
* the model composes the master result, which is the job it is good at.

## Assembly is concatenation-only

Same rule as `prompting.py` and for the same reason: a recording title
containing ``{evil}`` would raise or interpolate under `str.format`.

⚠️ **This section used to claim every value reaching a prompt here went
through ``prompting._sanitize_attribute`` first. That was false, and the gap
was a real cross-user prompt-injection surface.** Per-file titles and digests
in the listed-recordings section always went through it — but the CORPUS
HEADER's speaker roster and recurring-keyphrase list (``reducers._corpus_header``)
were interpolated into the ``<overview>`` block completely unsanitized. Speaker
display names are OWNER-controlled on a shared recording, so on a
multi-tenant deployment a name containing `` </overview><synthesis> `` was
attacker-controlled text landing, unescaped, in the highest-trust part of the
prompt the model is told to treat as authoritative (base rule 12).

Fixed: the roster and keyphrases now go through
``prompting._sanitize_body_text`` — the BODY-safe sanitizer, not the
attribute one. That distinction matters here specifically: the attribute
sanitizer caps at 120 chars, which is fine for one title but would silently
truncate a roster of a dozen names or a keyphrase list mid-render if applied
per-block instead of per-value. Per-file titles and digests in the listed
section keep using ``_sanitize_attribute``, unchanged.

## This package, and why it is a package

Split from a single 1242-line ``mapreduce.py`` (against this repo's ~300-line
guideline) into four seams, none of which changed behaviour:

| Module | Owns |
|---|---|
| `overview.py` | Sizing constants, `Overview`, `sections_budget`, `_clock` |
| `file_summaries.py` | The per-recording MAP: `FileSummary`, `scope_digest_hits`, `build_file_summaries` |
| `document_scope.py` | The document arm of the #403 Stage-6 mixed-collection gate |
| `speaker_map.py` | The per-speaker MAP (W2.3): `scope_speaker_digest_hits` |
| `reducers.py` | The REDUCE half: `CodeComposer`, `BatchReducer`, `build_overview` |

Every name this package exported before the split is re-exported below,
**including the underscore-prefixed helpers** — several are imported directly
by unit tests that exercise one pure function in isolation, and a pure move
must not break them. `from app.services.chat.mapreduce import X` continues to
work unchanged for every `X` that worked before.
"""

from __future__ import annotations

from app.services.chat.mapreduce.document_scope import _document_scope_hits
from app.services.chat.mapreduce.document_scope import document_scope_hits
from app.services.chat.mapreduce.file_summaries import MAP_TIER_SPEAKER_SUMMARIES_SETTING_KEY
from app.services.chat.mapreduce.file_summaries import MAP_TIER_SUMMARIES_SETTING_KEY
from app.services.chat.mapreduce.file_summaries import DigestScopeHits
from app.services.chat.mapreduce.file_summaries import FileSummary
from app.services.chat.mapreduce.file_summaries import _load_facts
from app.services.chat.mapreduce.file_summaries import _speaker_facts_entry
from app.services.chat.mapreduce.file_summaries import _speaker_in_roster
from app.services.chat.mapreduce.file_summaries import _summary_highlight_text
from app.services.chat.mapreduce.file_summaries import _summary_is_fresh
from app.services.chat.mapreduce.file_summaries import build_file_summaries
from app.services.chat.mapreduce.file_summaries import scope_digest_hits
from app.services.chat.mapreduce.overview import DEFAULT_BATCH_FILES
from app.services.chat.mapreduce.overview import DEFAULT_MAP_BUDGET_CHARS
from app.services.chat.mapreduce.overview import MAX_LISTED_FILES
from app.services.chat.mapreduce.overview import MAX_REDUCE_CALLS
from app.services.chat.mapreduce.overview import Overview
from app.services.chat.mapreduce.overview import _clock
from app.services.chat.mapreduce.overview import sections_budget
from app.services.chat.mapreduce.reducers import _BATCH_SYSTEM
from app.services.chat.mapreduce.reducers import BatchReducer
from app.services.chat.mapreduce.reducers import CodeComposer
from app.services.chat.mapreduce.reducers import _corpus_header
from app.services.chat.mapreduce.reducers import _empty_speaker_focus_overview
from app.services.chat.mapreduce.reducers import _speaker_focus_header
from app.services.chat.mapreduce.reducers import build_overview
from app.services.chat.mapreduce.speaker_map import _owner_matched_action_items
from app.services.chat.mapreduce.speaker_map import _sentence_speaker_in
from app.services.chat.mapreduce.speaker_map import _speaker_summary_entry
from app.services.chat.mapreduce.speaker_map import _speaker_summary_highlight_text
from app.services.chat.mapreduce.speaker_map import _speaker_summary_text_for_any
from app.services.chat.mapreduce.speaker_map import scope_speaker_digest_hits

__all__ = [
    "MAP_TIER_SPEAKER_SUMMARIES_SETTING_KEY",
    "MAP_TIER_SUMMARIES_SETTING_KEY",
    "DEFAULT_BATCH_FILES",
    "DEFAULT_MAP_BUDGET_CHARS",
    "MAX_LISTED_FILES",
    "MAX_REDUCE_CALLS",
    "BatchReducer",
    "CodeComposer",
    "DigestScopeHits",
    "FileSummary",
    "Overview",
    "build_file_summaries",
    "build_overview",
    "document_scope_hits",
    "scope_digest_hits",
    "scope_speaker_digest_hits",
    "sections_budget",
    "_BATCH_SYSTEM",
    "_clock",
    "_corpus_header",
    "_document_scope_hits",
    "_empty_speaker_focus_overview",
    "_load_facts",
    "_owner_matched_action_items",
    "_sentence_speaker_in",
    "_speaker_facts_entry",
    "_speaker_focus_header",
    "_speaker_in_roster",
    "_speaker_summary_entry",
    "_speaker_summary_highlight_text",
    "_speaker_summary_text_for_any",
    "_summary_highlight_text",
    "_summary_is_fresh",
]
