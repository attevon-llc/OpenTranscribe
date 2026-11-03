# search — snippets are masked, and the tags make that non-obvious (#86)

Moved intact from `backend/app/services/search/CLAUDE.md`. Read before touching highlight/snippet building or the search response cache key.

## Snippets are masked, and the tags make that non-obvious (#86)

**A snippet is unredacted transcript text.** It comes out of `transcript_chunks`, which stores
transcript text UNREDACTED by design, so it carries whatever the recording carried. Search
spans collection and group shares, so an unmasked preview reaches readers whose transcript
view would have masked it.

`_redact_snippets` masked `profanity` and `custom` only, on a docstring's claim that "this path
never carries PII spans". It does. A user whose policy masks **only** `pii` intersected to the
empty set and got **every snippet on every page verbatim**. Four things about the fix:

- **`MASKABLE_CATEGORIES` is `{pii, profanity, custom}` — `toxicity` is absent on purpose.**
  That detector emits a per-segment SCORE and never a span (`redaction/config`'s
  `_DETECTOR_CATEGORIES` maps it to the empty set); the `toxicity` *category*'s maskable spans
  come from the `llm` detector, a provider round-trip with no business on a search request. A
  toxicity-only policy therefore masks nothing here, correctly.
- **`<mark>` tags are split out before detection and restored after.** The wordlist path was
  safe by accident — its regex only matches word-boundary alphanumeric runs, so it can never
  touch a `<`. Presidio has no such property, and OpenSearch does **not** wrap whole words:
  real output includes `<mark>budget ? Not the </mark>original`. A span really can begin inside
  a highlight and end outside it; masking the raw string would emit an orphan `</mark>` and the
  frontend sanitizer then drops the fragment. A span crossing a tag masks each side separately
  (two adjacent labels), because the alternative swallows the tag.
- **⚠️ Detection is ONE CALL PER SNIPPET, and batching the page is forbidden.** Packing
  snippets into shared `analyze()` calls is the obvious optimisation, it is **2.2-3.0x
  faster**, and it **loses PII**: `en_core_web_sm`'s NER reports each distinct `PERSON`
  **once per document**. Measured directly — `Talia Yarrow` in two joined snippets yields
  one span, in three yields one, while three snippets naming three *different* people yield
  three. Through the live search API on a real page, the batched version left the name in
  clear in **31 of the 32 snippets containing it**, with `[NAME]` labels elsewhere on the
  same page so the result looked masked. A search page is by construction a set of fragments
  about the same subject, which is exactly the input that property destroys. The batched
  implementation was written, measured, and deleted; `_detect` carries the warning.
  - The same spaCy property means a name repeated **inside one snippet** is masked only at
    its first mention. That residual is app-wide (cached segment detection has it too), not
    a property of this path, and is not fixable here.
- **Cost, and why it is not the fail-closed branch's problem.** Warm Presidio, steady state,
  real pages from the live stack (94-200 snippets, 9-39k chars): **0.8-2.1 s** per page for a
  `pii`-masking user, against a 305-1372 ms baseline search. A `profanity`-only user pays
  **2.6-13.5 ms** (the detector gate is on the *categories*, not on `enabled`) and a
  redaction-disabled user pays nothing — and PII masking is opt-in twice over, since redaction
  is opt-out and `pii` is not in `DEFAULT_REDACTION_CATEGORIES`. Masking is applied before the
  per-user response cache (`SEARCH_CACHE_TTL_SECONDS`, 300 s), so a repeat query pays zero.
  Withholding the previews instead (as the fail-closed branch does) was rejected: that branch
  exists for when we *cannot* mask, not when masking is merely slow, and a search UI with no
  previews is the feature removed rather than a trade.
  - **FIXED: the response cache keys on the redaction policy.** It used to key only on
    `user_id`, so for up to `SEARCH_CACHE_TTL_SECONDS` after a user enabled masking, queries
    they had already run still returned the previously cached unmasked snippets — the same
    class of leak `redaction/export_policy.py` argues in general (a cached artifact must name
    the policy it was rendered under). `HybridSearchService.search` now resolves the config
    via `_resolve_redaction_config_for_cache` **before** the cache lookup — not inside
    `_redact_snippets`, which used to run only after a cache miss — and folds
    `_redaction_policy_fingerprint(cfg)` into `_make_cache_key` as `redaction_policy`. The
    fingerprint covers only the fields that can move `mask_snippets`'s output on this surface
    (`enabled_categories & MASKABLE_CATEGORIES`, `pii_entities`, `custom_words`, `allowlist`)
    — `toxicity_threshold` and `style` are excluded because neither can change a rendered
    snippet here: `toxicity` has no maskable spans on this path, and `mask_snippets` always
    forces `style="label"` regardless of the user's own preference. An unresolvable config
    gets its own fixed `"unresolvable"` bucket so it can never collide with a real policy's
    fingerprint. `cfg` is resolved exactly ONCE per request and passed into `_redact_snippets`
    (now a required third parameter) rather than re-resolved there, so the config that decided
    the cache key is provably the config that did the masking.
  - The first masked search in a fresh API process pays the **~7-10 s Presidio build** unless
    `redaction/warmup.py` already ran, and that warm-up's gate is evaluated at startup — so a
    user who enables redaction on a process that started with it off pays it once. Measured
    end to end through the API: 14.7 s on the first such query, 1.2 s on the next.

Two rules the implementation depends on, both easy to undo by accident:

- **The config session closes BEFORE any detector runs.** A ~1 s Presidio pass inside an open
  transaction holds `ACCESS SHARE` for its duration and queues every `ALTER TABLE` behind it —
  exactly what `scripts/audit-session-lifetime.py` exists to catch, and what hangs an Alembic
  upgrade mid-release.
- **Fail-closed uses `redaction.config.blocking_detector_failures`, not a second rule.** A
  detector failure withholds the page's snippet text only when it feeds a category *this* user
  masks; a box with no Presidio must not cost their previews to a profanity-only user.

Not masked, deliberately: `title_highlighted` and `speaker_highlighted`. Both are
user-assigned labels rather than transcript text, and masking a speaker name is what the
speaker plane exists to let people *set*.
