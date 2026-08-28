# Changelog

All notable changes to OpenTranscribe will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Release-rehearsal coverage for `opentr.sh backup`/`restore` and `opentranscribe.sh
  update --rollback`** (#598). `test-upgrade.sh` proved the forward upgrade path but never
  exercised the documented recovery path — exactly what an operator reaches for in a real
  emergency. Five new phases (13-17), landing on top of #599's restore fix: phase 12 asserts
  the rollback precondition (`# OT_PREVIOUS_IMAGE_TAG`) a real `update --version` records —
  phases 07/08 now invoke that command instead of a hand-rolled `.env` rewrite, since the old
  rewrite never recorded it and a `--rollback` at the end of the scenario used to exit 1 with
  "no previous version recorded". Phase 15 restores the phase-06b pre-upgrade backup over
  damage inflicted through the real API and asserts **content digests**, not row counts (a
  delete+insert pair leaves counts unchanged) — including that a table introduced by a
  post-FROM migration does not survive the restore, derived from a table-list diff rather than
  a hardcoded name. Phase 16 runs the real `update --rollback` and asserts the FROM image
  actually **serves** the restored FROM database through its own API (login, file list,
  transcript text) — not merely that the command exited 0. Phase 17 proves the documented
  recovery loop (roll back → re-upgrade) completes cleanly. A `ROLLBACK_INJECT_FAULT`
  self-check (`truncate`/`no-damage`/`stale-oracle`, wired into the new
  `selftest-rollback-fault-injection.sh`, ~1 minute against a throwaway isolated Postgres
  container) deliberately breaks the tail so its own failure detection is exercised for real —
  a leg that silently asserts nothing looks exactly like a leg that passes. Along the way:
  restoring a plain-format `pg_dump` truncated mid-`COPY` was measured to replay with exit 0
  and silently wrong data (psql treats an unterminated `COPY ... FROM stdin` at EOF as simply
  ending the copy, not a parse error) — the same "reports success, changed nothing" shape #599
  fixed in the product, now pinned as a property of the rehearsal's own fault-injection design
  rather than assumed. Three new guardrails in `lib/guardrails.sh`
  (`gr_assert_target_is_test_database`, `gr_fingerprint_repo_backups`/
  `gr_assert_repo_backups_untouched`, `gr_assert_not_repo_cwd`) protect against the tail's
  `DROP DATABASE` touching anything but this run's own database, and against a staged
  `opentr.sh` invocation (bare `docker compose`, no `-f` chain, `./backups` relative to CWD)
  writing into the repo checkout. `--no-rollback` / `ROLLBACK_REHEARSAL=0` opts out;
  `--only-rollback` resumes at phase 12 against an already-completed run. Deliberately out of
  scope, each for a stated reason: `backup --encrypt` (unattended `gpg` has no
  `--passphrase-file`), the in-app scheduled-backup system's own end-to-end restore proof, and
  MinIO/OpenSearch restore (the DB restore does not touch either — asserted, not just
  unclaimed).
- **Production installs now have a shipped `backup`/`restore` command** (#613). Every real
  self-hosted install (curl-install via `setup-opentranscribe.sh`, or an existing install kept
  current with `opentranscribe.sh update-full`) had **no way to run backup or restore at all**:
  `opentr.sh` is deliberately not in `release-manifest.txt` (its bare `docker compose` calls
  with no `-f` chain only work in a repo clone — MEASURED that the base compose file alone is
  an invalid project outside one), and `opentranscribe.sh`, the script that actually ships, had
  no `backup)`/`restore)` case at all. So the #599/#600/#610 restore-safety hardening landed
  this cycle never reached a real deployment, and `opentranscribe.sh:572`'s own #610 rollback
  preflight told operators to run `./opentr.sh restore <backup>` — a file they do not have.
  Fixed by promoting `backup_database`/`restore_database` out of `opentr.sh` into
  `scripts/common.sh` (already shipped, `release-manifest.txt:52`), parameterized by a leading
  compose-files chain and front-end name, and wiring `backup)`/`restore)` into both front ends
  — one implementation of the `DROP DATABASE` path, not two that could silently diverge.
  `opentranscribe.sh`'s arm wraps the call in `set +e`/`set -e`: the shared code is written for
  `opentr.sh`'s deliberate absence of `set -e` and has several unchecked `docker compose ...`
  statements that would otherwise abort the whole restore mid-flight under `opentranscribe.sh`'s
  own `set -e`. It also explicitly reads `POSTGRES_USER`/`POSTGRES_DB`/`BACKUP_HOST_PATH` from
  `.env` before calling in — `opentranscribe.sh` has no `set -a; source .env` prologue, so
  without this a restore would silently target the default database name on any install that
  customised it. Also fixed: `restore_database` never created `./backups` (a copied-in dump
  restored onto a fresh install failed closed with a message blaming `pg_dump`), and
  `scripts/release-tests/test-upgrade.sh`'s phase 06b/15 now stage `opentranscribe.sh` + base
  **and** prod compose (previously staged `opentr.sh` + base-only, which was itself an invalid
  compose project — the exact defect this release blocks on, unnoticed through a full rehearsal
  cycle because the postgres container it `exec`'d into was already running from an earlier,
  correctly-chained `up`). `docker-compose.backup.yml` (the overlay the in-app scheduled/S3
  backup feature needs mounted) is a separate, not-yet-shipped gap — tracked as its own
  follow-up rather than folded into this fix.
- **PKI testing now generates its own isolated env fragment and never touches `.env`.**
  `scripts/pki/generate-test-env.sh` emits two gitignored artifacts (`pki-test.env`,
  `pki-test.compose.yml`) that a new `add_pki_overlay()` in `opentr.sh` sources after `.env`,
  replacing two ~25-line `--with-pki` blocks that were duplicated verbatim in `start_app()` and
  `reset()`. Verified end to end against a pristine `cp .env.example .env` clone:
  `RUN_PKI_E2E=true pytest backend/tests/e2e/test_pki.py` passes 14/14 with a continuous
  sha256+mtime poll recording **zero writes to `.env`** across the whole run, and a paired red
  check (blanking the fragment reproduces the predicted 400 "PKI authentication is not enabled")
  confirms the wiring is load-bearing, not just present. Six pre-existing PKI bugs surfaced and
  were fixed along the way: `PKI_ADMIN_DNS` was documented as comma-separated when the parser
  requires semicolons (a DN's own commas made comma-separated silently grant zero admins);
  `setup-test-pki.sh` told operators to configure `admin@example.com`, the exact cert #593
  stopped using because it collides with the seeded dev super_admin; `scripts/pki/start-pki-
  prod.sh` (superseded by `opentr.sh start prod --build --with-pki`) is deleted; `--with-pki` in
  dev mode was outright refused despite `docker-compose.pki-dev.yml` already existing to serve
  it, which in turn uncovered a reference to a nonexistent `backend/Dockerfile.dev` and a
  `0600`-permission nginx key unreadable by the frontend's non-root user; `--with-pki`'s
  `--fresh` port-isolation exemption was factually wrong (it does publish ports) and is now
  properly isolated, with a new test asserting every exemption's stated reason is actually true
  of its overlay, not just present; and `env_file: .env` was **mandatory** at 19 sites across
  three compose files, so a fresh worktree with no `.env` at all — the actual root cause of
  #593's original blocker — couldn't even reach `docker compose config`.
- **Watch sources: per-file management, reachable at last** (#489). Each source card gets a
  **Files** button opening its full import history — what was imported, skipped, or failed, with
  the actual reason rather than a count. Server-side status filter and filename search (so it
  works on a source tracking thousands of files), pagination, and per-row or bulk **Retry** and
  **Delete record**. Retry is the only way to bring back a *skipped* file: a skipped record is
  terminal, so fixing the underlying problem alone would never re-import it. It is honest about
  what it does — the row moves to **Pending** and a scan is requested, which may queue behind a
  running scan or fall beyond `max_imports_per_scan`, so the copy says "queued" and the list
  refreshes itself when a scan lands rather than claiming success. Retry is not offered on files
  the API would refuse (already imported, in flight, or a part folded into a stitched recording).
- **Watch sources: email notifications can be attached to a specific source** (#490). "Notify me
  only when *this* source fails" was modelled in the backend and unreachable from the UI. A
  **Notifications** panel on each source card lets the source **owner** — not just a super admin,
  since holding a mailer's credentials and subscribing your own source to one are different
  rights — attach configurations with per-link notify-on-success / notify-on-error flags and
  extra recipients. It warns when a link is configured but would send nothing: both flags off, a
  disabled configuration, or no recipients on either side. Deleting a configuration now shows how
  many sources it would stop notifying.

- **Multilingual search and chat is a one-click switch, and it is measured** (#453). The
  Settings → Search picker now badges each embedding model *Multilingual* / *English only*,
  shows whether it is downloaded, and offers **Download & deploy** for one that is not — a
  non-default model previously required two hand-built API calls that 404ed for every model
  anyway. Chat derives its language support from the configured model instead of a hardcoded
  English-only constant, the English cross-encoder reranker is skipped on predominantly
  non-English content rather than reordering text it cannot read, answers follow the
  question's language (quotes stay in their original language), the rewritten retrieval
  query is never translated, search highlighting stems in each document's own language, and
  the library's languages are offered as a search filter. Measured on 206 human-judged
  Spanish MIRACL queries: nDCG@10 **0.7618** with the multilingual model vs 0.6570 with the
  English default (+16 % relative), with committed baselines under
  `backend/tests/eval/baselines/miracl-es-*`. The default model **stays English** — the
  multilingual model costs ~6.5× the ingest embedding throughput and its effect on English
  corpora is unmeasured — but it runs on the same 1 GB heap floor as the default, so
  enabling it needs no resource change.

- **FedRAMP AC-2 account-inactivity expiration is now enforced, not just documented** (#567).
  `ACCOUNT_EXPIRATION_ENABLED` / `ACCOUNT_INACTIVE_DAYS` (off by default) previously existed
  only as unread `Settings` fields. A daily Celery sweep now deactivates accounts whose
  `last_login_at` is older than the threshold, audit-logs each deactivation
  (`AUTH_ACCOUNT_EXPIRED`), never touches an account that has never logged in (`NULL` stays
  exempt), and refuses to leave zero active `super_admin` accounts.

- **A native diarization engine is now the default, replacing PyAnnote for the on-box pipeline**
  (#538). A from-scratch `diar-server` sidecar (`docker-compose.diar-native.yml`, wired through
  `./opentr.sh start dev --with-diar-native`) now runs behind `DIARIZER_ENGINE`, selectable
  independently of the ASR engine, and `local_provider.py`/`factory.py` consolidate diarization
  onto one seam regardless of which backend serves it. The sidecar also classifies speaker
  gender while it already holds the decoded audio, so the enrichment tail's separate ~87–90s CPU
  wav2vec2 pass is skipped whenever `DIAR_NATIVE_GENDER` is on — verified end to end producing
  the same labels as the CPU path at higher confidence (male 0.999/female 0.989 vs. 0.999/0.593).

- **Chat gained a live query-execution trace panel** (#514). Every chat turn can stream its
  retrieval pipeline stages — routing, planning, legs, reranking, synthesis — live over SSE as
  they happen, rendered as a paced, collapsible tree rather than only after the answer lands.
  It is diagnostic only — never stored, never changes the answer — and exists specifically so a
  user can tell whether an answer came from the transcript they expected or from unrelated
  material that happened to rank.

- **Corpus-scale RAG chat: digests, map-reduce, and a query planner replace the single-pass
  retrieval-only pipeline for large libraries** (#403). The retrieval-only design silently gave
  wrong or partial answers once a library grew past what a single retrieval pass could cover;
  this adds a genuine map-reduce leg over per-recording digests for "across many transcripts"
  questions, a rules-based query router (measured at 0.104% lookup leakage) that decides which
  leg(s) a question needs, a query planner that runs legs in parallel, recurrence detection so a
  recurring meeting series is treated as one entity across sessions, and per-speaker summary
  digests with rename propagation. Deterministic, LLM-free ingest artifacts (facts, an
  extractive digest, keyphrases) feed all of it. Supporting changes: a digest citation renders
  as a labeled **summary**, never as something someone actually said; an answer built from zero
  excerpts is now flagged rather than presented as confident; recording date/time provenance is
  now tracked and surfaced; a selectable hybrid search fusion strategy (#363) was added as the
  underlying plumbing; and search gained a digest plane and `doc_type` discriminator. Output
  redaction (masking what the model *writes*, not just what it was given) also landed here.

- **The chat LLM's context window is now discovered from the provider instead of trusting a
  hardcoded 8192 default** (#533). A silently-truncating default previously capped every long
  transcript regardless of what the configured model actually supports; the app now probes and
  records the real value.

- **Reasoning ("thinking") support is now a measured per-model capability, not an assumed one**
  (#64). Whether a configured LLM exposes a separate reasoning/thinking phase used to be
  guessed; it is now determined and recorded per model, which is what lets the chat UI correctly
  show or hide the collapsible reasoning display and correctly separate thinking from the answer
  only when a request actually asked for it.

- **Chat and search now show an honest retrieval-quality notice** instead of presenting every
  answer with equal confidence. Surfaced when the underlying retrieval is weak (e.g. sparse
  corpus, low-confidence matches), telling the user the answer may be incomplete rather than
  letting a thin result set look as authoritative as a well-covered one.

- **GDPR compliance hardening: an erasure ledger, legal-hold re-erasure, and restore
  reconciliation** (#442). Erasure previously left no durable record that an Art. 17 request was
  ever made or fulfilled. A new erasure-ledger service now records that erasure was requested
  (deliberately with **no free-text column**, so the ledger itself can never become a copy of
  the PII it documents), a legal hold can be lifted and the file re-erased, and a reconciliation
  task finishes erasures that could not complete in one pass.

- **Audit events now cover resource sharing and group membership changes, and separate the
  acting user from the affected one** (#443). Collection sharing, tag sharing, and group
  membership changes previously emitted zero audit events. Five new event types
  (`RESOURCE_SHARE`/`RESOURCE_UNSHARE`, `GROUP_MEMBER_ADD`/`_REMOVE`/`_ROLE_CHANGE`) are now
  wired at all seven mutation points. Audit rows also now carry the actor (`user_id`) and the
  subject as distinct first-class target fields, rather than only recording who did something
  with the affected user buried in an unqueryable `details` blob.

- **Postgres now enforces a server-side backstop against transactions left open during slow
  work** (#440). 35 known "open a session, do slow non-DB work, commit later" leaks were already
  fixed in application code, with `scripts/audit-session-lifetime.py` guarding against
  regressions; this adds the missing layer underneath — `idle_in_transaction_session_timeout`,
  tunable via `DB_IDLE_IN_TRANSACTION_TIMEOUT_MS` (default 5 minutes, `0` disables it), which
  can only terminate a connection sitting idle inside an open transaction and never interrupts a
  legitimately slow running query.

- **The transcript view now renders as soon as the transcript is durable, not at pipeline
  completion.** Segments are committed and readable at ~78% progress, but the client previously
  waited for the full completion event plus an extra second before fetching. Progress keeps
  running after the transcript appears, and speaker labels attach in place afterward via the
  existing `speaker_updated` events.

- **A community-contributed Q&A panel extractor is now a selectable system summarization
  prompt** (#136). Produces a clickable index of question → answer → the timestamp range where
  the answer was given, useful for recurring panels that field audience-submitted questions.

- **Four new locales — Italian, Arabic, Korean, and Dutch — bring the UI to 12 supported
  languages.**

- **`./opentr.sh` gained `--gpu-device N` and `--no-bindmount`.** `--gpu-device N` retargets the
  running stack's GPU without hand-editing `.env`. `--no-bindmount` forces a
  measurement/benchmark stack to run fully baked container code instead of the dev bind-mount,
  so a benchmark run can't silently pick up an uncommitted local change.

### Fixed

- **`--with-pki` enabled unauthenticated admin impersonation from the LAN** (#620). The
  fixture's trusted-proxy default was `127.0.0.1/32,172.16.0.0/12,192.168.0.0/16`, written into
  the stack as **both** `RATE_LIMIT_TRUSTED_PROXIES` and `PKI_TRUSTED_PROXIES` — and those are
  not the same kind of setting. The second one decides whether a bare `X-Client-Cert-DN` header
  is believed *as an identity*: `pki_mode` defaults to `header`, so a DN with no certificate at
  all is accepted from any peer in the allowlist. `192.168.0.0/16` is the range ordinary
  consumer/office routers hand out, `docker-compose.yml` published the backend on the host's
  wildcard address, and the admin DN is not a secret (`setup-test-pki.sh` hardcodes it), so on
  such a LAN any other device could `POST /api/auth/pki/authenticate` with a forged DN header
  and receive admin tokens — no certificate, no password. Reproduced through the real
  `_extract_user_info_from_request` before the fix and refused after it. The rationale comment
  that justified the range was wrong twice over: it claimed the value only enabled
  `X-Forwarded-For` spoofing (it also gates identity) and that "production never loads it"
  (`opentr.sh`'s `add_pki_overlay()` generates and sources it for
  `./opentr.sh start prod --build --with-pki` too). Three independent closures, because none of
  them alone is sufficient: the allowlist is now **derived** from the compose project's own
  docker network (`docker network inspect ... .IPAM.Config`) instead of guessed, which keeps
  #615 fixed — that host's real subnet is `192.168.96.0/20` — while trusting one `/20` rather
  than the 4096 in the enclosing `/16`, and is safe by construction because docker's IPAM
  refuses a pool overlapping an existing host route; the backend's published port binds to
  **loopback** for a `--with-pki` stack via a new `BACKEND_BIND_HOST` (default `0.0.0.0`
  everywhere else, so no other deployment changes), so mTLS-terminating nginx is the only front
  door; and the PKI nginx configs now **clear** `X-Client-Cert`/`-Verify`/`-DN` on every
  backend-facing location except the one that actually terminated mTLS. That last one was the
  same bypass a layer up and survived any narrowing of the CIDR: the plain-HTTP `:8080` server
  had no `/api/auth/pki` block, so the request fell through to the generic `location /api/`,
  which forwarded a client-supplied DN header from a peer the allowlist trusts *by design*.
- **The release rehearsal's rollback phase could crash outright, and — once it stopped
  crashing — still fail for two further reasons** (#618). The same unguarded-command-under-
  `set -e` class of bug as #617, this time a bare `curl` against the frontend inside an
  assignment, which returns non-zero on a connection failure alone and killed the script with no
  trace; a new `ac_wait_for_frontend()` (mirroring `ac_wait_for_health`'s poll/timeout shape,
  non-fatal on timeout) now guards it. Once the crash was fixed, the check it guarded still
  failed: `docs-site/` was never staged into the rollback rehearsal tree, the same gap already
  fixed for the post-upgrade tree — with `pull_policy` forced to `never` and no local
  `docs-site/` to build from, `docker compose up` for the remaining services aborted the whole
  batch, not just docs, since the rollback scenario starts everything from a stopped app rather
  than swapping one running service. A third instance of the same `set -e` class was found
  during the real end-to-end verification run this fix enabled for the first time: phase 18's
  summary step piped `as_summary` (which deliberately returns 1 when any assertion failed)
  through `tee`, and under `pipefail` that non-zero return killed the script before it could
  print its own "Finished" line or exit cleanly. Verified end-to-end with a real
  `test-upgrade.sh --yes` run: `.phase/` reached `18.done` for the first time ever. Two
  remaining, unrelated async-write-timing races (the same class #617 partially fixed, affecting
  different tables) were found and deliberately deferred as issue #619, non-blocking — every
  actual product-level integrity check in that same run (login, file access, transcript content,
  search, new work after upgrade) passed.
- **A release rehearsal failure late in the run could silently truncate the last several phases
  with no error trace** (#617). `dbs_diff_fingerprints()` is documented as informational — a
  non-zero return means a table digest differs, already recorded — but both production call
  sites (the restore assertion and a later digest check) invoked it as a bare statement under
  `set -euo pipefail`. Any real digest mismatch tripped `set -e` and killed the whole script on
  the spot, silently truncating the rollback-serves-data and re-upgrade verification phases that
  should have run after it. Both call sites are now guarded with `if`/`then`/`fi`, the same
  pattern the rehearsal's own fault-injection selftest already used. The mismatch that originally
  surfaced this was itself a benign timing race: speaker gender-attribute detection fires as
  fire-and-forget the instant a file's status flips to completed, and the rollback-oracle backup
  was taken immediately after seeding returned, with no wait for that async task to settle — so
  the speaker table could gain attribute values between the "before" snapshot and a later
  comparison. A new `dbs_wait_for_speaker_attributes()` polls until every seeded file's speaker
  rows have settled before either backup artifact is taken.
- **Speaker display-name changes could silently drift out of sync with the search index at 8
  (then 9) different repair/propagation sites** (#605). The index writer resolves a speaker's
  label through `canonical_speaker_label()` (`display_name` → confident `suggested_name` @
  ≥0.75 → `name` → "Unknown Speaker"), but 8 repair/propagation sites still used an older,
  less-correct `display_name or name` rule, and 5 writers of `suggested_name`/`confidence`
  dispatched no chunk-plane propagation at all — so a suggestion crossing the 0.75 confidence
  threshold, or later changing, could leave the OpenSearch index holding a stale label with no
  recurrence guard. A new `canonical_speaker_label_for_row()` is now the single resolver every
  repair site imports, and the 5 previously-silent writers (speaker identification, the
  reject-suggestion path, the reprocess pipeline's speaker_llm stage, and both confidence tiers
  of the retroactive-matching loop — previously only the ≥0.75 tier dispatched) are wired
  through `SpeakerRenameTracker`. Rejecting a suggestion now also clears
  `suggested_name`/`suggestion_source`, not just `confidence` — a second bug where
  `was_auto_labeled` kept reading a rejected suggestion as still live. One already-drifted
  production speaker was found and repaired via a targeted, single-file reindex (not a global
  one) after the fix landed. A follow-up sweep found a 9th site the original pass missed
  (`_handle_update_profile_action`'s profile-wide rename still computed the old ad hoc chain)
  plus two more untracked writers with no tracker wiring at all —
  `SpeakerMatchingService.assign_speaker_to_profile` (confidence alone can cross the suggestion
  threshold with no `display_name` write) and the standalone `scripts/batch_speaker_matching.py`
  maintenance script — both now record before/after via the tracker.
- **A truly fresh `cp .env.example .env` install couldn't boot MinIO, and `--with-pki` could
  silently refuse a valid client certificate** (#614, #615). `MINIO_KMS_AUTO_ENCRYPTION=on` (the
  shipped default) requires `MINIO_KMS_SECRET_KEY` in `<key-name>:<base64-32-bytes>` form;
  `.env.example` shipped the placeholder `CHANGE_ME_auto_generated_on_install`, which isn't that
  format, so MinIO crash-looped (`FATAL Failed to connect to KMS: kms: invalid secret key
  format`) until an operator generated a real key by hand. A new `ensure_minio_kms_secret()`
  generates one on first run, called from both `start_app()` and `reset_and_init()`. Separately,
  `--with-pki`'s trusted-proxy CIDR default (`127.0.0.1/32,172.16.0.0/12`) didn't cover Docker's
  whole auto-assigned bridge range — once the default pools are exhausted by other concurrent
  Docker networks on a host, the daemon spills into `192.168.0.0/16` chunks, and the fail-closed
  trusted-proxy check silently refused a peer outside the allowlist (a valid client cert just
  landed back on `/login` with no error). Measured against the real dev stack's own network
  (`192.168.96.0/20`, already outside the old default on a host with ~34 unrelated Docker
  networks) rather than assumed. The first fix widened the default to a blanket
  `192.168.0.0/16`; that was itself a vulnerability and is superseded by the `#620` entry
  below, which derives the actual subnet instead. A same-night follow-up audit
  of this work found four more real gaps: `restore_database` returned exit 1 on a **successful**
  `--no-safety-dump` restore because its own last statement's exit status silently became its
  return code; the pre-`DROP DATABASE` service stop ran unchecked, unlike every other destructive
  step in the function; two concurrent restores had no lock against racing each other's replay
  (now a non-blocking `flock`, released via an INT/TERM/ERR/EXIT trap since most failure branches
  call `exit N` directly); and the newly-generated MinIO KMS key — which every object written
  under KMS auto-encryption depends on for decryption — printed no warning that losing it makes
  that data permanently unreadable, and had no guard against an ambiguous `.env` with two
  `MINIO_KMS_SECRET_KEY=` lines.
- **The release rehearsal's chat SSE assertions were silently skipped on every run, having
  misread the chat answer's own text as an error frame** (#611). Not a backend bug — the
  backend's SSE stream and error-frame emission were exonerated four independent ways. The bug
  was in the harness: `ac_chat_completion` printed three newline-separated records (answer /
  citation-count / error) and consumed them by line position (`sed -n '1p/2p/3p'`); the mock
  LLM's answer is multi-line markdown, so line 3 of the answer text was misread as an error code.
  Because the harness's error branch then fired on this false positive, it had been skipping two
  real assertions — chat summary non-empty, chat turn has at least one citation — on every
  single run; they had never actually executed. A new `parse-chat-sse.py` parses the stream into
  one line of JSON instead of positional `sed`, so answer content can never forge a record
  boundary. Verified live against an isolated `--fresh` deployment: single-line JSON, a 686-char
  real answer, 7 citations, and both previously-skipped assertions now execute and pass. The AST
  guard added to `test_chat_sse_contract.py` to prevent this class of defect recurring was itself
  found bypassable: it claimed to check "every `event: error` frame" but actually scanned a
  hardcoded two-file list, invisible to a third file, and only matched bare-name `sse(...)`
  calls, silently skipping `chat_service.sse(...)`-style attribute calls a refactor would
  naturally produce. It now walks every `app/**/*.py` file and matches both call forms — which
  immediately surfaced two real, previously-unscanned `sse("error", ...)` emitters with no
  `code` field at all (`api/endpoints/files/subtitles.py`, `api/endpoints/files/__init__.py`),
  now fixed with a literal code each.
- **Resolving every suggested speaker on a file could cause speaker identification to silently
  re-run on it** (#603 follow-up). `task_detection_service`'s "has LLM speaker ID already run on
  this file" check reads `Speaker.suggestion_source == "llm_analysis"` as its sole existence
  proxy. Both the reject-suggestion path and the #605 fix for `was_auto_labeled` reading a
  rejected suggestion as still live nulled `suggestion_source` alongside
  `suggested_name`/`confidence` — so a file where a user rejected (or manually confirmed) every
  suggested speaker read as never-identified, and speaker identification got re-offered and
  re-dispatched, regenerating the exact suggestions the user had just resolved (held off only by
  the ~30-minute cooldown, not actually prevented). Every reader of `suggestion_source` already
  requires `suggested_name`/`confidence` to also be truthy before treating a suggestion as
  active, so leaving `suggestion_source` set after accept/reject can't resurrect a live
  suggestion — it only preserves the historical record `task_detection_service` needs.
  `suggested_name` and `confidence` are still cleared, which is the part #605 actually needed.
- **Semantic search could miss the obvious file for a compound-concept query** (#606).
  `_build_text_query`'s fuzzy `multi_match` (fuzziness `AUTO`) applied unconditionally to
  multi-word queries — OpenSearch's fuzzy multi-term matching is an OR, not an AND, so the
  stemmed term "explor" (from "exploration") fuzzy-matched the unrelated stemmed term "export"
  (Levenshtein distance 2), and "space exploration" scored a false keyword hit on every "Export
  Controls Sync" chunk with zero support for "space" anywhere in that file. A second, independent
  defect surfaced once the first was fixed: OpenSearch 3.4's collapse + hybrid RRF
  (`score-ranker-processor`) returns a wrong, query-independent ranking whenever the keyword leg
  matches zero documents — confirmed live via two unrelated zero-keyword-hit queries producing
  byte-identical collapsed scores. The fuzzy clause is now single-word-only, and a cheap count
  pre-check routes a fully keyword-starved query to a neural-only collapse body instead. A
  follow-up audit found the multi-word decision itself was computed off a length-filtered word
  list (`len(w) >= 2`), so a query with one short token (e.g. "x exploration") was scored as
  single-word once the short token was dropped, silently reopening the exact false-positive class
  this fix closed — measured live: 7 hits before the fix (2 false positives) vs 5 after. Fixing
  that also deleted **all** typo tolerance for multi-word queries outright, despite the docs
  still advertising it; a second, additive fuzzy clause (`operator: and` + `fuzziness: AUTO`,
  requiring every term to fuzzily match something) restores it without reproducing the original
  single-lucky-neighbour bug — measured live: "exploratino sapce" (two typos) went from 0 hits to
  3.
- **`opentr.sh restore` could let a newer, already-running app silently re-migrate a
  just-restored older backup forward** (#610). `restore_database()` unconditionally restarted
  the app services it had stopped, on every code path — success, failed replay, and failed
  verify alike. In a rollback scenario the image that came back up was the newer one, and since
  the backend runs `alembic upgrade head` on every startup, it immediately re-migrated the
  just-restored older backup, destroying the entire point of the restore. The documented
  rollback recipe in `docs-site/docs/operations/upgrading.md` reproduced this verbatim — a
  shipped operational defect, not just a rehearsal artifact. A new pure decision function
  (`pg_restore_restart_decision`, fail-closed on any unknown or corrupt schema head) now governs
  whether to restart, and `opentranscribe.sh update --rollback` gained a companion preflight that
  reads the live Alembic head, asks the target image's own migration tree whether it recognizes
  that revision, and refuses (exit 1, overridable with `--force-downgrade`) on a miss.
  `--migrate-forward`/`--no-restart` flags make the choice explicit rather than automatic.
- **Flower's worker list could permanently omit a healthy worker that simply wasn't ready within
  Flower's first second of life** (#609). `/api/workers` is populated by a single `celery
  inspect` broadcast issued once, at Flower's own process startup, with a 1-second reply timeout,
  and Flower never re-inspects on its own — any worker not ready to answer in that window is gone
  from `/api/workers` forever, regardless of pool type (the `--pool=threads` hypothesis in the
  original issue was ruled out by direct probing). `gpu-scale-smoke.sh` and
  `bulk-processing-cheatsheet.sh`'s `bulk-workers()` now force `?refresh=1` with a bounded retry
  instead of trusting the cached snapshot, and `docker-compose.yml`'s flower command raises
  `--inspect_timeout` to 10000ms and drops two flags (`--queues=`, `--broker=`) that Flower's own
  argv filter silently discarded and that were actively misleading about what was configured.
- **A quick-win batch closed six small, independently-filed defects** (#604, #589, #593, #594,
  #607, #608). `gnupg` was missing from the backend's production Dockerfile, so
  `backup_service`'s `gpg --symmetric` step had no binary to call (#604).
  `_get_or_create_collection_with_dedup` inferred whether it had created a new collection from a
  cache-invalidation side effect that fired identically on both the real-create and the
  lost-race/collision path, so callers could not tell the two apart — it now returns
  `(collection, created: bool)` explicitly (#589). The PKI E2E test admin certificate's email
  matched the seeded dev super_admin, and `assert_email_link_permitted` unconditionally refuses
  any email-matched link onto an existing super_admin — a new `pkiadmin@example.com` test cert
  breaks the collision (#593). `UserASRSettings.base_url` was modeled but never wired into
  `GladiaProvider`; it now is, guarded by a new fail-closed `ASR_ALLOW_PRIVATE_ENDPOINTS`
  mirroring the LLM SSRF-guard pattern (#594). `UserLLMSettings.is_active` silently defaulted to
  `True` on every row instead of funneling through the single `_set_active_configuration` path on
  create/set-active/PUT/delete-promote (#607). And `full-test-matrix.md`'s vLLM GPU-utilization
  guidance for a 12 GB card was corrected from a claimed-passing `<=0.45` (which doesn't fit at
  any tested setting, per the issue's own measurements) to an honest NOT MEASURED, with a new
  `LLM_TEST_VLLM_EXTRA_ARGS` passthrough added as an unverified workaround path (#608).
- **The in-app scheduled/S3 backup feature's `pg_dump -Fc` output had no restore path at
  all** (#600, P0). `pg_restore` — the only tool that reads custom-format output — appeared
  nowhere in the repo; `./opentr.sh restore` sniffed the file's `PGDMP` magic bytes and
  printed a hint command (`docker compose exec -T postgres pg_restore -U $db_user -d
  $db_name - < $backup_file`) that was itself broken (wrong redirect placement) and, fixed
  naively, reproduces #599's exact silent-corruption bug: drifted data survives, and
  `alembic_version` ends with two conflicting rows — `pg_restore` exits 1 there (unlike
  `psql`'s 0), but only after already committing the partial damage, so the nonzero exit is
  not the safety net it looks like. Two of the operations guide's own documented restore
  recipes had the same defect. `./opentr.sh restore` now dispatches on the magic bytes into
  a real `pg_restore --exit-on-error --single-transaction --no-owner --no-privileges` replay
  branch, reusing #599's confirm / mandatory safety-dump / drop-recreate / verify sequence
  unchanged — one user-facing command, two internal replay backends. The safe path
  deliberately does **not** pass `-j`/`--jobs`: measured, it is mutually exclusive with
  `--single-transaction`, so parallel restore is a documented, accepted cost of the
  atomicity guarantee (an operator who needs it can still run `pg_restore -j` by hand). The
  verifier's expected-table-count now reads the archive's own `pg_restore --list` TOC,
  filtered on the type field (`$4 == "TABLE" && $5 != "DATA"`) rather than the naive
  `grep -c ' TABLE '`, which overcounts by matching `TABLE DATA` entries too (measured: 4 vs
  the correct 2 on a two-table archive) — a verifier built on that filter would fail every
  correct restore. For an S3-destination backup, whose local artifact is deleted right after
  upload, `./opentr.sh restore --from-s3 <name>` fetches it first via a new
  `python -m app.scripts.fetch_backup` (run inside the backend container, the only place the
  AES-256-GCM-encrypted S3 credentials can be decrypted) — fetching after the database is
  dropped would leave an operator holding an unreachable bucket and no way to authenticate
  to it, so the fetch is ordered strictly before anything destructive and the fetched
  artifact's size and magic bytes are verified before it is trusted. Proven end to end by a
  new integration suite that runs the real `run_pg_dump` inside a container built from
  `opentranscribe-backend:latest` against a throwaway, network-isolated Postgres — not a
  hand-fabricated `-Fc` file — plus a fast static suite pinning `pg_dump --format=custom` so
  a future format change fails loudly in the unit suite instead of only in an integration
  test somebody skipped.
- **`./opentr.sh restore` silently failed to restore data into a populated database and
  reported success anyway — and left `alembic_version` with two conflicting rows** (#599,
  P0). A plain `pg_dump` file carries no `DROP`/`--clean` statements, so replaying it into
  an already-populated database made every statement fail; without `ON_ERROR_STOP`, `psql`
  exited 0 regardless. Worse, the backup's `alembic_version` row did not collide on
  primary key with a drifted row already present, so it inserted successfully while every
  data-table `COPY` failed — leaving two rows in a table Alembic requires exactly one from,
  un-migratable without manual repair. `restore` now guarantees an exact restore by
  dropping and recreating the database (`DROP DATABASE ... WITH (FORCE)`, PG13+) and
  replaying the dump inside a single transaction, so a failure rolls back to nothing rather
  than a hybrid schema. It also: takes a mandatory pre-restore safety dump (fails closed if
  that dump itself fails), requires typing the database name to confirm (not `y`/`n` — a
  reflexive `y` was judged far more likely for a command whose name sounds recuperative),
  verifies the restored row/table counts and `alembic_version` before ever printing success,
  and now reads `POSTGRES_USER`/`POSTGRES_DB` from `.env` instead of hardcoding
  `postgres`/`opentranscribe` (a non-default name previously meant `backup`/`restore` could
  silently target the wrong database). `--yes` and `--no-safety-dump` support scripted use.
- **A watch source importing the same recording twice under two names** (#489). Content dedup
  filtered out the source being scanned, so a folder holding `meeting.mp4` and a renamed copy
  imported both and reported each as fresh. The `duplicate_same_source` skip reason existed in
  the schema and was produced by no code path at all.
- **An incomplete multi-part recording could be stitched early and transcribed as if whole**
  (#489). `retry_count` doubles as the multipart wait-scan counter, but failed standalone
  imports increment the same column — so a part that had failed twice entered the group already
  "aged" and, at the default wait of three scans, tripped the stitch on its very first grouping
  scan. The result was a silently truncated recording. The counter now resets on entry into the
  wait, so an established wait still ages and the missing-parts timeout still fires.
- **A deduplicated upload was reported as a failed upload** (#489). `POST /files` answers a
  content match with a structured 409 naming the file you already have, precisely so the UI can
  say so; nothing in the frontend consumed it, so a correct refusal surfaced as an upload error.
- **Watch-source email notifications could be dropped with no trace** (#490). A disabled
  configuration, or a link resolving to no recipients, was skipped silently — an admin who never
  received mail had nothing to diagnose. Both now log a warning naming the configuration.
  `additional_recipients` was also unvalidated free text, so a typo'd address was accepted and
  quietly discarded at send time; it is now rejected on save.

- **A model switch's re-embed could be silently skipped, leaving search ranking two
  incomparable vector spaces against each other** (#453, the #437 failure class). The reindex
  coordinator leaked its per-user lock on two early-return paths, so a reindex dispatched
  within the following hour answered "already running" while the switch reported success —
  measured live: the mismatched state costs 42 % of nDCG@10 versus the clean configuration.
  Also fixed on the same path: the model register/deploy/undeploy endpoints 404ed for every
  model in the registry (the path parameter never matched the `/` every model name contains),
  and the name→id lookup could return an ML Commons model *chunk* id, which made deploy fail
  with HTTP 500 and the switch refuse with 409 forever. The RAG evaluation harness's settle
  check also hung indefinitely on corpora past ~2,000 files (approximate distinct-count
  undercounting by one, forever); it now counts exactly.

- **Chat answers no longer leak prompt-internal block vocabulary** (#536). The base system
  rules that explain the `<counted>`, `<overview>`, `<recurrence>`, and `<synthesis>` evidence
  blocks were present on every turn, even when no such block was — so a model could narrate
  "there is no `<recurrence>` block" to a user who was never supposed to see those names.
  Block-specific rules are now included exactly when their block survives into the turn's
  prompt, including the case where the token budget trims a block away mid-assembly.

- **`.env.example` no longer pins new installs away from the native diarization engine.** The
  template still said `ENGINE_DIARIZER_BACKEND=pyannote  # only option in v1` after `native`
  became the coded default, so any `.env` copied from it silently overrode the default engine.

- **`directory_sync` (the periodic LDAP reconciliation/deprovisioning sweep) now has an admin
  settings API and UI panel.** Every sibling scheduled-config subsystem (backup, media mirror, ASR, LLM,
  engine, redaction) already had one; this sweep did not, so `directory_sync.enabled` stayed at
  its coded `False` default in every real deployment unless an operator wrote directly to the
  `system_settings` table. `GET/PUT /api/admin/directory-sync`, `GET .../status`, and
  `POST .../run` (super_admin only) mirror `backup_settings.py`'s pattern, with a new
  "Directory sync" tab under Settings → Authentication (alongside "Group mappings", for the
  same reason: this sweep also reconciles group membership and privilege, not just account
  status).

### Changed

- **Chat retrieval defaults: `final_chunks` 12 → 40, `max_chunks_per_file` 4 → 12** (#531).
  Measured on two corpora (AMI-81 and ELITR-Bench) against a calibrated answer judge
  (Cohen's κ 0.857): ~1.8–2× the answer-content recall of the old defaults, with negative
  controls (absent topics/speakers correctly refused) intact on every arm and median chat
  latency +13% (~49 s → ~56 s locally). On metered LLM providers the larger excerpt budget
  means proportionally more input tokens per turn; both values remain admin-tunable
  (Settings → Chat) and user preferences can still narrow them. `candidate_pool` (48) and
  reranking (on) are unchanged — a rerank on/off A/B on the current build measured a wash,
  and widening the pool measurably hurt.
- **`DELETE /api/tags/cleanup` now defaults to the caller's own tags.** It previously always
  swept every account's unreferenced tags, while its inspection sibling `GET /api/tags/unused`
  is caller-scoped — so an admin who read the list and then ran cleanup irreversibly deleted
  rows they were never shown. The deployment-wide sweep is still available but must now be
  named *and* acknowledged: `?scope=all_users&confirm=true`, the same double opt-in as
  `POST /api/org-admin/gdpr/erase-organization`. `scope=all_users` without `confirm` is a 400.
  The response gained a `scope` field; `deleted_count` and `message` are unchanged.
  **Breaking for any script or runbook that relied on the wide default** — add
  `?scope=all_users&confirm=true` to preserve the old behaviour. There is no frontend caller.

- **Docs site upgraded to Docusaurus 3.10.2 (#423).** All six `@docusaurus/*` packages are now
  pinned to the same exact version — `@docusaurus/theme-mermaid` was the only one declared with
  a caret, and Docusaurus refuses to build when an official package drifts away from
  `@docusaurus/core`. `@docusaurus/faster` is a separate package as of 3.10 and is required by
  `future.v4: true`, so it is now a declared dependency. The content migration that had blocked
  this: twelve blog posts used `<!-- truncate -->` and seven headings used the CommonMark
  `{#explicit-id}` anchor, both of which MDX rejects; they are now `{/* truncate */}` and
  `{/* #explicit-id */}`, which Docusaurus 3.10's heading plugin reads as the same explicit IDs,
  so every inbound anchor link still resolves (`onBrokenAnchors: 'throw'` proves it at build
  time). `docs-site/README.md` documents the three MDX-only spellings so they are not
  reintroduced.

### Fixed

- **security:** Gladia's `result_url` — a value the vendor's own API returns in its response and
  is then polled up to 720 times over ~2 hours, with the user's API key attached — was fetched
  unconditionally, with no SSRF validation and no redirect protection. The existing SSRF guard
  (#594) validated only the configured `base_url`, once, at construction; a self-hosted/private
  `base_url` (`ASR_ALLOW_PRIVATE_ENDPOINTS=true`) or a merely misbehaving server could point
  `result_url` anywhere, leaking the API key and reaching internal services or cloud metadata
  endpoints, and none of the outbound calls passed `allow_redirects=False` either, so even a URL
  that passed validation could redirect to an internal target after the check. Mirrors the
  pattern `llm_service.py` already uses for the identical bug class (#444): every outbound call
  now goes through `resolve_pinned_target()` plus a pinned session and `allow_redirects=False`,
  with `result_url` validated immediately after being read from the job-creation response —
  before the poll loop starts — so a blocked URL now raises immediately with a clear reason
  instead of retrying up to 720 times into a generic timeout.
- **Search documentation named environment variables and embedding models that do not exist.**
  `configuration/environment-variables.md` told operators to set `NEURAL_SEARCH_ENABLED`,
  `OPENSEARCH_ML_COMMONS_ENABLED`, `OPENSEARCH_URL`, `OPENSEARCH_USERNAME`,
  `NEURAL_SEARCH_MODEL_ID` and `NEURAL_SEARCH_BATCH_SIZE` — none of which the backend reads;
  the real names are `OPENSEARCH_NEURAL_SEARCH_ENABLED`, `OPENSEARCH_HOST`/`OPENSEARCH_PORT`,
  `OPENSEARCH_USER` and `OPENSEARCH_NEURAL_MODEL`. `configuration/neural-search-setup.md`
  offered `bge-large-en-v1.5` as one of three selectable models; it is not in any registry and
  the real list is the seven verified models. Both files also sized the embedding model in
  **VRAM** — it runs on CPU inside the OpenSearch JVM and never touches the GPU, so the budget
  is heap. The JVM heap default was documented as 1 GB in three places (`operations/performance-tuning.md`
  contradicted itself in two sections) when `docker-compose.yml` has set 4 GB with
  `bootstrap.memory_lock` since the measured heap work. `user-guide/admin-panel.md` gained the
  `all-MiniLM-L12-v2` row that shipped in the registry without a docs update, and
  `docs-site/README.md` no longer points at the pre-transfer `davidamacey.github.io` URL.
- **`backend/README.md`'s production environment block named five variables the backend does
  not read**: `SECRET_KEY` (it is `JWT_SECRET_KEY`), `MINIO_ENDPOINT`/`MINIO_ACCESS_KEY`/
  `MINIO_SECRET_KEY` (they are `MINIO_HOST`/`MINIO_PORT`/`MINIO_ROOT_USER`/
  `MINIO_ROOT_PASSWORD`) and `OPENSEARCH_URL` (it is `OPENSEARCH_HOST`/`OPENSEARCH_PORT`).
  Following it produced a deployment that silently kept every default, including the default
  JWT secret and `minioadmin`/`minioadmin`. The block now defers to `.env.example`.
- **The v0.3.3 blog post was dated a year early** (`2025-01-13`), placing it below the
  v0.1.0 announcement on the blog index as though 0.3.3 had shipped first; the `v0.3.3` tag
  is dated 2026-01-14. The post's explicit `slug:` means its URL is unchanged.
- **An unreachable Redis made every cached request pay retry sleeps.** The cache service never
  remembered a failed connection — it re-dialled on every call, and each attempt paid redis-py's
  default exponential backoff. So with Redis down, a degraded cache presented as a dead API:
  tag lists, file listings and status summaries each slept through several retries. Now a failed
  attempt opens a 30-second circuit and the client is built with retries disabled, since the
  cooldown is the retry policy. Measured on one request path: 75.2s → 0.16s.
- **The task progress bar was frozen at 50%.** `GET /api/tasks` and `GET /api/tasks/{task_id}`
  synthesized their response from the media file's status instead of reading the `task` table,
  so `progress` was hardcoded to `0.5` for every in-progress file and the Task Status bar never
  moved — making a running transcription indistinguishable from a wedged one. The pipeline had
  been recording real per-stage progress all along. Also fixed in the same response: the task id
  is now the real Celery id (so it can be passed to `POST /api/tasks/system/recover-task/{id}`,
  which previously could only 404), `task_type` reports the actual type instead of always
  `"transcription"`, and a failed file surfaces its real error instead of the literal
  `"Transcription failed"`.
- **Account-lifecycle controls were not enforced on ~100 endpoints.** `get_current_context` —
  the credential entry point for chat, tags, collections, comments, search, file upload and
  org-admin — depended on the credential layer rather than the account-lifecycle gate, so a
  deactivated, expired, unapproved or force-password-change account could still act. That
  included `POST /api/org-admin/gdpr/erase-organization`, an irreversible whole-tenant erasure.
  `endpoints/files/management.py` had the same gap on all 8 of its handlers, including
  `DELETE /api/files/{uuid}/force`.
- `GET /api/admin/stats` reported a hardcoded version `1.0.0` instead of the real build version,
  and its `gpu` field changed type from a list to a dict whenever stats collection failed.
- `POST /api/files/management/cleanup-orphaned` returned a `marked_orphaned` counter that was
  never incremented, so it reported `0` in every deployment. The field is gone and the endpoint
  now describes what it actually does (bulk stuck-file recovery). Real orphan cleanup is
  `POST /api/admin/data-integrity`.
- **`POST /api/files/{uuid}/retry-summary` was broken end-to-end — every valid retry request
  returned 500.** Two stacked bugs: the endpoint passed the file's internal integer primary key
  instead of its UUID into the retry helper, which looks the file up by UUID and always failed
  the lookup; and separately, the retry helper called `asyncio.run()` internally while already
  running inside this endpoint's own async event loop, which always raises
  `RuntimeError: asyncio.run() cannot be called from a running event loop`. No test exercised
  past the "LLM not available" branch, which is why both went unnoticed. Also fixed in the same
  code path: a failed dispatch (e.g. a broker outage) used to destroy the file's previous summary
  before finding out whether a new one could actually be queued — a retry that failed left users
  with no summary at all instead of the one they started with. It now restores the prior summary
  if dispatch fails.
- A SCIM `PATCH` with a bare `{"op": "remove", "path": "members"}` (no value) — the shape Okta
  and Entra send to empty a group — silently did nothing instead of clearing membership.
- Every SCIM-driven audit event (account creation, updates, and deactivation via Entra/Okta
  provisioning) recorded the affected user only by username, never by their stable numeric ID —
  unlike every other administrative audit emitter in the app. This broke "everything done to
  this account" audit-log queries keyed by user ID for any SCIM-managed account.
- The Tasks page could crash rendering a task whose timestamps arrived as ISO strings rather
  than native datetimes.
- A background drift-repair job that keeps speaker profile assignments in sync with OpenSearch
  could report full success ("N updated, 0 errors") while every write silently failed against a
  missing document — a dead exception handler could never observe the real outcome.
- The YouTube download quota could report `-1` remaining (the app's own sentinel for
  "unlimited") for a user who was actually over their hourly or daily limit, once the count
  reached or exceeded it.
- An admin-facing migration progress tracker could silently corrupt its list of failed files
  into an empty object on any progress update recorded before the first failure, breaking the
  admin UI's error list for that migration run until a failure was recorded.
- **security:** A search-result snippet's redaction masking had no path for the `custom` word
  category at all — custom words were matched only per highlight fragment, so a configured
  custom redaction word split across a `<mark>` tag boundary (e.g. a highlighted partial match)
  never appeared intact in either fragment and leaked verbatim into the search preview.
- **security:** A watch source's local-upload path-traversal guard fell back to comparing the
  watch root against itself whenever the destination's parent directory didn't exist yet —
  trivially true regardless of where the destination actually pointed — allowing a crafted
  remote path with a not-yet-existing nested parent to write an arbitrary file outside the
  configured watch root.
- OpenSearch speaker-embedding writes silently dropped on any real connection blip: the
  transient-error retry checked the write exception against Python's builtin `ConnectionError`,
  which the OpenSearch client's own `ConnectionError` does not subclass, so the retry never
  actually fired.
- Merging two speakers unconditionally reset the surviving speaker's OpenSearch
  `collection_ids` to empty, silently removing it from every collection-scoped voiceprint
  search it belonged to on every merge (Postgres collection membership was unaffected — the
  break was OpenSearch-only and invisible outside collection search).
- Three scheduled Celery beat tasks (database backup, media mirror, and LDAP directory sync)
  committed their "this window is claimed" timestamp *before* dispatching the actual job. A
  broker hiccup at that exact moment silently skipped the whole scheduled run — up to 24 hours
  for a daily backup, or a full day's LDAP deprovisioning sweep — with nothing surfaced to the
  admin UI.
- A batch speaker-migration orchestrator only persisted its list of dispatched Celery batch IDs
  once, after every batch had been queued. A failure partway through the dispatch loop left the
  already-queued batches unrecorded, so the migration could never be marked stopped and its
  "Stop" control could no longer revoke the batches still running in the background.
- The CPU-only (lightweight) transcription path could mark a file as permanently failed and
  notify the user of an error *before* Celery's own automatic retry had a chance to run, on a
  transient MinIO or network error that the task was specifically configured to retry.
- Two concurrently-processing files whose diarization produced the same speaker label for the
  same file (a documented risk under Celery's at-least-once delivery) could crash the whole
  transcription/rediarization task instead of gracefully reusing the already-created speaker
  row, because the fallback path re-attempted an insert without first rolling back the failed
  transaction.
- The legacy (pre-3-stage) transcription task ignored a user's configured diarization source
  (e.g. "off" or "pyannote") when routed through a cloud ASR provider, always defaulting to the
  provider's own diarization regardless of what was configured.
- Three `db_helpers` query functions (`safe_get_by_id`, `get_file_tags`, `get_user_file_stats`)
  caught a database error and returned a fallback value without rolling back the failed
  transaction, leaving every later query on that same session failing with "current transaction
  is aborted" until something further up the call stack happened to roll back.
- A pipeline-timing duration calculation treated an epoch-millisecond value of exactly `0` as
  "marker absent" instead of "a real timestamp of zero," silently dropping the computed
  duration (not reachable with today's always-nonzero timestamps, but a latent correctness gap).
- A benchmark comparison task crashed on a truncated or corrupted snapshot file instead of
  reporting the same graceful error every other failure path in that module uses.
- Two dead OpenSearch helper functions (`bulk_add_speaker_embeddings`, `cleanup_orphaned_
  embeddings`) and one dead Celery-task helper (`get_failed_summary_count`) — all fully tested
  but with zero production callers, one an explicitly-documented unimplemented stub — were
  removed.
- A segment spanning two detected overlap regions was assigned to whichever region happened to
  be processed last, discarding whichever assignment had higher confidence — an
  order-dependent, non-deterministic result for the same input. Overlap-group assignment now
  deterministically keeps the highest-confidence match per segment.
- `speaker_processor.py` carried its own `normalize_speaker_label`, a duplicate of the canonical
  implementation every ASR/diarization provider already normalizes through and strictly less
  correct: no zero-padding for single-digit labels (`"1"` stayed `"SPEAKER_1"`), no recognition
  of provider-specific formats (`"S1"`, `"spk_0"`, `"Guest-1"`, …), and unrecognized text was
  blindly string-prefixed (`"host"` → `"SPEAKER_host"`) instead of hashed into a valid
  `SPEAKER_XX` form. It now delegates to the canonical implementation.
- **Confirming "Unlock Account" in User Management showed a button labeled "Delete."**
  `unlockAccount()`'s confirmation call omitted the confirm-button label, so it fell through to
  `UserManagementTable`'s own default (`common.delete`) — the label every other row action
  (lock, force logout, MFA reset) supplies explicitly. Unlocking clears only a failed-login
  counter and deletes nothing.
- **Logging out mid-request could leak the previous user's protected-media credentials into
  the next session.** `configService`'s protected-media-auth fetch had no way to discard a
  response that resolved after `resetProtectedMediaAuthConfig()` ran on logout — a stale
  response landed in the fresh cache regardless. A generation counter now discards any fetch
  started before the most recent reset.
- **Extracting audio from a large video silently skipped the resumable-upload path.**
  `uploadExtractedAudio()` had no multipart branch, unlike `uploadFile()` — large extracted
  audio always went through the legacy single-request path multipart uploads exist to avoid.
- **GPS coordinates from real camera/phone metadata were garbled.** The ISO 6709 location
  parser split on a regex that didn't treat `-` as a valid non-delimiter character, so a
  real-world string like `+40.6894-074.0447+002.000/` mis-parsed into the wrong latitude and
  longitude.
- **Search result pages 7 and 8 (of 20+) rendered a nonsensical `5 … 6` in the pager.** The
  ellipsis-insertion check compared the raw current page against a threshold instead of the
  actual clamped window start, so it inserted a "gap" marker between two adjacent page numbers.
- **Deselecting the last file in the gallery left the UI stuck in selection mode**, and a
  file removed from the list stayed counted in the selection forever. `toggleFileSelection`
  now derives `isSelecting` from the selection size, and `setFiles()` prunes selections against
  the incoming file list.
- **Cancelling an upload didn't stop its pending auto-retry timer**, which could later
  resurrect an upload the user had explicitly cancelled. `retryUpload()` now checks the
  upload's status before proceeding.
- Two identical-looking calls to reload AI suggestions after a cache-invalidation event
  handled a rejected promise differently — one caught, one not. Both now handle it the same way.
- A zero-denominator video frame rate (`0/0`) produced `Infinity`, which downstream code
  treated as a valid, truthy value. It's now treated as unset.
- Failing to create a new speaker while editing a segment only logged to the console, with no
  on-screen feedback — inconsistent with every other error path in the app.
- `removeUpload()`'s "is this upload still active" check omitted the `preparing` status,
  unlike every other such check in the same file.
- A failed audio extraction leaked its FFmpeg in-memory filesystem handles instead of cleaning
  them up, and a failed metadata read wasn't checked for a non-zero exit code before its output
  was used.
- Extremely large or sub-byte file sizes could render as `"1.2 undefined"` instead of a valid
  unit.
- An AI-suggested tag or collection with a genuine `0` confidence score was silently
  overwritten with the "unknown confidence" default of `0.5`, misrepresenting a real
  low-confidence signal as moderate.
- A failed dynamic import of the toast-notification module in the recording store was an
  unhandled promise rejection, not the "logged to console only" behavior the code's own comment
  claimed.
- `search.ts`'s `setQuery` was the only filter-mutating action that didn't reset the result
  page back to 1, unlike every sibling setter.
- The locale store's `initialize()` had no guard against being called twice, unlike its
  sibling `network.ts` store — a second call leaked a duplicate `languageChanged` listener.
- Concurrent callers of `llmService.getStatus()` with no cached value each fired their own
  request; whichever response arrived last could overwrite a newer cached value with stale
  data. Concurrent calls now share one in-flight request.
- `notifications.ts`'s `getNotifications()` threw a `TypeError` on every call due to a
  temporal-dead-zone bug in how it read the store's current value.
- The AI-summary panel rendered a blank area with no explanation in two reachable states:
  pending with no LLM configured, and failed with no retry available. Both now show an
  explanatory message instead of nothing.
- **The admin-tunable retry ceiling was bypassable through three routes.** It was enforced on
  the two `POST .../retry` endpoints but not on bulk retry, bulk reprocess, or the tasks
  router's direct retry route, so a file past the configured limit could still be resubmitted
  through those. All five entry points now share one ceiling check
  (`system_settings_service.retry_ceiling_message`). Separately, `max_retries=0` — documented
  everywhere as "unlimited retries" — was inverted: `retry_count < 0` is never true, so setting
  it actually blocked every retry immediately.
- **SSRF: two outbound fetches driven by untrusted input skipped the existing SSRF guard.**
  The LLM context-window probe dialed an admin-entered endpoint directly with `requests`/
  `aiohttp`, and the yt-dlp media importer fetched a thumbnail URL that comes from the
  extractor's metadata for the submitted page, not the page URL itself — both bypassed the
  `resolve_pinned_target`/pinned-session pattern already used for the primary URL. Both now go
  through it, refusing private/link-local targets before any request is made.
- **security:** A configured MediaCMS media-source hostname bypassed SSRF protection via DNS
  rebinding. The hostname validator never resolved DNS, so `169.254.169.254`, `127.0.0.1`,
  private IPs, and `metadata.google.internal` all passed as a valid host for any authenticated
  non-admin user. It now routes through the canonical `is_safe_url`, plus a defense-in-depth
  check at every outbound request. A separate defect in the same integration let its three
  outbound requests (login, media-info, download) follow an HTTP redirect after that validation
  passed, so a registered media source could 302 the real request to an internal target; it now
  uses the same `resolve_pinned_target` + pinned session + `allow_redirects=False` pattern the
  LLM service already used correctly.
- **security:** Two more SSRF gaps survived the original outbound-URL hardening. The guard
  validated a URL's resolved address but then let the actual outbound request follow redirects
  unpinned, so a public URL that redirected to `169.254.169.254` still reached cloud instance
  metadata; and RFC 6598 carrier-grade-NAT addresses (`100.64.0.0/10`) were classified as
  neither private nor global, so they passed the check either way. Outbound requests are now
  pinned end-to-end using the already-validated address, and the CGNAT range is now correctly
  treated as private.
- **security:** `allow_private=True` silently disabled the SSRF guard's cloud-metadata block
  entirely, rather than only widening the allowed address range as documented. Reachable at
  login time: OIDC discovery-document/JWKS fetching sets this flag, so an OIDC provider (or an
  admin's "Test connection") pointed at `169.254.169.254/latest/meta-data/` would dial instance
  metadata and wait out a 10-second timeout instead of being refused outright.
- **security:** A quarantined (DMCA/legal-hold) file's data kept leaking through surfaces the
  original quarantine work missed, even though the file itself 404s everywhere else:
  `GET /files/metadata-filters` and `/search/filters` facet aggregations both returned
  language/format/codec/date/size values drawn from quarantined files to any user (including,
  for search facets, the file's own owner); comments on a quarantined file were fully readable
  and editable; the speaker listing and cross-media-occurrences view leaked a quarantined file's
  speakers even to its own owner; and a tag whose only file was quarantined stayed visible. All
  six gaps are now closed through the app's existing `is_hidden_for`/`is_quarantined` pattern,
  with an explicit, default-excluded admin bypass on the two read-only facet endpoints.
- **security:** A `super_admin` account could be scoped down to only their own files when
  listing collection media, while a plain `admin` was not — `get_collection_media` hand-rolled a
  `role != "admin"` check instead of the canonical `User.is_admin` property six lines away.
- **security:** An admin lowering the configured retry ceiling to stop a runaway (e.g. metered
  cloud ASR) cost loop could still be silently ignored on the single-file retry route, and
  `reset_retry_count=true` bypassed the ceiling entirely for any file owner, not just admins — a
  second, separate gap from the three-more-routes retry-ceiling fix above. `POST
  /files/{uuid}/retry` read `MediaFile.max_retries`, a column nothing ever writes (always its
  ORM default of 3), instead of the admin-tunable system setting; both paths now route through
  the same ceiling check and require admin for a reset.
- **security:** Content redaction could silently disable itself for text in a language its
  matcher didn't recognize, while reporting a clean scan. A language-support check compared a
  raw language string (`"eng"`, `"English"`, `"en "`, …) verbatim against `{"en"}` and, on any
  mismatch, quietly dropped the PII/profanity/toxicity detectors rather than treating an
  unrecognized language as "run every detector" — so the coverage report subtracted the skip as
  legitimate, and an unresolvable language read as "covered, clean." The LLM detector had the
  same fail-open: it was credited as covered whenever enabled, even when the provider call
  failed and returned nothing. Two disagreeing `normalize_language` implementations (13 of 21
  test inputs differed) are unified into one function that never guesses a fallback language.
- **security:** A chat/summary/search read could show a person's name unmasked whenever the
  redaction model tagged it `ORGANIZATION` instead of `PERSON`. The default masked-entity list
  excluded `ORGANIZATION`, and every masking surface (transcript segments, search snippets, chat
  masking, summary masking) shares one detector and one default entity set. Measured against
  `en_core_web_sm`: a real surname like "Blackwell" scored `ORGANIZATION @ 0.85` — identical to
  an actual company name — and no confidence threshold can separate the two. `ORGANIZATION` is
  now masked by default.
- **security:** `GET /api/files/{uuid}/summary` returned the AI-generated summary completely
  unmasked (#465) — no redaction, no fail-closed branch — so a user whose policy masks PII in
  the transcript view could still see that same PII restated in the summary's own words. The
  admin redaction floor was bypassed identically. Summary masking now runs live and walks the
  summary's free-form JSON tree rather than assuming fixed field names.
- **security:** Chat egress masking used the wrong party's policy, and `blur` leaked plaintext
  outright. The design called for masking by the file *owner's* redaction policy; the shipped
  code used the *requester's* — letting a sharee with a permissive policy read PII the owner
  meant hidden. Egress masking is now "strictest wins": masked if either party's policy says to,
  resolved per file so one strict owner in a multi-owner chat scope doesn't over-mask everyone
  else's files. A related defect found only after fixing the first: the `blur` masking style
  leaked the original plaintext.
- **security:** FIPS 140-3 boot validation checked that secrets existed but not that they were
  the right algorithm or actually random. A FIPS-mode deployment with `ENCRYPTION_ALGORITHM_V3`
  set to anything other than the approved AES-256-GCM, or a padded/low-entropy `ENCRYPTION_KEY`,
  booted without complaint. Boot now validates the configured algorithm against an explicit
  allow-list and checks secret entropy before allowing FIPS mode to start. Three more FIPS
  defects fixed alongside it: JWT signing was documented as HS512 under FIPS but every real
  login path signed with the hardcoded HS256 (a dead code path, corrected in documentation, not
  a compliance violation since HMAC-SHA-256 is itself FIPS-approved); enabling FIPS mode
  silently invalidated every user's existing MFA backup codes; and an MD5 usage remained
  reachable under FIPS mode.
- **security:** The five PKI certificate-revocation settings (verify-revocation, soft-fail, CA
  cert path, OCSP timeout, CRL cache) were configurable in the admin UI and persisted to the
  database, but read by nothing (#498) — PKI auth read straight from `.env`, so an administrator
  hardening a deployment by disabling soft-fail in Settings saw no behavior change at all. All
  five now resolve through the same DB > `.env` > coded-default chain as every other setting,
  with a new cross-field rule refusing revocation verification enabled with no CA bundle
  configured.
- **security:** Comment edit and delete had no tenant check (#497) — the only two handlers in
  the comments module without the tenant-scoping gate every sibling handler applies, so a user
  could edit or delete comments on a file belonging to an organization they had since left,
  since authorship (unlike tenant membership) survives an org change.
- **security:** GDPR Article 17 erasure could report SUCCESS while transcript text and RAG
  chunks remained fully indexed and searchable. Every OpenSearch step in the erasure path was
  wrapped in a blanket exception-suppressor, so a transient OpenSearch outage during an erasure
  silently left the transcript document, chunks, and summaries in place while the response,
  audit log, and API all reported a completed erasure. Failures on this path are now recorded,
  matching the pattern voiceprint erasure already used.
- **security:** `DELETE /api/admin/users/{uuid}` performed an irreversible account/file/
  transcript deletion with no audit record at all, while its functionally identical twin
  `DELETE /api/users/{uuid}` audited the same deletion correctly (FedRAMP AU-2/AU-12, GDPR
  Art. 30(2)(d)). Now emits the same audit event as its twin. Separately, an install could be
  left with zero `super_admin` accounts through two unguarded routes: that same admin-delete
  route allowed deleting the last `super_admin` (already refused on its `/api/users/{uuid}`
  twin), and the GDPR user-erasure route carried neither a last-admin guard nor a self-erasure
  guard. Both routes now carry both guards.
- **security:** A group in one organization could gain a member from a different organization,
  and from there reach that organization's shared collections. `user_group` was the only
  user-owned table with no organization stamp, so nothing constrained group membership to one
  tenant, and adding a member resolved its target purely by UUID with no tenant check.
- **security:** Switching accounts in the same browser session (signing in as a different user
  without a full page reload) could serve the previous user's cached data to the next one.
  Several per-user caches — the tier-scoped feature-flag store, the stored-protected-media-
  credentials cache, and `apiCache`'s module-level cache (tag lists, file listings, status
  summaries, gallery/speaker/collection data, and prefetched file-detail payloads) — were
  "fetch once" latches whose only call site ran at initial app mount, which an SPA login
  transition never re-runs. `clearUserState.ts` now clears all of them on logout/login.
- A transient storage hiccup during upload completion could delete a just-uploaded file's
  database row and tell the user their upload failed, while the bytes sat safely in the bucket.
  The storage-existence check folded every failure (MinIO restart, network blip) into the same
  result a genuinely absent object returns. Only a confirmed absent object now returns that
  result; any other storage error returns a 503 and leaves the row `PENDING` for the existing
  idempotent retry. The content-hash dedup fingerprinting path had the identical bug. Separately,
  the background orphan-upload sweeper could delete an upload that was still genuinely in flight
  if a storage outage outlasted one 15-minute sweep window — it now confirms the object is truly
  absent before deleting anything.
- A scheduled OpenSearch cleanup sweep (running four times daily) could wipe an entire index if
  its Postgres reference query ever returned empty, treating "no valid IDs found" as "every
  document is an orphan," with no floor or ratio guard — the same failure shape behind this
  project's June 2026 data-loss incident. This sweep and four sibling scheduled sweeps now
  verify before deleting instead of deleting first.
- **Recordings over one hour displayed the wrong duration** (e.g. "125:00" instead of "2:05:00"
  for a 7500-second file) on gallery cards and other duration-derived views. The formatting
  helper never carried minutes into hours; a second, millisecond-precision copy of the same
  logic had the identical bug.
- **The browser's microphone indicator stayed lit after clicking "Stop Recording."** Hardware
  cleanup (mic tracks, `AudioContext`, the level-meter loop) was only reachable from the
  explicit "clear" action or a start-time error path, not from a normal stop.
- **Loading more results inside the in-transcript search modal could either spin forever or
  silently give up after one hiccup.** It now retries with the same jittered backoff the upload
  path already uses, and shows an error only once retries are exhausted.
- A FastAPI validation error's structured detail rendered as a generic, unhelpful message
  everywhere in the app — the shared error-message helper (206 call sites) silently dropped the
  422 array-detail shape FastAPI returns for validation failures.
- The client-side upload size limit could silently disagree with what the admin actually
  configured on the server; it now reads the value the backend exposes on
  `GET /api/system/capabilities` and no longer falls open to "unbounded" while that value hasn't
  loaded yet.
- Search-term highlighting inside the transcript, summary, and topics panels was illegible in
  dark mode — three components each defined their own conflicting highlight style with no
  dark-mode variant at all.
- A modal opened from within another modal, or on a narrow/mobile viewport, could render behind
  it instead of on top. No shared z-index scale existed; one is now the single source of truth.
- **Deleting a user account failed with a 500 for any account that had ever had a file
  transcribed** — effectively every real account. Speakers were bulk-deleted before the
  transcript segments referencing them, and that foreign key has no cascade, so the delete
  always failed with a constraint violation the generic exception handler reported as an
  unhelpful, unnamed error.
- A media download could hang for up to 15 minutes waiting on a server-sent-events stream that
  would never publish. The per-file "prepare in progress" guard was set before dispatching the
  prepare task, but nothing released it on completion.
- The admin audit log for auth-configuration changes could throw when the change's author was a
  since-deleted user — the ORM model still declared the author column non-nullable after a
  migration made it nullable in the database.
- Three `db_helpers`-adjacent chat query functions held a Postgres session open while waiting on
  OpenSearch or an LLM response (session-open wall time 1473–1672ms → 255–520ms; idle-in-
  transaction time 355–400ms → 0–5ms) — the same session-hold pattern that had already wedged
  the dev database twice in one day by queuing a migration behind an idle-in-transaction
  session.
- **Two unquoted `.env.example` values broke bash on a fresh install.**
  `OIDC_SCOPES=openid email profile` (unquoted spaces made every `opentr.sh` invocation try to
  run `email` as a shell command, printing `.env: line NNN: email: command not found`) and
  `LDAP_USER_SEARCH_FILTER=(sAMAccountName={username})` (unquoted parentheses are bash array
  syntax, silently turning the value into a one-element array instead of a string). Both are now
  quoted.

### Performance

- **On the native diarization engine, transcription and diarization now run concurrently
  instead of back-to-back** (`max(transcribe, diarize)` instead of the sum) — measured
  87.8s → 50.3s (43% faster) on a 66.5-minute test clip, byte-identical output verified.

## [0.5.0] - 2026-08-10

> **Planned release: `v0.5.0`** — version is pending; this release is not yet published and remains subject to further change.

### Overview

This release lands four major feature areas plus a wave of hardening and dependency work. **Diarization boundary correction** (issue #193) adds a default-on word-boundary smoother and an experimental acoustic backchannel re-check that measurably reduce speaker mislabeling at turn seams. **Content redaction** introduces PII / profanity / toxicity detection with read-time masking across every display and export surface, served by a new dedicated `celery-redaction` CPU worker, with per-user opt-out and an admin enforcement floor. The **cloud ASR provider suite** is now production-verified end-to-end — AWS Transcribe, Speechmatics, AssemblyAI, Gladia, and pyannote.ai all flipped from experimental stubs to tested. The **media download architecture** completes its migration to presigned-URL streaming with SSE progress, async bulk export, and bounded, auto-expiring derived-asset caching. Plus CrisperWhisper model support, an Engine Configuration admin-UI cleanup, full 8-locale i18n parity, and a batch of Dependabot/CI updates.

This release also incorporates the substantial pipeline work that landed since v0.4.1: a refactored **combined transcription engine** with an optional **multi-GPU split**, **hybrid mode** (CPU transcription + GPU/MPS diarization) for small GPUs and Apple Silicon, a large **upload & pipeline performance overhaul** (presigned direct-to-MinIO uploads, content-hash dedup, shared-memory WAV handoff), end-to-end **pipeline timing instrumentation**, model-aware VRAM/batch tuning, orphan-sweeper resilience, and a slimmer backend image. **This release contains breaking changes** — see Upgrade Notes.

### Added

#### Tag management (PR #381, by [@forrestsatterfield](https://github.com/forrestsatterfield))

Tags could be created but never corrected — no rename, no merge, no way to delete a single one, and the only cleanup was an admin-only purge of every unused tag in the deployment. Meanwhile the app generated duplicates itself: the auto-labeler resolved a name against its normalized form before creating anything, while the paths a person types into matched on the exact stored string, so typing `Interview` beside an existing `interview` made a second tag the AI would then match.

- **One resolver for every creation path.** Manual tagging, upload prepare, URL ingest, watch-source polls and auto-labeling all resolve through `services/tag_service.py`: normalized-exact (case, hyphens, underscores and repeated whitespace collapse), a 50-character clamp, and a SAVEPOINT-guarded insert. Bulk paths use a batched sibling that costs a constant number of queries regardless of list length.
- **Rename, merge and delete**, each showing what it would touch **before** it acts — including a count of files beyond the caller's own library, since a shared tag reaches further than you can see. Merge collapses the duplicate-association case rather than aborting on it, and locks rows in id order so two merges in opposite directions cannot each delete the other's survivor.
- **Accept / reject for auto-labeled tags.** Rejecting removes only the associations the AI created; a tag people have also applied by hand survives with that work intact.
- **Collision clusters** group tags that normalize to the same name, with a preselected survivor and separately-ranked near matches that are never cluster members.
- **Bulk apply across a gallery selection**, from the Tags button or Organize → Add / Remove tag.
- **Tag management is a modal**, reached from a **Tags** button beside Collections; with a selection it applies to those files, with none it manages the library. Tags are metadata over the library, not a destination.
- **Ownership is explicit.** Every tag reports `mine`, `system` (the shared vocabulary every account sees) or `shared_with_me` (someone else's, visible because they shared the media it sits on). The UI offers Rename/Delete only where the backend will accept them, and `GET /tags?scope=` takes the same three values so a scoped request returns rows reporting that ownership. Admins can promote a tag into the shared vocabulary, which folds identically-named tags into it so a deployment converges on one `Interview`.
- **Tags travel with shared media.** Sharing a collection makes its files' tags visible to the recipients — in the picker, the gallery filter and search — computed from the file rather than copied, so unsharing removes them again with no cleanup. A second person tagging a shared file reuses the existing tag rather than adding the same word twice.

#### Tag sharing and the ownership model (`v386_add_tag_share`)

A tag was either yours alone or published to the whole deployment, so giving one word to a colleague meant publishing it to everybody — or letting each person coin their own copy, which is the duplication this feature exists to stop.

- **Share a tag with specific users and groups.** `tag_share` mirrors `collection_share` (one user or one group, CHECK-constrained, partial unique indexes). Deliberately **no permission column**: a share grants *vocabulary* — see it, filter by it, apply it — while rename/merge/delete stay with the owner.
- **Every tag reports its `ownership`**: `mine`, `system` (the shared vocabulary), or `shared_with_me`. `GET /tags?scope=` accepts those same three values, so a scoped request returns rows reporting that ownership. The UI offers destructive actions only where the backend will accept them.
- **Tags travel with shared media**, computed from the file rather than copied — so unsharing removes them again with no cleanup step, and a second person tagging a shared file reuses the existing tag rather than adding the same word twice.
- **Tag management is a modal** beside Collections, with search, sort, a create field, the files each tag touches, and bulk chips shared with the collections modal. AI tag review was removed: it asked users to judge a tag with no media on screen, which is the file detail page's job.

#### Release engineering (`scripts/release.sh`)

- **The release process is now a staged, resumable, agent-drivable command sequence instead of
  a prose checklist.** `scripts/release.sh` is a thin dispatcher over `scripts/release/NN-<stage>.sh`
  stage scripts (`preflight → bump → verify → test → build → scan → rehearse → tag → publish →
  smoke → promote → finish`), each independently runnable via `status | explain <stage> |
  <stage> | run <version> [--skip|--only|--from] [--dry-run] [--json] [--yes]`. A release ledger
  under `.release/<version>/steps/` records status, operator, SHA and any override per stage, so
  a release that dies partway through resumes rather than restarting from zero. `tag`, `publish`,
  `promote` and `finish` are the only stages that reach outside the repo — they refuse without
  `--yes` and each carries an `ask` rule in `.claude/settings.json`.
- **Derived version facts replace hand-maintained tables that rot.** The Alembic head is now
  derived from the `down_revision` graph (previously `grep '^revision' | tail -1`, which sorted
  by filename and only worked by luck once the id chain became non-contiguous); FROM/TO versions
  for the upgrade-rehearsal scenario are self-derived from the `VERSION` file and the newest git
  tag that also has published Docker Hub images.
- **Runtime version verification**: a new public, DB-free `GET /api/version`
  (`{version, git_sha, build_time, api_version}`) and `/health/ready` now always report
  `schema`/`schema_revision`/`schema_head`, so the harness can assert "a container started"
  really means "the new code, at the new schema, is running" rather than trusting a tag.
- **Schema-drift gate**, scoped to categories that actually raise at runtime rather than a
  hand-maintained allowlist.
- **Reproducible installs**: `setup-opentranscribe.sh --version vX.Y.Z` / `--branch <ref>` pins
  every download call to that ref; the default resolves to the latest **published GitHub
  Release** (not the newest tag, since images are promoted after the tag lands) and never
  silently falls back to `master` on a resolution failure.
- **Real-speech release-test fixtures**: both release scenarios derive two 45-second speech
  clips from an existing repo test asset and gate on them in `preflight`, closing a gap where a
  rehearsal would have failed at the upload step (synthetic tone audio transcribes to empty
  segments).

### Fixed

#### Search by tag never worked (PR #381)

Transcript indexing read `media_file.tags` and `media_file.collections`, but the model declares `file_tags` and `collection_memberships` — so both `hasattr` guards were permanently false and **every transcript was indexed with an empty tag array and no collection ids**. A manual reindex read the same data correctly, which is why the gap stayed invisible. Indexing now reads the association rows, and every tag mutation enqueues a targeted refresh so filtering by a tag and searching for it agree.

Five further defects this work surfaced, all predating it: `list_tags` dropped a tag entirely when its only file was inaccessible (rather than showing it with usage 0); both tag read endpoints swallowed query errors and returned an empty list with a 200, rendering a broken query as an empty library; seeded default tags were created without a normalized name, leaving the four most common tags in every install invisible to normalized-exact resolution; the bulk rail admitted any non-null permission, so viewer access to a shared collection was enough to mutate its files; and the bulk rail's per-file handler continued without rolling back, so one file's database error aborted the transaction and every later file failed.

Redis pattern deletes now use `SCAN` rather than `KEYS`, which is O(keyspace) *and* blocks the instance that also carries the Celery broker.

#### AI Chat with RAG over your transcripts (issue #52)

- **Chat is a first-class page** alongside Search and Speakers. Ask questions across your recordings and get answers grounded in what was actually said, streamed token by token, with numbered citations that deep-link to the exact timestamp in the player (`/files/{uuid}?t=`).
- **Retrieval pipeline**: conversational query rewriting (expands "what about her?" into a standalone query) → hybrid BM25 + vector search over speaker-turn transcript chunks with RRF fusion → CPU cross-encoder reranking → round-robin diversity sampling so one long recording cannot crowd out the rest of a multi-file selection → short-lived retrieval cache. Every stage is an admin-tunable, DB-backed setting applied on the next message — **no new `.env` vars**.
- **Ask about one person**: a **Speakers** scope filter that is exact rather than approximate — because transcripts are indexed as speaker turns, selecting a speaker retrieves only their own words, so "what did Dana commit to?" can never be answered from someone else's sentence *about* Dana. Speakers are an axis orthogonal to recordings/collections/tags (use together, or alone for "everything Dana said, anywhere"), and the model is told about the filter so it reports a person as out of scope rather than claiming they were never discussed.
- **ChatGPT/Open WebUI interaction parity**: edit a question and re-answer from that point (later turns superseded, not deleted), regenerate, stop mid-stream, per-message and per-code-block copy, message timestamps, conversation export (Markdown or JSON with sources as deep links), archive/restore, date-grouped searchable history, per-conversation model switching with a smaller-context warning, token-usage panel, and keyboard shortcuts (`Cmd/Ctrl+Shift+O`, `Cmd/Ctrl+/`, `Escape` to stop).
- **Scope by recordings, collections, or tags** — or leave it as "All transcripts". Collections and tags resolve to files at query time, so a recording added to a collection later is automatically in scope for existing conversations. The picker estimates context-window usage before you commit. The gallery gains a **"Chat with N"** bulk action that hands the selection straight to a scoped conversation.
- **Optional transcript context**: any conversation can turn retrieval off and act as a plain assistant, with an unmistakable *Context off* chip so an ungrounded answer never looks like a grounded one. Four-layer system prompt (immutable base rules → per-user default from Settings → Chat → project → per-conversation) where every layer appends and none can replace the base rules.
- **Redaction is honoured before the LLM**: the OpenSearch chunk index stores transcript text unredacted (correct for searching your own words), so retrieved excerpts are re-masked with full categories whenever the owner's or an admin-forced `redact_before_llm` policy applies — and masking **fails closed**, withholding a passage it cannot mask rather than sending it raw. Stored answers and citation snippets keep that masking.
- **Prompt-injection hardening**: excerpts are delimited, the base rules state that excerpt content is data and never instructions, closing-tag sequences in transcript text are defused, and the prompt is assembled by concatenation only — never a format string over untrusted text.
- **Abuse controls & audit**: 20/min per-IP, a configurable per-user hourly ceiling, and a concurrent-stream cap, all failing open on a Redis outage. Chat events (`chat.conversation.create/delete`, `chat.message.send`) join the audit trail with metadata only — **never message content**.
- **Projects** (issue #360, migration `v376`): group conversations by client, recurring meeting or case. A project pins a **default transcript scope** every chat inside it inherits — so a project pinned to a client's collection searches that client's recordings without re-picking context — and a **project-level instruction layer** carrying standing background. Deleting a project **keeps its conversations** (`ON DELETE SET NULL`); they become ungrouped. `chat_conversation.project_id` is nullable, so every existing conversation is unaffected.
- **Per-conversation answer length and focus** (issue #359): `max_tokens` and `top_p` alongside the existing creativity and model controls, behind an *Advanced* disclosure. The reply budget is resolved **before** the prompt is built, since prompt assembly reserves context for the answer; it is clamped to the model's window and any plan cap rather than failing the request. `top_p` is omitted entirely when unset, because some models reject sampling parameters outright.
- **Per-user RAG preferences**: users can lower *Excerpts per answer* and turn *Rerank excerpts* off for their own chats. Both are **ceilings, never overrides** — applied after the tenant limit so a preference can only tighten what the administrator allows. Reranking is one-way: it can be switched off, never on when the admin has it off.
- **Mock LLM provider for development and testing**: `./opentr.sh start dev --with-mock-llm` runs an OpenAI-compatible server on the app network so chat and AI features work without a GPU, an API key, or an internet connection. Scenario models (`mock-echo` returns the prompt it was given, `mock-error`, `mock-empty`, `mock-slow`) drive the app's real error paths, and the pytest fixtures fall back to a subprocess so CI needs no setup. `./opentr.sh start dev --with-llm-test` is its GPU-backed sibling — a real, lightweight model (vLLM by default, an Ollama profile as an alternative) on an isolated GPU, for testing chat against genuine model output.
- **LLM streaming** is new across the board: `LLMService.chat_completion_stream()` with parsers for OpenAI-style SSE, Anthropic events, and Ollama NDJSON, plus stop-generation, a first-token watchdog, and token accounting (estimated where a provider does not report usage).
- **Collapsible reasoning/thinking display**, collapsed by default (Open WebUI-style), for
  providers that stream their reasoning separately from the final answer — vLLM/OpenRouter
  `reasoning_content`/`reasoning`, Anthropic extended-thinking `thinking_delta` blocks, and
  Ollama's `message.thinking`. Providers with no dedicated reasoning field are handled by an
  incremental `<think>...</think>` extractor that correctly reassembles a tag split across
  stream chunks, so the answer never leaks unparsed thinking text.
- **Optional retention**: `chat.retention_days` (default 0 = keep forever) with a daily beat sweep. Conversations join GDPR erasure in both the account and org-member paths.

#### Amazon Bedrock provider

- **New LLM provider: Amazon Bedrock**, via the unified **Converse / ConverseStream** API — one integration that reaches Claude, Nova, Llama and Mistral, rather than a per-vendor adapter. Set `BEDROCK_REGION` and `BEDROCK_MODEL_NAME`; there is deliberately **no API-key setting**, because boto3 resolves credentials through the standard AWS chain (instance role, task role, profile, environment), so an EC2/ECS/EKS deployment provisions no secret at all.
- **Cross-region inference profiles** are handled for you: a bare foundation-model ID is prefixed with the geography derived from your region (`us.`, `eu.`, `apac.`), which lets AWS route around a saturated home region. An explicitly prefixed ID or a full inference-profile ARN is used verbatim, so you can pin an exact profile (for example one carrying cost-allocation tags).
- **Tenant attribution**: each request carries `requestMetadata`, so Bedrock's own invocation logs can be reconciled against the usage records below rather than merely trusted.
- Streaming, cancellation and error handling match every other provider. Bedrock reports throttling and server faults as *members of the event stream* rather than as raised exceptions — unhandled, those look like a silently truncated answer — so each is surfaced as a normal error.

#### Usage tracking (all editions)

- **New: see what you are using.** `GET /usage/me` returns totals and a per-model breakdown over a trailing window; `GET /usage/me/daily` returns the daily series behind a chart. This is a **core, open-source** feature — anyone paying an LLM bill has the same question a hosted tenant does.
- One `usage_event` per assistant message, keyed on the message UUID so a retry cannot double-count. Usage is stored in **tokens, not currency** (provider-neutral, so a vendor price change does not invalidate stored history), and cost is derived at read time.
- **Costs are labelled estimates, and unpriced is not free.** A model with no known rate reports tokens only and sets `cost_incomplete`, because a confident `$0.00` is a worse answer than an honest blank. Local runtimes (Ollama, vLLM) are reported as explicitly free — a distinct state. Amazon Bedrock is deliberately unpriced: it is AWS-operated with its own rate card, and pricing it from Anthropic's published rates would be confidently wrong.
- **Prompt-cache tokens are tracked and priced separately** from ordinary input tokens throughout. Cache reads bill far below the uncached input rate and cache writes above it, so folding either into the input count would misprice every cache-enabled deployment.

#### Per-tenant chat limits (cloud-edition seam)

- Two new resolvers on `core.tenant_limits` — chat ceilings (messages/hour, concurrent streams, output tokens, retrieved chunks) and a model allowlist. Both default to community no-ops, so **the self-hosted edition is unchanged**: no limits, no model restriction.
- A tenant limit can only ever **tighten** an operator's setting, never widen it. The model allowlist is enforced server-side, because the per-conversation model comes from a user-supplied setting.
- New `chat.ungrounded` capability gates the *"use my transcripts: off"* toggle. Enabled everywhere by default — it has legitimate uses — and when disabled it **degrades to a grounded answer rather than rejecting the request**.
- Migration `v374`; new capability key `chat.rag` (community default: on); 141 new i18n keys across all 8 locales.

#### Unified in-app search foundation (PR #282)

- **One search primitive, one behaviour, everywhere.** A new shared search bar component
  (counter, prev/next, two-phase loading spinner, i18n labels) and shared fuzzy-match utilities
  (fuse.js-backed, diacritic/case folding) replace ad hoc find-in-page logic duplicated across
  the transcript viewer and summary panel.
- **The transcript find bar now sees matches beyond the loaded page.** It instantly highlights
  the currently-loaded window, then resolves a debounced, file-scoped `GET /search/count` (a
  lightweight `size=0` OpenSearch query, ~20ms vs ~85ms for a full search) to show an `N of M+`
  indicator and drive progressive load-more when matches exist outside what's rendered. The
  summary panel's find bar is a thin wrapper (the whole summary is already in memory, so its
  find stays complete).
- **macOS-style search over Settings**: a search box above the sidebar tabs replaces the grouped
  nav with ranked, highlighted results as you type; selecting one jumps to the section and
  flashes the matched control. Built from the i18n key tree, so it works in all supported
  locales, respects capability/edition gating, and required no edits to any settings panel.

#### Open-core cloud seams & strict edition separation (PR #250)

- **Vendor-clean extension seams**: the commercial managed edition now layers onto generic, open extension points — a pluggable external token-verifier registry with JIT provisioning onto generic `external_id`/`external_org_id` columns, transcription pipeline hooks (quota reservation before dispatch, usage metering on completion — no-ops in community), a capabilities/entitlements resolver, per-tenant retention/upload-limit resolvers, and a frontend `$lib/cloud` seam whose community version is an inert stub. **No vendor noun appears anywhere in the open-source backend or frontend source** — enforced by a `seam-guard` CI gate (grep over `backend/app` + `frontend/src`) and an import-linter contract. The paid UI (hosted-auth wrapper, billing/usage/team panels, quota stores, cloud i18n packs) lives in the commercial repo and is overlaid at cloud-image build time only.
- **Multi-tenant isolation (inert in community)**: nullable `organization_id` scoping across media, collections, speakers, and search planes with membership-mirror authorization (`resolve_org_context`/`require_org_admin`), default-deny tenant gating threaded through every read surface — gallery, detail, search, subtitles/segments/waveform, stream/download URLs, analytics, comments, tags, topics, summaries — plus org-filtered speaker/voiceprint kNN and cross-org share blocking. Personal scope (`organization_id IS NULL`) behaves exactly as before; the community edition is unaffected.
- **GDPR erasure & abuse takedown (edition-neutral)**: real Art. 17 erasure cascading object storage, relational rows, and OpenSearch voiceprint (biometric) docs — org-scoped for org admins, account-wide only for the data subject or platform super-admins, with legal-hold files preserved (Art. 17(3)(e)) and the acting admin audited; admin quarantine/legal-hold with exclusion from every read surface (list, detail, search, autocomplete, collections) and release restoring the file's recorded prior status.
- **Takedown owner notice — DMCA §512(g) (issue #262)**: quarantining a file now sends its OWNER a persistent in-app notification (new WebSocket types `file_takedown` / `file_takedown_released`, 8-locale i18n) carrying the file title/filename, the admin-recorded reason, and counter-notice instructions pointing at the deployment's `ABUSE_CONTACT_EMAIL` (file UUID included for the counter-notice reference); releasing sends an access-restored notice with a working file link. The acting admin's identity is never disclosed, the file itself stays hidden (404) while quarantined, and a notification failure never blocks the takedown or release. Documented in `docs/abuse-and-takedown.md`.
- **Usage events spine**: an idempotent `usage_event` table + `record_event` service for metering/product analytics (empty in community).
- **JIT provisioning hardening**: linking an external identity to an existing account by email match requires the IdP to assert the address verified (fail-closed `email_verified` on `ExternalIdentity`); `super_admin` accounts are never JIT-linked by email; external IdPs grant at most `admin` and never demote.
- **Tenant/privacy hardening follow-ups (issue #262, migration v372)**: audit events now carry a nullable `organization_id` stamped at write time where the writer has tenant context (takedown/release, GDPR erasures, unredacted-view, prompt share/clone), and the org-admin audit read scopes on it — org-stamped events (including `user_id`-NULL failed logins) plus legacy un-stamped events attributed via member ids; other orgs' stamped events are never visible. Background imports capture the org at CREATION time instead of guessing from memberships: `watch_source.organization_id` (backfilled by v372) stamps every watch import, and playlist/URL placeholders receive the originating request's org through task kwargs (`resolve_owner_org_id` demoted to a documented last-resort for storage recovery). Remaining collection sub-surfaces (get/update/delete, share list/create/update/revoke, collection-media add/remove/list) are tenant-gated via `ctx.org_id`; group-targeted shares of an org collection now require every group member to belong to that org; org-context media adds reject cross-scope files. `SpeakerProfile` rows created via the API inherit the request's (or the speaker's) org. Collection member counts and the paginated collection-media list exclude quarantined files for non-admins. User-triggered re-diarization fires the before-dispatch access seam with a zero-hours reservation so a suspended/canceled cloud org can no longer burn GPU (402; community no-op, `CLOUD_SEAM_VERSION` unchanged).
- **Speaker-cluster tenant scope (issue #262, migration v373)**: cross-video speaker clusters are now tenant-scoped like the rest of the speaker plane. `speaker_cluster.organization_id` (NULL = personal) is stamped at creation from the member speakers' file org and mirrored onto the OpenSearch centroid doc, so org files join org clusters and personal files join personal clusters — previously org-file speakers could never join ANY cluster (isolation-safe but degraded to per-speaker singletons). `batch_recluster` now partitions the Phase-2 similarity graph per tenant scope, so a member's org and personal recordings of the same voice are never merged into one cluster. The one-off tenant backfill stamps existing cluster rows + docs from their member speakers' file orgs (all-same-org rule; legacy mixed-scope clusters stay NULL, are counted in the summary, and dissolve into per-scope clusters on the next re-cluster), replacing the earlier strip-all-org cluster repair. Community edition: org is NULL everywhere, one partition, no org field on any doc — behavior unchanged.

#### Authentication & identity (issues #353, #354, #355; migrations `v377`–`v383`)

- **Generic OpenID Connect — any conforming provider, not one vendor (issue #353)**: endpoints are
  resolved from the provider's `.well-known/openid-configuration` when a **Discovery URL** is set,
  and only fall back to the realm URL template (`<server>/realms/<realm>/protocol/openid-connect/…`)
  when it is not. That template is one product's URL shape, and it was the *only* code path, so
  Authentik, Authelia, Okta, Entra ID, Auth0 and Zitadel were all handed a 404 on the login
  redirect. Discovery documents and JWKS are TTL-cached for 15 minutes (the JWKS was previously
  refetched on **every** token validation), a document missing required endpoints is not cached so
  a broken configuration is not pinned for the whole TTL, and a discovery failure degrades to the
  realm URLs rather than taking a working deployment down. The **Roles Claim** is a configurable
  dotted path (`realm_access.roles` by default, `groups` for Authentik/Okta, `roles` for Entra ID)
  and falls back to the userinfo endpoint when the claim is absent from the token. The internal-URL
  swap applies to discovered endpoints too.
- **The OIDC surface is renamed `oidc_*`, and `KEYCLOAK_*` keeps working forever
  (`v377`, `v378`)**: configuration keys, Pydantic schema, service, admin-panel tab, i18n across
  all 8 locales, and the routes (`/api/auth/oidc/login`, `/api/auth/oidc/callback`) are all
  provider-neutral. **No identity provider needs reconfiguring** — the registered redirect URI
  points at the SPA's `/login` page, never at the backend routes. Stored database configuration is
  renamed by `v377` carrying the ciphertext across unchanged (no decrypt/re-encrypt), and `v378`
  renames `user.keycloak_id` → `user.oidc_subject` (named for what it is: a `sub` is unique per
  *issuer*, not globally), `keycloak_refresh_token` → `oidc_refresh_token`, and the `auth_type`
  value `keycloak` → `oidc`. `KEYCLOAK_*` environment variables are translated onto the canonical
  `OIDC_*` names before settings are built — an input adapter, not a second implementation — with
  the legacy spelling winning when both are set and a single deprecation line at startup. A unit
  test fails the build if the retired noun appears under `backend/app/` outside a three-entry
  allow-list, each carrying a written reason. `docs/KEYCLOAK_SETUP.md` → `docs/OIDC_SETUP.md`
  (the old path is a redirect stub), and `docs-site/docs/authentication/keycloak.md` →
  `oidc.md` likewise.
- **OIDC provider presets**: a "Provider preset" dropdown (Keycloak, Authentik, Entra ID, Okta,
  Google Workspace, Generic) at the top of the OIDC settings panel fills the roles claim,
  scopes, and (where applicable) discovery URL to the known shape for that provider — addressing
  the most common silent-failure class (wrong claim path → login succeeds → groups/roles come
  back empty → nobody notices until permissions are wrong). Authentik, Entra and Okta presets
  surface their known caveats (Authentik's hardcoded `email_verified: false`, Entra's
  GUID-shaped groups claim, Okta's opt-in groups claim) at configuration time. OIDC Test
  Connection now also reads the provider's discovery document `claims_supported` and reports
  whether the configured roles-claim path is advertised (yes/no/unknown), rendered as a claims
  panel in the settings UI.
- **Guided first-run setup wizard (#28)**: shown once to the bootstrap `super_admin`, surfacing
  the three settings a first-time operator actually needs (password change, SSO/LDAP setup, and
  the MFA-required/login-banner/approval-on-signup security defaults) instead of leaving them to
  discover dozens of auth-related env vars and several admin tabs unassisted. It presents
  existing settings screens rather than duplicating them.
- **The identity-source model (issue #354)** — `local_enabled`, `allow_registration`, per-user
  `auth_type` + `allow_local_fallback`, and `pki_allow_password_fallback` as a deployment ceiling
  over the per-user flag. Previously `/token` always accepted a local password, so an
  LDAP- or OIDC-owned deployment could not actually turn local authentication off; the intended
  auth method was advisory. The API also refuses the incoherent combination
  (`allow_registration` on while `local_enabled` is off — self-registration mints local-password
  accounts that could never sign in), and re-checks the *resulting* state so it cannot be
  assembled one save at a time. **An active `super_admin` with a password path is exempt**: auth
  configuration is super_admin-gated, so without the exemption a deployment that disabled local
  login while its IdP was misconfigured would have no way back in.
- **Admission control — "does this deployment want you", separately from "are you who you say"
  (`v379`)**: `oidc_allowed_groups` / `oidc_blocked_groups` evaluated against the roles claim
  (semicolon-delimited, because a directory group value is a DN and contains commas; blocked is
  evaluated first and means *denied*), and `require_account_approval`, which lands a newly
  provisioned account — self-registered **or** JIT-provisioned by any external IdP — in a
  `pending` state with an admin queue at `GET`/`POST /api/admin/user-approvals`. An empty
  allow-list admits everyone, so upgrading changes nothing until an operator sets one. Refusals
  return the same generic 401 an unusable token gets, and are audited.
- **IdP group mapping (`v378`)**: a `group_mapping` row binds one directory claim value —
  an LDAP group DN or an OIDC role/group name — to an in-app `UserGroup`, to a role grant, or to
  both. Both directory paths already carried the caller's full group list and discarded everything
  but a single "is this an admin" bit. Applied at login for LDAP **and** OIDC, and on the periodic
  LDAP sweep, through **one** implementation. `grants_role` is capped at `admin` in the wire
  contract, in the service, and by a database CHECK constraint — **`super_admin` is unreachable
  from any identity provider**, and a `super_admin` is never demoted by reconciliation either.
  `user_group_member.source` marks directory-derived membership, so reconciliation removes only
  what it added and a hand-added membership is never touched; the column defaults to `manual`, so
  the default *is* the backfill. Super_admin API at `/api/admin/group-mappings`, with an admin
  panel (Settings → Authentication → **Group mappings**, LDAP/OIDC sources — `proxy`-sourced
  mappings are still API-only) and a dry-run `POST /test` that resolves a claim list (or a real
  LDAP account) and reports matched vs unmatched claims without writing anything.
- **Trusted-header (reverse-proxy) authentication (`auth_type='proxy'`)**: a front-line
  authenticating proxy (oauth2-proxy, Authelia, Cloudflare Access) can assert an already-verified
  identity in a header instead of the user presenting credentials to OpenTranscribe directly. One
  shared trust module (`header_trust.py`, also used by PKI's header mode) decides whether to
  believe the assertion: the **immediate socket peer**, never `X-Forwarded-For`, must be in a
  configured CIDR allowlist, and an empty allowlist refuses every assertion rather than trusting
  the network. An optional shared secret is compared in constant time, the role header is capped
  at `admin` (never `super_admin`), and every refusal — including from an untrusted peer — is
  audited. Settings → Authentication → **Trusted Header** configures it (previously API/`.env`
  only).
- **PKI trusted-proxy allowlist and header names are now genuinely DB-backed**: the Settings UI's
  PKI panel has had "Trusted Proxies" and header-name fields since PKI shipped, but
  `pki_authenticate()` only ever consulted a module-level list parsed from `.env` at process
  start — a Settings UI save silently did nothing. `DynamicAuthSettings` gained the missing
  `pki_trusted_proxies` / `pki_cert_header` / `pki_cert_dn_header` properties (the same DB > env
  > default layering every other PKI field already had), so narrowing or widening the allowlist
  through the UI now takes effect immediately, matching trusted-header auth's equivalent
  `ProxyConfig.from_db` resolution.
- **SAML 2.0 service-provider support (`auth_type='saml'`, `v383`)**: a fourth external identity
  source (issue #35) via python3-saml — SP metadata at `GET /saml/metadata`, SP-initiated login
  at `GET /saml/login`, and the IdP's own POST/redirect callback targets (`/saml/acs`, `/saml/sls`).
  Signature verification is python3-saml's, never hand-rolled. Reuses the same admission control,
  approval-state, account-linking and session machinery already built for OIDC — SAML's
  `email_verified` is always treated as unasserted (no standard SAML claim for it), so an
  email-match account link is refused unconditionally rather than being a togglable setting. IdP
  group mapping and SP-initiated logout notifying the IdP are deliberately out of scope for this
  release. Configurable end-to-end at Settings → Authentication → **SAML 2.0** (SP identity, SP
  certificate/key, IdP identity, signing posture, attribute mapping, group admission) — previously
  API/`.env` only.
- **SCIM 2.0 provisioning (`/scim/v2`, RFC 7643/7644, `v382`)**: mounted at root rather than under
  `/api` because RFC 7644 §3.1 fixes the base path and every connector appends `/Users`/`/Groups`
  to it. Bearer-token authenticated against a hashed, revocable token a super_admin issues at
  Settings → Users → SCIM Tokens (`/api/admin/scim-tokens`). Every write goes through the same
  services the admin UI and directory sync already use — no SCIM call ever writes a role, and
  `super_admin` is untouchable through it; `DELETE` and `active: false` both disable the account
  and revoke its sessions rather than deleting the row, matching an IdP dropping someone from
  scope rather than actually removing them. Filtering supports exactly
  `<attribute> eq "<value>"` on the documented attributes; anything else is a clean
  `400 invalidFilter`. Not rate-limited (a 256-bit token bursts hundreds of requests from a small
  IdP egress pool by design).
- **Directory sync and deprovisioning (LDAP)**: there was previously **no deprovisioning at all**.
  Sync ran only at login and only upward, so an account deleted or disabled in Active Directory
  kept a live row forever — and because refresh tokens rotate on every use, an actively-used
  session survived the user's termination indefinitely. A beat-driven pass now probes every active
  LDAP account, reconciles groups and role for the ones still present, and disables **and revokes
  the sessions of** the ones that are provably gone. Fail closed on *ambiguity*, not on error:
  "the directory says gone" acts, "I could not ask the directory" aborts the pass. `super_admin`
  and `local` accounts are never touched, it disables rather than deletes, and
  `directory_sync.max_disables_per_run` bounds the blast radius — a directory answering "gone" for
  everyone (wrong search base, wrong group DN) is indistinguishable from mass offboarding.
  Defaults are deliberately timid (`enabled=false`, `dry_run=true`), so an operator opts in twice.
  Six DB-backed `SystemSettings` rows, no new environment variables; **no endpoint and no admin
  panel yet**.
- **Admin invitations**: an admin names an address plus the target `role` and `auth_type`; the
  invitee proves control of the address and chooses their own credential, or is handed to the IdP
  for an external `auth_type`. This closes a real gap — disabling self-registration was only half
  a feature, because `POST /api/admin/users` could not set `auth_type`, so every admin-created
  account was `local` and could not authenticate at all on a deployment where local passwords are
  off. Tokens are SHA-256 hashed at rest, single-use and expiring, and every rejection (unknown,
  expired, revoked, already used, address already registered) returns one identical message.
  Admin-created accounts can now set `auth_type` too, omitting the password field entirely for an
  external type.
- **Email verification**: `require_email_verification` was a declared auth-config key, rendered as
  a switch, stored on save, and **read by nothing**. It now gates local password login (only —
  an LDAP/OIDC/PKI address is asserted by the provider, and blocking those logins would
  second-guess the IdP), with 24-hour tokens rate-limited to 3 issues per hour and
  `/auth/verify-email` + `/auth/verify-email/resend` endpoints.
- **Account lifecycle enforcement**: `must_change_password` and `account_expires_at` were both
  written and never read — the admin "force change on next login" flag let the user sign in with
  the admin-chosen password and never prompted, and a time-boxed contractor account stayed usable
  forever. Both are enforced at the one dependency every user-facing route passes through, with
  machine-readable `detail.code` values (`password_change_required`, `account_expired`,
  `banner_acknowledgment_required`, `account_pending_approval`, `account_rejected`) so clients
  branch on a contract rather than on English prose. Password expiry (`password_max_age_days`,
  FedRAMP IA-5(1)) now feeds the same flag rather than inventing a second mechanism; an account
  with no recorded `password_changed_at` is warned about rather than force-changed, because
  nothing stamped that column on older accounts and forcing all of them would be a self-inflicted
  outage.
- **The login banner is enforced, not merely displayed (FedRAMP AC-8)**: `banner_acknowledged_at`
  was written by the acknowledgment endpoint and read by nothing; the SPA approximated the control
  with a `sessionStorage` flag that clears per tab, is trivially removed, and never reaches the
  server. The consent AC-8 requires was therefore never a precondition for anything. It is now a
  server-side gate — and **an acknowledgment expires when the banner text changes**, compared
  against that config row's `updated_at`, because someone who accepted one classification notice
  has not accepted a later, stricter one.
- **Session controls that actually apply (`v377`)**: idle timeout, absolute timeout and the
  concurrent-session limit + policy were read from `.env` while the admin UI wrote the database,
  so the Session tab was inert. All three are DB-backed now and take effect without a restart. The
  panel also offered `oldest`/`newest`/`all` for the concurrency policy, none of which the backend
  compares against, so the AC-10 limit silently enforced nothing whichever option was chosen; the
  vocabulary is now `terminate_oldest`/`reject` on both sides, and hitting the cap is audited
  either way. `absolute_expires_at` is carried forward through rotation and never recomputed — it
  is the only thing that caps a client that refreshes forever. Both timeout columns are nullable
  and un-backfilled, so upgrading does not sign everyone out a second time. Users see and revoke
  their own sessions in Settings → Profile; admins can list and revoke another account's.
- **One session store, not two.** `auth/session.py` carried a Redis `SessionManager` implementing
  the timeout half with **zero call sites**. It was deleted rather than wired up: two owners would
  enforce against different session sets the moment Redis and Postgres diverged, and issue #324
  already established that Redis is a cache here, not the system of record. A session is a
  `refresh_token` row.
- **Transactional auth email through the deployment's real mail configuration**: password resets,
  invitations and verification links are delivered by a designated `EmailNotificationConfig` row
  (super_admin; `PUT /api/admin/auth-config/email/designation`), falling back to the `SMTP_*`
  environment transport when no row is designated. A designation naming a missing or disabled
  configuration is rejected at **write** time, and deleting or disabling the designated row is
  refused — the read path degrades quietly enough that a bad designation would surface only as
  undelivered password resets.
- **Auth-config audit is visible in the product**: `auth_config_audit.changed_by` is a NOT NULL
  foreign key that was never serialised, so the answer to "who turned MFA off / changed the LDAP
  bind password" sat in Postgres and was invisible. Settings → Authentication → **Audit** now
  shows configuration changes with the account that made each one.
- **Three privilege tiers, with a stated rule.** *Anything that changes how the deployment runs,
  or stores infrastructure credentials, is `super_admin`; anything that manages users and their
  content is `admin`.* Creating another `super_admin` is now a UI action (Settings → Users →
  Role) rather than something reachable only through direct API access, it is audited, and the
  last remaining `super_admin` cannot be demoted or deleted. A unit test walks the live dependency
  tree and fails if a new route lands at the wrong tier or is accidentally public.
- **Admin user management** gained lock/unlock (unlock is now the true inverse of lock: it clears
  **both** the deactivation and any failed-login lockout), force-logout, MFA reset, per-account
  session listing, and an `auth_type` column.

#### Backend observability & monitoring

- **Prometheus metrics**: new root-mounted `/metrics` endpoint (internal-network only; denied by nginx) exposing request-duration histograms by route template/method/status, request counters, in-flight gauge, **per-request DB-query-count histograms** (`db_queries_per_request` — surfaces N+1/duplicate queries per endpoint), DB query latency, cache hit/miss counters, priority-aware Celery queue-depth gauges, and product counters (`user_signups_total` by auth method, `files_uploaded_total` by source). No user IDs or raw paths in labels (cardinality-safe).
- **Structured access logs**: every request emits one access-log line carrying `user_id`, `org_id`, `request_id`, route template, status, `duration_ms`, and `db_query_count` — human-readable in `LOG_FORMAT=text` (default), single-line JSON (Loki/CloudWatch-ready) in `LOG_FORMAT=json`. Slow SQL statements (> `SLOW_QUERY_MS`, default 500 ms) log a parameter-free WARNING with the request ID.
- **Request-ID propagation into Celery**: tasks dispatched during a request now carry its `X-Request-ID`, so API requests and their background work correlate in logs; worker logging is configured via Celery's `setup_logging` signal (JSON-capable like the API).
- **Readiness probe** `GET /health/ready`: probes PostgreSQL/Redis (critical → 503) and OpenSearch/MinIO (reported, non-critical) for load balancers and orchestrators; `/health` is unchanged.
- **Optional Prometheus + Grafana overlay**: `./opentr.sh start dev --with-monitoring` starts Prometheus (:5186) and Grafana (:5185) with a provisioned datasource pair (Prometheus + read-only PostgreSQL) and two prebuilt dashboards — an ops dashboard (latency p50/p95/p99 by route, RPS, 5xx rate, DB queries/request, cache hit ratio, queue depth) and a product/usage dashboard (signups, uploads, DAU/WAU, transcription minutes). Fully optional; nothing changes when the flag is absent. Docs: `docs-site/docs/operations/monitoring.md`.

#### Fresh / isolated deployments & data-safety guardrails

- **`./opentr.sh start dev --fresh [name]`**: throwaway deployments in an isolated compose project with their own containers AND named volumes; the NAS/bind storage overlay is **never** loaded in fresh mode, so experiments physically cannot touch real data. Standard dev ports by default (with a guard if the main stack holds them) or `--port-offset N` for side-by-side; `--seed-benchmark` uploads sample media once healthy; `stop/status --fresh`, `fresh-list`, and a confirm-gated `fresh-destroy` manage lifecycle; `--dry-run` prints the compose plan without starting anything.
- **Explicit NAS-overlay directives**: the silent `.env` auto-load now announces itself with the resolved data paths; `--no-nas` suppresses it, `--nas` opts in explicitly.
- **Live-data guardrails**: every NAS-overlay start writes a `.opentranscribe-live-data` marker README into each bind-mounted data directory AND its parent (a tripwire for cleanup scripts and humans), and `./opentr.sh data-paths` prints exactly which host paths hold live data so they can be checked before any cleanup.

#### In-app scheduled database backups (no host cron)

- **Admin UI–configured backups** (Settings → System Management → Backups): enable/disable, cron schedule, destination, GFS retention (7 daily / 4 weekly / 12 monthly), optional gpg encryption (passphrase file), Run Now with last-result display. All settings are DB-backed (`backup.*` SystemSettings) — **no host cron, no new env vars, no new Python deps** (native minimal cron parser; no croniter).
- **Execution**: the existing celery-beat fires a lightweight `backup.check_schedule` every 5 minutes; when the DB-stored cron is due it dispatches `backup.run` → `pg_dump --format=custom` directly from the worker (the backend image now ships `postgresql-client`) to the mounted destination, then prunes by GFS. Schedule changes apply with no restarts.
- **Destination — mounted folder OR S3-compatible bucket**: the optional `docker-compose.backup.yml` overlay (`./opentr.sh start dev --with-backup`) maps `BACKUP_HOST_PATH` → `/backups` for a local destination; alternatively the destination can be an **S3-compatible bucket** (AWS S3, MinIO, Backblaze, etc.) so dumps land **off the host machine** — endpoint/region/bucket/prefix + access key, with the secret key encrypted at rest (AES-256-GCM, write-only, never returned) and a Test Connection button. GFS retention prunes over either backend. When the destination is unmounted/unreachable the feature degrades gracefully (UI warning, task records a status and never crashes).
- **Optional OpenSearch snapshots in the same run**: when enabled (Settings → Backups → "Include OpenSearch snapshot"), each scheduled backup also snapshots the search indices to the **same destination** beside the `.dump` files, pruned by the **same GFS retention**. The snapshot runs only after a successful database dump and its outcome never fails the dump (search indices are rebuildable from Postgres, so this is a convenience). Uses a filesystem snapshot repository allow-listed by the `--with-backup` overlay; degrades gracefully without it.
- **Recovery-key companion (#243)**: a database dump alone is **unrecoverable** without the `.env` master keys that wrote its AES-256-GCM ciphertext, so every successful run now makes the destination self-describing. With backup encryption ON, `opentranscribe-recovery.env.gpg` lands beside the dumps carrying `ENCRYPTION_KEY` / `JWT_SECRET_KEY` (and `MINIO_KMS_SECRET_KEY` when set) under the **same gpg passphrase** — the passphrase in your password manager then unlocks a complete restore. With encryption OFF, a no-secrets `RECOVERY-README.txt` (key names + SHA-256 fingerprints only) documents what to preserve separately, and admins get a **one-time warning notification** that the dumps alone are not restorable. One always-current companion per destination; a companion failure never fails the backup; key values never appear in logs or the recorded result.
- **Failure surfacing (#244)**: scheduled-backup outcomes are now surfaced proactively instead of only on the admin page. New Prometheus metrics — `backup_last_success_timestamp_seconds` (alert on staleness), `backup_last_status` (1/0), and `backup_runs_total{result}` — persisted by the worker and projected onto `/metrics` at scrape time (restart-proof, same sample-at-scrape pattern as queue depths). Failed runs (pg_dump error, missing mount, unreachable bucket) send a `backup_status` WebSocket notification to every admin with the error message; successes with non-fatal warnings notify too. **Retention-prune errors no longer fail a completed dump** — they're recorded as `prune_error` warnings (matching the OpenSearch-snapshot warn-only design), and the Backups panel now shows prune + recovery-companion status per run.
- **Media Mirror (#242)**: the backup system now covers the irreplaceable media originals, not just the database. A new **default-OFF** admin subsection (Settings → Backups → Media Mirror) schedules an **incremental copy of the MinIO media bucket** to a **separate destination** — a mounted folder (`BACKUP_MIRROR_HOST_PATH` → `/media-mirror`, via the same `docker-compose.backup.yml` overlay) or an **S3-compatible bucket** (AES-256-GCM encrypted write-only secret, Test Connection) for a true off-host copy. Objects are compared by size + ETag so only new/changed media transfers (write-once media makes nightly deltas tiny); regenerable data (temp preprocessed audio, derived/bulk caches) is excluded; per-object failures never abort a run; a configurable throttle caps I/O pressure; a Redis lock prevents overlapping runs; the run executes on the download worker (never the GPU queue). **The mirror never deletes at the destination** — a fat-fingered or malicious source-side delete cannot propagate (explicit tested invariant). Observability follows the #244 pattern: `media_mirror_last_success_timestamp_seconds`, `media_mirror_last_status`, `media_mirror_runs_total{result}`, per-outcome object counts, admin WebSocket notification on failure (success silent), last-run status + Run Now in the panel. Settings are DB-backed `backup.mirror_*` SystemSettings (cron schedule, no restarts). Closes the backup audit's High-severity media gap; restore-from-mirror documented in `docs-site/docs/operations/backup-restore.md`.

#### Configurable external storage & search backends (issue #284 Phase 1B — A1.11/A1.12/A1.13)

- **Native AWS S3 storage backend (`STORAGE_BACKEND=s3`)**: object storage is no longer hardwired to the bundled MinIO container. The `s3` backend targets the real regional endpoint (or any S3-compatible provider via `S3_ENDPOINT_URL`), signs SigV4 with `S3_REGION`, and uses virtual-host addressing. Credentials come from the **AWS provider chain by default** (`S3_USE_IAM_ROLE=true` — environment, EKS/IRSA web identity, ECS task role, EC2 instance metadata), so a deployment needs no static keys and gets automatic rotation; `S3_USE_IAM_ROLE=false` signs with `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` instead. Optional `S3_CONFIGURE_BUCKET_CORS` applies a browser-upload CORS policy (`ETag` exposed) so direct-to-bucket PUTs pass preflight. **`STORAGE_BACKEND=minio` is the default and self-hosted behaviour is unchanged** — same endpoint, credentials, addressing, and presigned-URL rewriting as before.
- **Uploads above 5 GiB on S3**: AWS rejects a single PUT over 5 GiB (`EntityTooLarge`) only after the browser has streamed the whole body, so `POST /files/prepare` now withholds the presigned URL for oversized objects and the client falls back to the API-mediated upload, which spools to disk and writes multipart in 64 MiB parts. MinIO's 5 TiB single-PUT ceiling means the browser-direct path is untouched on the default backend.
- **Configurable presigned-URL public host + STS-safe TTL**: `STORAGE_PUBLIC_URL` is a backend-agnostic alias for `MINIO_PUBLIC_URL` and decides the browser-facing origin (empty keeps today's `/s3` proxy path on MinIO and leaves native-S3 URLs untouched). All presigned lifetimes are now clamped to `PRESIGNED_URL_MAX_SECONDS` (default 6 h): a presigned URL cannot outlive the credentials that signed it, and IAM-role STS sessions expire well inside the previous 24 h default, so long URLs silently began returning 403. The host rewrite also matches `https://` endpoints (previously `MINIO_SECURE=true` defeated it) and the last hardcoded `http://minio:9000` → `localhost:5178` rewrite (plus the undocumented `EXTERNAL_MINIO_URL`) is gone.
- **Configurable OpenSearch auth (`OPENSEARCH_AUTH=basic|sigv4`)**: every OpenSearch client is now built from one place. `sigv4` signs with the AWS credential chain (`OPENSEARCH_AWS_REGION`, `OPENSEARCH_AWS_SERVICE=es|aoss`) and forces TLS, which is what an Amazon OpenSearch Service domain with an IAM access policy requires; `basic` is the default and unchanged.
- **Embedding-mode switch (`OPENSEARCH_EMBEDDING_MODE=local|managed`)**: `managed` adopts a model the domain already hosts (`OPENSEARCH_NEURAL_MODEL_ID`) instead of mutating ML Commons cluster settings and registering a model by URL — operations a managed AWS domain does not permit, which previously made neural search fail to initialise there. `local` is the default and unchanged.

#### Storage recovery: in-place re-ingestion of orphaned MinIO objects

- **`python -m app.scripts.reingest_minio`** (run in the backend container; `--dry-run`, `--limit N`, `--user-email`, `--no-dispatch`, `--throttle N`): registers media objects that exist in MinIO but have no database row — each new `MediaFile` points at the **existing** object key in place (zero bytes copied or duplicated), gets a real imohash fingerprint, and is dispatched through the standard processing pipeline. Idempotent: re-runs skip already-referenced objects. Born from a data-loss incident where the database was destroyed but all original media survived in MinIO.
- **YouTube metadata recovery, rate-limited**: `recovery.youtube_metadata_fetch` harvests title/duration per surviving `youtube_<id>` thumbnail prefix via yt-dlp metadata-only requests (~1 every 5 s, resumable via a MinIO sidecar — never re-downloads videos), and `recovery.youtube_metadata_backfill` re-attaches titles by duration matching (±2 s, unique-match-only in both directions — ambiguous matches stay safely untitled).

#### Diarization boundary correction (issue #193)

- **Word-boundary smoothing (default ON, pure-CPU)**: a post-processing pass (`boundary_resolver.smooth_word_speakers`) that collapses 1–3 word "wrong-speaker islands" at turn seams, guarded by silent-gap and flanking-speaker checks. It relabels existing words only — never fabricating speech — and runs at the path-agnostic `finalize_segments()` chokepoint so every transcription path gets it identically. Measured −32% relative WSER and islands 82→15 on the reporter's hand-labeled clip; AMI-regression-safe.
- **Acoustic backchannel re-check (default OFF, experimental, GPU)**: re-embeds short disputed/overlap words with the diarizer's WeSpeaker model while audio is still in memory and reassigns them to the best-matching speaker centroid by voiceprint cosine — recovering absorbed backchannels ("yeah", "mm-hmm") the smoother can't. A further ~−15% WSER atop the smoother, ~1.9 s added per 10-minute file. Carried on `EngineConfig` so the engine stays DB-free.
- **Live admin tuning, no restart**: Settings → Engine Configuration gains a smoothing toggle, an acoustic re-check toggle, and two number inputs (cosine margin, max word duration), DB-backed with env fallback. The Engine Settings API gained float-value support.
- **CrisperWhisper model support**: selectable English-only Whisper model with precise word-level timestamps (`nyrahealth/CrisperWhisper`, ~10 GB VRAM). Short names and the PyTorch repo id resolve to the loadable CT2 build at load time (including per-file reprocess), with a verbatim-tokenizer normalization pass that restores spacing, repairs timestamps, and preserves word count.

#### Content redaction (PII / profanity / toxicity)

- **Read-time masking across all surfaces**: detects sensitive/offensive content and masks it with `[CATEGORY]` placeholders at every display and export surface. The full original transcript is always retained in the DB — masking is a read-time transform from cached spans (`services/redaction/spans.py:apply_redactions`). Detect-once / cache-forever (`transcript_segment.redactions` + `.toxicity`); enabling, categories, style, custom words and allowlist are all read-time (no recompute). Detectors: profanity/custom wordlist, Presidio regex + spaCy NER (optional GLiNER) PII, toxicity classifiers (English + multilingual XLM-R), and an optional LLM detector reusing `LLM_PROVIDER`.
- **Dedicated `celery-redaction` worker service**: a new independently-scalable CPU service (queue `redaction`) is the only worker that loads the PII/toxicity models. Runs at lower OS priority (`nice`) with capped intra-op threads; GPU-visible so it can opt into GPU when free VRAM allows, falling back to CPU. Added to the Flower queue list with a health check.
- **Per-user settings + admin policy floor**: per-user settings (opt-out by default) at `/user-settings/redaction` with a live example preview, style options, language support and lock indicators, plus an admin governance floor at `/admin/redaction-policy` that can force categories and mandate censored exports. New Svelte panels (`ContentRedactionSettings`, `RedactionPolicySettings`), a "Redacting…" status chip with WebSocket auto-update, and an owner/admin "show original" reveal via `?redact=false` (audited; forced categories never reveal).
- **Search-snippet redaction** and **redact-before-LLM**, so summaries and LLM features never see unredacted text.
- **Offline redaction model pre-download**: `scripts/download-models.py` gains `download_redaction_models()` (GLiNER PII + toxicity classifiers), gated by `DOWNLOAD_REDACTION_MODELS`; `Dockerfile.prod` installs the `en_core_web_sm` spaCy pipeline for Presidio.

#### Cloud ASR

- **AWS Transcribe — full production support**: dual-credential support (encrypted `access_key_id` column + secret `api_key`, both AES-256-GCM; falls back to the boto3 default chain when blank), BCP-47 language mapping, up to 30 speakers (previously capped at 10), and a "Multilingual (code-switching)" catalog entry. New ASR config UI fields ("AWS Access Key ID" / "AWS Secret Access Key", write-only).
- **Speechmatics, AssemblyAI, Gladia, pyannote.ai verified end-to-end** and flipped from experimental to tested (see Fixed for the underlying corrections).

#### Watch sources auto-import (issue #26)

- **Automatic ingestion from watched sources**: configure a **local mounted folder**, an **S3-compatible bucket** (AWS / MinIO / Backblaze / Wasabi), or an **SMB/CIFS network share**, and OpenTranscribe polls each source on its own interval (Celery Beat orchestrator + per-source scan), copies new media into app storage, and runs the full transcription/diarization/embedding pipeline automatically. Originals on remote sources are never moved or deleted; local sources can optionally delete-after-import. Settings → Watch Sources (per-user, with an admin "all sources" view).
- **Three-layer deduplication on the imohash fingerprint**: within a source (path), across sources (content), and cross-pipeline against existing `media_file` rows (manual upload / URL import / prior watch import) — duplicates are recorded with a skip reason and linked to the existing file instead of re-importing.
- **Multi-part recording stitching**: split recordings (`name_P001.ext`, `name_P002.ext`, …) from dropped VTC/podcast connections are auto-detected by configurable regex, grouped within a time window, and stitched with ffmpeg (stream-copy when codecs match, re-encode fallback otherwise). Incomplete groups wait a bounded number of scans for missing parts before stitching what arrived.
- **Multi-provider email notifications**: optional SMTP / Microsoft 365 (Graph OAuth2) / Exchange notifications on scan completion, with BLUF summary and per-file status. Encrypted credentials (AES-256-GCM), never returned in API responses.
- **Folder browser, connection testing, and per-source file history** in the UI; new `docker-compose.watch.yml` overlay and `./opentr.sh start dev --with-watch` (plus `--with-smb-test` for a local Samba test share).
- **Event-driven watching for local folders (issue #294)**: `watch.fs_events_enabled` and the per-source **Watch for file-system events** checkbox previously did nothing — no `watchdog` observer existed anywhere, so an admin could enable them and still wait a full scan interval (15 min by default) with no explanation. They are now real: a supervisor in **celery-beat** watches each opted-in local source and dispatches the existing per-source scan seconds after a file lands. **The scheduled scan is untouched and remains the safety net** — this layer only makes it fire sooner, and every failure path degrades to polling rather than raising.
  - **Cross-platform by construction.** A plain `Observer()` would appear to work on a Linux host and silently do nothing for everyone else, because inotify does not see host-side writes through a macOS Docker bind mount (VirtioFS/gRPC-FUSE) or a Windows drive under WSL2, and never sees a remote writer on NFS/SMB/a NAS. `auto` mode therefore rejects those mount families by filesystem type and then **verifies delivery with a live probe** before trusting native events, falling back to watchdog's `PollingObserver` (works everywhere) otherwise.
  - **The UI says which mode a source actually got** — an "FS events" / "FS polling" / "Watch failed" / "Every N min" badge with the backend's own explanation on hover, published through Redis with a short TTL so a stopped beat container degrades the badge honestly instead of lying.
  - Bursts are coalesced into one scan per source (debounce = the file-stability window + 5 s, so the scan never races the "still being written" check), dispatches are Redis-locked, and the watched set is reconciled from the database every 30 s. Two new DB-backed admin settings: `watch.fs_events_mode` (`auto` | `native` | `polling` | `off`) and `watch.fs_events_poll_seconds`. `docker-compose.watch.yml` now mounts the watch folder into `celery-beat` as well.

#### Downloads & storage

- **Bounded derived-asset cache with retention + cleanup**: all regenerable derived assets (subtitle-embedded videos, extracted audio) moved under a `processed-videos/derived/` prefix governed by a single server-side MinIO lifecycle rule. Configurable retention (`DERIVED_CACHE_RETENTION_DAYS`, default 7) with DB-over-env so admin UI changes apply with no redeploy; new `cache_management_service`, admin API, and a "Media Cache" UI subsection (usage / retention / clear-now). A one-time startup pass reclaims legacy pre-prefix derived objects from upgraders.
- **Audio-only and original-media downloads**: the file-detail download button became a dropdown — video with subtitles, original video, and audio as MP3 / WAV / Original (lossless stream-copy via ffprobe-probed codec). `extract_audio()` with MinIO caching; `NoAudioTrackError` → HTTP 422.
- **Async bulk-export ZIP auto-expiry + browser E2E tests**: bulk-export ZIPs get a 24h MinIO lifecycle rule; new Playwright browser-driven E2E tests cover the download dropdown and gallery bulk export.

#### Platform

- **CPU-only install flag (`--cpu`)**: `setup-opentranscribe.sh`, `opentranscribe.sh`, and `opentr.sh` now accept a `--cpu` flag (and honor `OPENTRANSCRIBE_FORCE_CPU=1` for unattended installs) to explicitly opt out of the GPU compose overlay. Required on hosts where the NVIDIA Container Toolkit is detected by Docker but GPU passthrough is non-functional — e.g. WSL2 without a WSL-capable Windows NVIDIA driver, where auto-detection would otherwise enable the GPU overlay and cause celery-worker / celery-cpu-worker to fail at container start with `nvidia-container-cli: initialization error: WSL environment detected but no adapters were found`. The choice is persisted to `.env` as `FORCE_CPU_MODE=true`, so `./opentranscribe.sh start/restart/stop` continues to skip the GPU overlay without re-passing the flag. Default behaviour for GPU users is unchanged.
- **CPU-mode safe defaults and visibility**: builds on the `--cpu` flag with end-to-end CPU-mode awareness. The installer now writes `ENABLE_DIARIZATION=false` to `.env` whenever `DETECTED_DEVICE=cpu` (PyAnnote requires CUDA), prints a "CPU-Only Mode — Performance Notes" advisory in the install summary, and leaves the existing `select_whisper_model()` recommendation of `base` for CPU intact. The backend logs a single startup warning when a worker boots in CPU mode with a heavyweight Whisper model or diarization enabled. `GET /system/stats` now returns `device_mode`, `force_cpu_mode`, `whisper_model`, and `diarization_enabled` so the Settings → System Statistics panel renders a CPU-only advisory banner with the right "forced via flag" vs "no GPU detected — automatic fallback" subtitle. All 8 UI locales (en, es, fr, de, pt, ru, zh, ja) include the new strings. End-to-end testing plan documented at `docs/CPU_MODE_TESTING.md`.
- **Documentation**: new feature pages (boundary correction, content redaction), a developer guide for boundary correction, cloud-provider comparison + dataset-sweep results, and a consolidated `docs/market-research/` dossier.

#### Transcription engine & multi-GPU

- **Combined transcription engine**: a new `backend/app/transcription/engine/` orchestrator (`Engine` + `EngineConfig`, typed `JobSpec`/`JobResult`/`PreprocessResult`/`RawInferenceResult` dataclasses, a `MetricsCollector`, and `TranscriberBackend`/`DiarizerBackend` Protocol interfaces with a registry of faster-whisper / whisperx / cloud / pyannote backends). `pipeline.py` is now a thin shim delegating to `Engine.process()`, guarded by a byte-equal parity gate (`scripts/benchmark_engine_compare.py`) that asserts identical segments/language/overlap and embeddings within 1e-6 vs the legacy path.
- **Split-stage Celery pipeline + shared-volume WAV handoff**: engine `run_preprocess()` / `run_gpu_stage()` / `run_cpu_finalize()` stages; preprocess stages the 16 kHz WAV onto a shared `transcription-temp` volume so the GPU task mmap-loads it instead of re-downloading from MinIO, and waveform generation resamples that WAV (scipy `resample_poly`) instead of re-running FFmpeg.
- **Phase 4 multi-GPU split (`--with-gpu-split`)**: `ENGINE_GPU_SPLIT=true` routes ASR and diarization to separate `gpu-transcribe` / `gpu-diarize` Celery queues (new `celery-worker-gpu-transcribe` / `celery-worker-gpu-diarize` services under a `gpu-split` compose profile, activated via the new `./opentr.sh ... --with-gpu-split` flag).
- **DB-backed engine settings + admin Engine Configuration panel**: `EngineConfig.from_db_with_env_fallback()` reads `SystemSettings` `engine.*` keys (DB → env → default); new admin API `GET / POST(/update) / DELETE({key}) /api/admin/engine-settings` with db/env/default source badges and per-key reset, surfaced in a new Svelte Engine Configuration settings panel.
- **Engine metrics endpoint**: `GET /api/admin/engine-settings/metrics` returns per-worker Redis snapshots (GPU ready-queue depth, in-flight count, idle seconds, last-stage durations; 120 s TTL).

#### Hybrid mode & adaptive hardware

- **Hybrid mode — CPU transcription + GPU/MPS diarization**: auto-activates on macOS/MPS and on CUDA GPUs whose VRAM is too small for the configured model (batch=2 peak > 80% of total VRAM), unlocking 4–6 GB NVIDIA GPUs and Apple Silicon. Adds `should_use_hybrid_mode()` and a separate `diarization_device` on `TranscriptionConfig`. New env: `WHISPER_HYBRID_MODE` (auto|true|false), `WHISPER_HYBRID_CPU_MODEL` (default `small`).

#### Uploads & ingestion

- **Presigned direct-to-MinIO uploads + content-hash dedup**: opt-in `use_presigned=true` on `POST /api/files/prepare` returns a presigned PUT URL + task_id; the browser PUTs bytes directly (HTTP returns in ~100 ms, no multi-GB buffer in API heap) then calls the new `POST /api/files/complete`. Adds an `imohash_service` (constant-time fingerprint via three ranged MinIO reads) and a `MediaFile.imohash` column for dedup / artifact-cache keys / reprocess short-circuit; frontend SHA-256 moved to a web worker. The legacy multipart POST remains as a transparent fallback.
- **`celery-cloud-asr-worker` service**: the CPU worker is split into `celery-cpu-worker` (compute queues, concurrency 8) and a network-bound `celery-cloud-asr-worker` (queue `cloud-asr`, concurrency 16) so cloud-ASR jobs don't head-of-line-block local postprocess; added to every compose overlay.

#### Observability & resilience

- **End-to-end pipeline timing instrumentation**: `app.utils.benchmark_timing` captures 30+ wall-clock markers from HTTP ingress through async indexing into a durable `file_pipeline_timing` table, with admin endpoints `GET /api/admin/timing/{task_id}`, `GET /api/admin/timing`, and `GET /api/admin/timing-summary/recent`. Entirely gated on `ENABLE_BENCHMARK_TIMING` (zero overhead when off).
- **Orphan upload sweeper + retry-aware timing + error-path flush**: a new `cleanup.orphan_upload_sweeper` (beat every 15 min) reclaims PENDING `MediaFile` rows and MinIO objects orphaned by client disconnects (>30 min); failed pipelines now write a terminal `pipeline_error_end` marker and flush timing so they leave a durable row.
- **Scratch janitor**: an hourly `cleanup.scratch_janitor` purges per-file scratch dirs older than 1 h to keep crashed pipelines from filling the shared volume.

#### Summary prompt sharing (issue #78)

- **Admin prompt sharing completed — clone, attribution, audit, popularity**: any accessible summary prompt (system / shared / your own) can be cloned into your editable library via `POST /api/prompts/{uuid}/clone` and a Clone button in the "Shared by Others" section (clones count toward the 50-prompt cap). Shared prompts carry attribution — a new nullable `summary_prompt.shared_by` column (migration `v365`) records who flipped sharing on, distinct from the creator, surfaced as `shared_by_name` in the UI. `usage_count` now actually increments once per successful summarization on the resolved prompt, so the shared library's "popular" ordering and "most used" metric are meaningful. `PROMPT_SHARE` / `PROMPT_UNSHARE` / `PROMPT_CLONE` events flow through the audit log; clone strings are translated across all 8 locales.

#### Operations

- **Encrypted database backups**: `./opentr.sh backup --encrypt` pipes `pg_dump` directly into GPG symmetric AES-256 (the plaintext dump never touches disk); `./opentr.sh restore` transparently detects and decrypts `.gpg`/`.asc` backups. Plain backups now print a reminder that dumps contain all user transcripts in plaintext.
- **Multi-GPU pipeline split overlay (`docker-compose.gpu-split.yml`)**: a new opt-in overlay that runs transcription and diarization on **separate GPUs** for higher throughput on a 2+ GPU host. `./opentr.sh start dev --with-gpu-split` (alias `--gpu-split`) activates the `gpu-transcribe` / `gpu-diarize` worker services (already defined in the base compose under the `gpu-split` profile) and appends the overlay, which grants each worker a dedicated GPU reservation (`GPU_TRANSCRIBE_DEVICE_ID` / `GPU_DIARIZE_DEVICE_ID`). Docker remaps each reserved card to container index 0, so both workers run on `CUDA_VISIBLE_DEVICES=0` (same pattern as `--gpu-scale`). Pairs with `ENGINE_GPU_SPLIT=true`.
- **Deployment-configuration operations guide**: new `docs-site/docs/operations/deployment-configuration.md` documents every deployment type and its exact `./opentr.sh` command, the healthcheck/`start_period`/`depends_on` first-init model, the `pipeline_scratch` cross-worker handoff contract, the three GPU modes (single / dual / split), the security posture (loopback infra ports, `no-new-privileges`, secret generation), and the NAS/NVMe storage overlay.
- **Backend tests in CI + a canonical local test gate (issues #21/#123)**: GitHub Actions now runs the backend unit/API suite on every PR (fresh PostgreSQL service, CPU-only requirements) alongside the frontend vitest job; `./scripts/run-integration-tests.sh` is the canonical local pre-merge gate (the ungated suite plus every gated security suite in both FIPS modes plus integration-marked tests); MinIO/OpenSearch-backed tests auto-enable when the live dev stack is reachable and skip cleanly otherwise; and the Playwright E2E suite gained upload, search, settings, and transcript-editing coverage with a shared one-login-per-session auth state.
- **Engine benchmark suite (`./opentr.sh bench`)**: a durable end-to-end benchmark orchestrator that exercises the real stack in an isolated `otbench` compose project (frozen image code, fresh volumes — physically separate from live data), with single-file and queue-throughput phases plus a collate step, a fixed mixed benchmark corpus (`--corpus-file`, `--profile`, `--shuffle`), a resumable checkpointed GPU soak orchestrator with a watchdog for unattended auto-resume, dual-GPU phases, and a latency/contention collator view. An opt-in `FFMPEG_THREADS` cap protects low-core deployments from ffmpeg oversubscription. Collated results live in `docs/BENCHMARK_RESULTS.md`.

#### Pre-ship functional fixes

- **The Certificate Info panel in Settings never showed anything, for every PKI-authenticated
  user** (#397, #398). It read a user-store field nothing ever populated — the metadata lives
  behind a dedicated endpoint no call site used. The component now fetches its own data, gated
  to PKI logins so non-PKI sessions pay nothing.
- **A watch source's "skip files older than N days" setting couldn't be turned off.** The
  age-skip column had a database default that silently overrode an explicit "no limit" value
  from the UI, so a deployment intending to import everything kept silently skipping old files.
- **Four defects in the install/upgrade/model-download path.** The model downloader hardcoded
  the `:latest` backend image, silently defeating version pinning; its "models already exist?"
  prompt had no unattended guard, hanging an automated install indefinitely; its exists-check
  threshold contradicted the script's own definition of a partial download, so an interrupted
  download was reported complete and the user hit missing weights at first transcription
  instead of at install time; and the advertised model list had drifted, silently omitting the
  chat reranker and redaction models from the printed summary.
- **A quarantined (DMCA/abuse-takedown) file displayed the literal text
  "FileStatus.QUARANTINED"** in the gallery and file-detail UI instead of a real status (#301).
- **A WebSocket connection held a pooled Postgres connection, in an idle uncommitted
  transaction, for the connection's entire lifetime** — hours, for a normal browser tab. DB
  access was only needed for the few-millisecond auth check at the top of the handler; the idle
  transaction held whatever locks its snapshot acquired, which is what stalled a pending schema
  migration for roughly 15 minutes during development.
- **Uploading a file over 2 GB was accepted alone but rejected as "too large" when dropped
  alongside a second file** (#298). Two different maximums existed for the two code paths; both
  now read one 15 GB ceiling from a single source of truth.
- **A truncated SMB watch-source download was silently accepted as a complete file, transcribed,
  and stored with no trace above debug level** (#293). The byte-count mismatch was raised inside
  a `try` whose broad `except` (there to tolerate servers where `stat()` is unavailable)
  swallowed it. Because content-hash fingerprinting hashes the truncated bytes, a later
  re-import of the complete file wasn't recognized as a dedup case either.
- **Duplicate detection silently stopped working for uploads above ~4 GB**, with no error shown
  anywhere, on a UI advertising a 15 GB limit. The browser-side fingerprint worker hashed via
  `file.arrayBuffer()`, which Chrome throws `NotReadableError` on above roughly 4 GB; because
  hashing is optional and the failure was swallowed, the upload still succeeded and just skipped
  dedup — for exactly the largest files, where re-transcribing a duplicate is most expensive.
- **Cloud (pyannote.ai) and local diarization speaker-label normalization crashed with an
  unguarded `TypeError`, and AWS Transcribe access keys were not forwarded** (#299, #300).
- **Every PostgreSQL-backed panel on the Product & Usage Grafana dashboard rendered "No data"**,
  while the Prometheus ops dashboard worked. Grafana 12 renamed the built-in PostgreSQL
  datasource plugin id; the provisioning file and all 14 panel datasource references still used
  the old id, so panel query routing failed even though the datasource itself resolved.
- **The first-run setup wizard could block a user from changing their own password.** Clicking
  "Change my password" inside the wizard navigated to the Profile & Security page but left the
  wizard modal open on top of it, covering the form it had just navigated to.
- **A bulk-export could appear to hang forever with the ZIP already sitting ready in object
  storage.** A check→subscribe race let a worker publish its "completed" pub/sub event before
  any subscriber existed for it — the bulk-export twin of the already-documented download SSE
  lost-wakeup race, fixed the same way (re-check after subscribing).

### Changed

- **Removed the write-only `reactiveFile` store from the file-detail page (issue #338)**: `frontend/src/routes/files/[id]/+page.svelte` declared `const reactiveFile = writable(null)` — a page-local `const`, never exported — and wrote to it 13 times. Nothing subscribed: no `$reactiveFile`, no `.subscribe()`, no importer. A store with no subscribers does nothing on `.set()`, so all 13 calls were inert, and they actively misled review (a reader sees `reactiveFile.set(file)` after a mutation and concludes the UI will refresh). The real update path is the page's own `file` assignment/invalidation propagating to its children — which is what `frontend/src/components/fileDetail/CLAUDE.md` already documented as the pattern. The store, its 13 writes, the `setReactiveFile` member of `FileNotificationContext`, its 3 call sites in `$lib/fileDetail/notificationHandler.ts`, and the doc comment that described it as the websocket integration point are all gone. `setFile` is untouched — it performs the real `file = ...` assignment and is load-bearing.
- **Backend code-quality overhaul (maintainability, no behavior change)**: a sweep of the FastAPI backend with characterization tests as the regression net. **SQLAlchemy 2.0 typed models** — all 26 model files converted from legacy `Column()` to `Mapped[]`/`mapped_column()`, which let mypy see real column types and drop ~165 errors, and made 257 defensive `int(current_user.id)`-style casts provably redundant (removed); a `pg_dump` before/after diff proved zero schema change. **Blocking I/O off the event loop** — 17 `async` handlers that made synchronous MinIO/OpenSearch calls were either converted to `def` (FastAPI threadpools them) or wrapped in `run_in_threadpool`. **Endpoint dedup** — a message-parameterized `require_resource_owner` helper consolidated 17 copy-pasted ownership checks, a shared `paginate()` helper and an `ErrorHandler.internal_error()` replaced repeated boilerplate, all behavior-preserving and snapshot-gated. **Comprehensive endpoint test coverage** — characterization suites for all 39 previously-untested endpoint modules (~720 new tests across auth, files, speakers, settings, collaboration, and system/admin), with a byte-exact ownership-contract spec; coverage floor ratcheted 35 → 37 %. Celery task DB sessions standardized on `session_scope()`. **UUIDv7 generation** — all primary `uuid` columns now mint time-ordered RFC 9562 v7 identifiers (better index locality than random uuid4) via a small dependency-free generator; backward-compatible (existing rows coexist), with a defensive idempotent migration (`v368`) that converts any legacy `varchar(36)` uuid column to native `uuid` so older deployments upgrade without breaking.
- **In-place storage recovery / re-ingestion**: a new `python -m app.scripts.reingest_minio` registers media objects that exist in MinIO but have no database row — pointing each `MediaFile` at the existing object key (zero copy/duplication) and dispatching the standard pipeline — plus rate-limited yt-dlp metadata-only recovery for orphaned YouTube thumbnails. Built for disaster recovery where media survives but the database is lost.
- **Fresh / isolated deployments**: `./opentr.sh start dev --fresh <name>` runs a fully isolated stack (own compose project + volumes, NAS overlay never loaded) for safe experimentation; explicit `--nas`/`--no-nas` directives replace the silent auto-load; `.opentranscribe-live-data` marker files and a `data-paths` subcommand guard live bind-mounts against accidental cleanup.
- **Frontend modularity, quality & accessibility overhaul (issue #174)**: split the eight oversized Svelte components (`TranscriptDisplay`, `SettingsModal`, the file-detail / speakers / gallery routes, `Navbar`, `UserFileStatus`, `CollectionsPanel` — each 1,400–3,500 lines, ~19,000 lines combined) into focused single-responsibility children under new `transcript/`, `settings/`, `speakers/`, `gallery/`, `navbar/`, `fileStatus/`, `collections/`, and `fileDetail/` folders, with each route/parent kept as a thin coordinator and **no behavior, DOM, or visual change** (the eight shells dropped to ~10,400 lines, ~8,500 lines moved into focused children). Consolidated ~25 duplicated time formatters into `$lib/utils/formatting` (test-locked), extracted the client-side transcript export into a golden-tested `$lib/export` module, centralized `@keyframes` into `styles/animations.css` (with `prefers-reduced-motion`), deduplicated the collections create/edit modals, and added reusable, accessibility-correct UI primitives (`Tabs`, `Dropdown`, `Avatar`, `Badge`, `Chip`, `CopyButton`, `ExpandableSection`, `SearchableSelect`, `ConnectionStatusBanner`) plus a typed `clickOutside`/`apiError`/`focusTrap` toolkit. Stood up a **Vitest** unit/component harness (71 tests across 15 files) and wired **ESLint** (flat config) into pre-commit + CI; added a SvelteKit `+error.svelte` boundary, modal focus-trap / `aria-modal` / return-focus, icon `aria-label`s, and 17 per-folder `CLAUDE.md` docs. Verified per-commit by svelte-check (0 errors/0 warnings), `vite build`, the unit suite, and the live Playwright E2E suite.
- **Frontend type-safety, backend-leverage & resilience (issue #174, follow-on)**: enabled TypeScript `strict: true` and cut explicit `any` from 406→190 occurrences (catch blocks swept to `unknown` behind typed `getErrorMessage`/`getErrorStatus`/`getErrorCode` helpers); added an i18n key-parity checker (`npm run check:i18n`, all 8 locales) wired into CI. Pushed display shaping to the backend (thin-frontend): segments now carry an always-populated `resolved_speaker_name` and the API pre-computes `grouped_segments`, with the client retaining a fallback path so old payloads never break. Surfaced WebSocket reconnect state through a non-blocking status banner, and added an env-gated (`VITE_SENTRY_DSN`) error-reporting hook that is a lazy no-op by default (no dependency added to the home-label bundle).
- **Frontend dev-tooling & regression safety net (issue #174, follow-on)**: a bundle-size analyzer (`npm run build:analyze`, `rollup-plugin-visualizer`, gated so the default build is unaffected), dead-code detection (`knip`) and import-cycle detection (`madge`) wired as report-only CI steps, and removal of 3 orphaned modules they surfaced. Added **axe-core** accessibility assertions (baselined so only new serious/critical violations fail) and **Playwright visual-regression** screenshot baselines (light + dark, 4 primary surfaces) to the E2E suite, plus backend serializer unit tests for the new pre-shaped fields.
- **Unified upload finalization across both ingest paths**: the legacy multipart and presigned `/complete` routes now share one post-commit dispatch tail (`dispatch_upload_pipeline`: resolve per-file Whisper model → fire thumbnail → dispatch the transcription pipeline). This eliminated the hand-copied duplication that had let the two paths drift (the missing-thumbnail and missing-validation gaps). Extracted-audio uploads (client-side audio extraction) were also moved onto the presigned path with a legacy fallback, so all browser uploads share one consistent ingress. The dead `X-Extracted-Audio` header (never read by the backend; source metadata flows via the `extracted_from_video` body field) was removed.
- **Bulk subtitle export is now async + presigned**: `POST /api/files/bulk-export` (synchronous ZIP streamed through the API) is replaced by `POST /api/files/bulk-export/prepare` (returns a `job_id`) plus the SSE stream `GET /api/files/bulk-export-stream?job=<id>`. The ZIP is built on the `download` Celery worker, stored in MinIO, and delivered to the browser as a short-lived presigned URL — the API never proxies the archive bytes. This keeps bulk exports robust under concurrent users, backlog, and larger-than-expected batches, and reconnect-safe: a dropped EventSource still receives the result.
- **Media downloads moved off the API request path**: `POST /api/files/{uuid}/prepare-download` returns a ready presigned URL for passthrough/cache hits, else enqueues ffmpeg work; `GET /api/files/{uuid}/download-stream` is an SSE endpoint that pushes progress and the ready URL and re-checks the cache on reconnect. Media bytes now always stream directly from object storage (Range-capable), never through API container memory.
- **Presigned media URL lifetime raised to 6 hours** (`MEDIA_URL_EXPIRE_SECONDS` 300→21600) so a single URL outlives long viewing/labeling sessions of multi-hour files (previously 403'd mid-playback).
- **Engine Configuration admin UI trimmed to runtime-safe settings only**: removed `gpu_split` (deployment topology — hangs tasks without the `--profile gpu-split` workers), `precompute_vad` (unimplemented stub), and `shared_volume_path` (internal infra) from the admin API and panel. They remain env/deployment config. The panel now shows transcriber/diarizer backend plus the boundary controls.
- **Engine Configuration panel fully internationalized**: all infrastructure keys (titles, backend labels, Save/Reset/Saved, DB/Env/Default source badges) translated across all 8 locales at full key parity. The ASR provider dropdown now flags experimental/untested providers inline.
- **Single canonical `purge_media_file` for all delete paths**: every delete path (interactive single/force/bulk, N-day retention, orphan cleanup) now routes through one implementation that removes storage artifacts (original + thumbnail + derived cache), OpenSearch data (speakers v3/v4, transcript, chunks, summaries), the DB row, Redis state, and empty clusters — eliminating drift where retention deletes left the derived cache and orphaned data behind.
- **nginx**: dedicated no-buffering location for the SSE download/bulk-export streams (defined before `/api/`); the `/s3/` MinIO proxy brought to parity with `proxy_buffering off` + `proxy_max_temp_file_size 0` + extended timeouts for large presigned downloads.
- **Download spinner accuracy**: processed downloads now use `fetch()` so the button holds its "Processing…" state for the real ffmpeg duration, and backend errors surface as toasts; loading skeletons aligned with the search and profile layouts.
- **Dependencies**: `speechmatics-python` → `speechmatics-batch`; added `meeteval`, `presidio-analyzer`, `presidio-anonymizer`, `gliner`, `detoxify`; bumped `uvicorn`, `qrcode[pil]`, `onnx`, `yt-dlp`, `sentence-transformers`, `google-cloud-speech`, and `mypy`. Frontend dependency bumps (`@typescript-eslint`, `vite-plugin-pwa`, `devalue`; svelte pinned to avoid a 5.56.x parser regression) and `npm audit fix` to 0 vulnerabilities. CI action bumps (codeql, setup-python, cache, upload-pages-artifact, setup-buildx, anchore/scan). Dependabot reconfigured to weekly grouped updates (one frontend + one backend PR). July 2026 refresh: ~42 backend bumps (fastapi capped `<0.137` — 0.137+ breaks templated-route labeling in the observability middleware, tracked follow-up; presidio pins constrain numpy `<2.5` and cryptography `<47` transitively) and 12 frontend bumps (axios 1.18.1, dompurify 3.4.11, SvelteKit 2.69) with known-breaking majors held by policy (`@eslint/js` 10, `@types/node` 26, torch/torchaudio managed by hand, typescript-eslint trio pending the eslint 10 migration); CI + tooling aligned to the Python 3.13 runtime.
- **Model-aware Whisper batch sizing**: `_get_optimal_batch_size(model_name)` now caps batch at empirically validated thresholds per model and GPU class (from the Phase B VRAM study), replacing over-aggressive defaults (e.g. 32 on an A6000) that burned VRAM for no throughput gain (throughput plateaus at batch≈8).
- **GPU concurrency auto-detection recalibrated**: the `GPU_CONCURRENT_REQUESTS=auto` formula changed from `(vram−6000)//1000` (cap 4) to `(vram−7000)//4000` (cap 12), based on a measured ~7 GB warm baseline + ~4 GB/task — an RTX A6000 now runs up to 10 concurrent transcriptions (was capped at 4).
- **Diarization embedding batch pinned at 16**: the per-run VRAM-budget knobs were replaced by a fixed `EMBEDDING_BATCH_SIZE = 16` that forces the fork's auto-scaler off (`PYANNOTE_FORCE_EMBEDDING_BATCH_SIZE=16`), giving a predictable ~1 GB peak so ~25 diarization pipelines fit on an A6000.
- **Eliminated duplicate upload I/O**: the source file was previously fetched from MinIO up to four times per video. Waveform now reads the preprocessed 16 kHz WAV (~10× smaller), metadata extraction runs `ffprobe` against the presigned URL (reads ~1 MB of container headers via `extract_media_metadata_from_url`) instead of re-downloading, and same-host workers hand off the WAV via a shared scratch volume (atomic rename + hard-link) with a MinIO fallback for multi-host.
- **Deferred thumbnail + full-document indexing off hot paths**: thumbnail generation (3–8 s FFmpeg) now dispatches to a task after the DB commit, and full-document transcript indexing moved onto the embedding worker, so completion fires sooner.
- **URL-ingest (yt-dlp) speed parity**: YouTube/URL ingestion now mints the task_id at entry, threads timing markers, computes imohash for dedup, defers the thumbnail FFmpeg to the queue, and runs preprocess sub-stages in parallel — matching the direct-upload fast path.
- **Upload critical-path compressions**: a single DB commit on intake (flush instead of double commit/refresh), streaming magic-byte validation (validate the first chunk before reading up to 50 GB), and a duplicate short-circuit on the legacy POST path before reading bytes.
- **Pandas removed from the diarization path**: `diarizer` / `speaker_assigner` / `reprocess` refactored onto a numpy-backed `DiarizeResult` dataclass.
- **Tunable infra knobs**: SQLAlchemy pool now configurable (`DB_POOL_SIZE` default 20, `DB_MAX_OVERFLOW` default 40, was hard-pinned 10/20); MinIO large uploads use 64 MiB multipart parts; OpenSearch refresh is suspended during large bulk loads (`SEARCH_LARGE_TRANSCRIPT_CHUNKS` default 500); download worker concurrency default raised 3→5.
- **Reference-counted frontend scroll-lock utility**: a new `src/lib/scrollLock.ts` (`lockScroll` / `unlockScroll` / `resetScrollLock`) replaces ad-hoc `document.body.style.overflow` toggling across modals and panels, fixing races where one modal closing unlocked the body while another was still open.
- **Datastore healthcheck grace periods (`start_period`)**: postgres, minio, and opensearch each gained a 60 s healthcheck `start_period` (retries 5→10/20) so a slow first-init on a large bind-mounted data dir (cluster create + WAL, bucket/IAM reconciliation, JVM boot + shard recovery) doesn't cross the retry budget, get marked unhealthy, and abort every `depends_on` service. The redis healthcheck was tightened (timeout 30s→5s, retries 50→10), and the GPU/CPU/embedding/model worker `start_period`s were raised 40s→120s to cover cold model preload + first-run HuggingFace download.
- **`celery-nlp-worker` now waits on `backend: service_healthy`** like every other worker (was `depends_on: [postgres, redis, minio, opensearch]` by start order only), so it can no longer race the schema before migrations have applied on first start.
- **`./opentr.sh start`/`reset` now block on health (`up -d --wait --wait-timeout 700`)**: a container that is created but never becomes healthy now surfaces as a non-zero exit with `ps` + recent logs, instead of the old optimistic "✅ Services are starting up." The success message changed to "✅ Services are up and healthy." `opentr.sh` also adopted `set -uo pipefail` (with the genuinely-optional `.env` vars pre-defaulted).
- **`./opentr.sh` worker lists completed**: `restore`, `restart-backend`, and the worker stop/start lists now include `celery-redaction` and `celery-cloud-asr-worker` (previously omitted, so those workers weren't stopped before a DB restore or restarted with the backend). The bench flow replaced blind `sleep`s with a deterministic backend-health poll.
- **`reset prod --build` forces no-pull at `up` time** (`--pull never`), matching `start prod --build`, so a locally-built image isn't clobbered by a Docker Hub pull when a `build:` context is also present (`pull_policy: never` isn't reliably honored in that case).

- **Backend overhaul — bugs caught by the new characterization test suite**: the ~720-test endpoint-coverage program (see Changed → "Backend code-quality overhaul") surfaced and fixed several latent defects. Malformed UUIDs on `DELETE /files/{uuid}` and on ten `speaker_clusters` routes flowed straight into a `uuid`-typed `WHERE` clause, producing an unhandled **500 plus a poisoned request transaction** instead of a clean 404 — now guarded. `list_speaker_profiles` wrapped its body in a bare `except` that **masked an intentional 403 as a 500** when filtering by another user's collection. The topics retroactive-auto-label status endpoint **500'd when Redis was unreachable** instead of degrading. Two routes were dead/unreachable and removed: `GET /api/llm/providers` (always 500 — called a nonexistent method) and `GET /api/files/analytics` (shadowed by the UUID-typed file route). A test-only defect was also fixed: API tests that dispatched Celery tasks were publishing into whichever Redis answered on the host's default port — `SKIP_CELERY` now covers the dispatch path.
- **Anonymous page loads triggered a spurious logout cascade**: since the httpOnly-cookie auth migration, the SPA's `initAuth` probed `/auth/me` on every page load; for anonymous visitors that guaranteed a 401 console error, fired a pointless `POST /auth/logout`, and — worst — `abortAllRequests()` cancelled the login page's own `getAuthMethods` fetch, so PKI/Keycloak/LDAP buttons could silently fall back to defaults. New `GET /api/auth/session` probe returns 200 for everyone (`authenticated` / `refreshable` flags), `initAuth` restores expired sessions silently via the refresh cookie instead of bouncing to login, and `fetchUserInfo` no longer has logout side effects.
- **Gallery hover-prefetch 404s for non-playable files**: hovering (or landing with the cursor over) a gallery card for a file in `error`/`processing` status prefetched a video stream URL that can't exist, logging a console 404 on every gallery visit. Prefetch now skips the stream URL unless the file is `completed`.
- **Flower healthcheck always unhealthy**: the flower service inherited the backend image's Docker HEALTHCHECK (API on :8080, which flower doesn't serve). A flower-specific compose healthcheck now probes its own unauthenticated `/flower/healthcheck`.
- **Dev-stack auth security limits vs the e2e suite**: the dev overlay (`docker-compose.override.yml`, never loaded in prod) now relaxes the per-IP auth rate limit and account-lockout threshold (`DEV_*`-tunable) so the 270+-test Playwright suite isn't throttled or lockout-poisoned; production keeps the strict `.env` defaults. E2E negative-login tests also switched to a nonexistent account so they can never lock the real admin account.
- **Thumbnails missing on presigned uploads + live gallery update**: video files uploaded via the presigned path (`/files/prepare` → direct MinIO PUT → `/files/complete`) never got a thumbnail because the dispatch lived only in the legacy multipart handler, so gallery cards stayed blank. `/files/complete` now dispatches thumbnail generation (extracted into a shared `dispatch_thumbnail_for_video` / `dispatch_upload_pipeline` used by **both** ingest paths), and `generate_thumbnail_task` emits a `file_updated` WebSocket event with a presigned `thumbnail_url` so the card swaps in the thumbnail **live during processing** instead of only on a full refresh.
- **Orphaned PENDING rows from failed presigned PUTs**: if the browser's direct-to-MinIO PUT never completed, `/files/complete` returned 400 but left a stuck PENDING row in the gallery. It now deletes the orphaned row (parity with the legacy path's failure cleanup).
- **Latent Redis pub/sub subscriber death (broke ALL realtime notifications)**: the WebSocket notification subscriber died on the first idle read timeout and never recovered, silently breaking transcription progress and all WebSocket updates. It now runs in a supervised reconnect loop with exponential backoff, treats idle `get_message` timeouts as benign, and uses `health_check_interval` + socket keepalive.
- **Video player presigned-URL refresh interval**: the file-detail and search-preview players refreshed the presigned playback URL on a hardcoded 5-minute timer regardless of the URL's real lifetime (`MEDIA_URL_EXPIRE_SECONDS`, 6h by default), needlessly re-fetching and re-setting the video `src` mid-playback. The players now use the URL's actual expiry returned by the backend.
- **Speechmatics diarization**: the deprecated `speechmatics-python` SDK returned transcripts with no speaker labels; migrated to `speechmatics-batch` (async `AsyncClient`, `submit_job`→`wait_for_completion`, parsing `results[].alternatives[0].speaker`). Speaker labels are now returned correctly.
- **AssemblyAI + Gladia end-to-end**: AssemblyAI switched to the required `speech_models` list and trimmed to working models; Gladia upload fixed to send a filename + content-type multipart part.
- **pyannote.ai transcription parsing**: word tokens are keyed `"text"` (the parser read `"word"`, returning empty words) — fixed, with API-body error surfacing added.
- **MinIO `delete_prefix`**: used the wrong `DeleteError` attribute (`.object_name`) that would raise `AttributeError` while logging a failed bulk delete — corrected to `.name`.
- **Derived-cache orphan leak**: `delete_media_file` now clears the file's derived cache and audio variants (not just video).
- **mypy 2.x strictness**: widened `upload_file_to_storage` to accept `bytes | bytearray`; annotated `.first()` results in `auto_label_service`.
- **Scratch volume ownership**: the `pipeline_scratch` named volume was root-owned while workers run as UID 1000, so `is_scratch_available()` returned False and every upload silently fell back to MinIO, defeating the shared-memory handoff. `./opentr.sh` now chowns it to 1000:1000 (and `rebuild-backend` also rebuilds `celery-cloud-asr-worker`).
- **Split-stage path leaks**: plugged a WAV cleanup leak and sanitized the `task_id` filename in the split-stage path; plumbed `asr_model` through `diarize_gpu_task`; tightened the Whisper→diarization handoff cleanup.
- **First-init datastore race left containers stuck "Created"**: on a fresh start against a large bind-mounted data dir, a slow datastore init crossed the healthcheck retry window, compose marked the datastore unhealthy, and every `depends_on` service was aborted before it ever started (symptoms: containers stuck `Created`, "relation does not exist" against a half-built schema). Fixed by the healthcheck `start_period` grace periods (see Changed) and by dropping the legacy `init_db.sql` mount from the NAS overlay — schema is built by Alembic/Python on backend startup, and the redundant init script only slowed the first boot that triggered the race.
- **Several broken deployment types repaired**:
  - **gpu-split**: the `gpu-transcribe` / `gpu-diarize` workers had no image/build in the dev or prod overlays, so `--with-gpu-split` couldn't start them. Added image/build/volumes (mirroring `celery-worker-gpu-scaled`) to `docker-compose.override.yml` and `docker-compose.prod.yml`, plus the new `docker-compose.gpu-split.yml` reservation overlay; `CUDA_VISIBLE_DEVICES` for both split workers fixed to `0` (the reserved card's in-container index).
  - **offline & bench**: both were missing the required `celery-redaction` service (redaction detection runs on every transcript), so those stacks would never process redaction. Added it to `docker-compose.offline.yml` (with HF cache + `HF_HUB_OFFLINE=1`) and `docker-compose.bench.yml`.
  - **lite**: the cloud-ASR worker was defined as a brand-new `celery-cloud-worker` service (duplicating ~30 hardcoded env vars that drifted from the base, plus referencing a bad `external` network) instead of overriding the base `celery-cloud-asr-worker`. Renamed to override the base service and inherit its connection/credential env, and removed the broken external-network block.
  - **pki-dev**: documented and fixed the compose chain (the dev override is required for the non-frontend/backend services and the shared network), resolved a host-port clash with Vite/docs (PKI plain-HTTP now publishes on `PKI_HTTP_PORT`, default 5187; mTLS stays on `PKI_HTTPS_PORT`/8443), and removed the stray private bridge network so it joins the stack's default network.
- **`pipeline_scratch` cross-worker handoff missing on several services**: the scaled GPU worker (override/prod/offline) and the GPU-split workers lacked the `pipeline_scratch:/scratch/opentranscribe` mount that the other transcription workers use to read the CPU-staged preprocessed WAV. Without it the worker can't see the handoff and silently falls back to re-downloading each file from MinIO. Mount added everywhere a transcription worker runs.
- **Aux overlay networks (ldap/keycloak/smb) hardcoded the project name**: the test-IdP overlays joined an `external` network literally named `transcribe-app_default`, so they failed to attach for any clone whose compose project name wasn't `transcribe-app`. Replaced with the project-agnostic `default` network named `${COMPOSE_PROJECT_NAME:-opentranscribe}_default`.
- **Setup-script LLM API keys silently discarded**: `setup-opentranscribe.sh` wrote LLM keys with `sed` patterns that targeted commented placeholder lines (`# OPENAI_API_KEY=...`); when the line wasn't in the expected commented form the substitution was a no-op and the key was lost. Rewritten to use an `_upsert_env` helper that sets the value whether the key is present, commented, or absent.

### Performance

#### Backend request-path hardening (issue #284 Phase 2 — A2.4–A2.8)

- **Blocking work no longer runs on the event loop**: ~30 API handlers were declared `async def` with no `await` anywhere in their bodies — only synchronous SQLAlchemy, Redis reads and Celery dispatch — so each one held the asyncio loop for the whole request and stalled every other request the process was serving, WebSocket traffic included. They are now plain `def`, which FastAPI dispatches to Starlette's threadpool: **all 13 collection handlers, all 8 topic handlers, the 9 async task-system handlers** (plus their nested `BackgroundTasks` callables), and **`POST /files/process-url`**, whose body runs yt-dlp's synchronous `extract_info` — a full metadata fetch against YouTube/Vimeo/… that can take tens of seconds. Responses, status codes and payloads are unchanged; a new AST-based test fails the build if an awaitless `async def` handler reappears in those modules.
- **Speaker merge and profile rename return immediately**: `POST /speakers/{uuid}/merge/{target}` used to average both voiceprints in OpenSearch, clear the MinIO video cache for both files, delete the source document, recompute *each* profile's consolidated embedding (one kNN read per profile member) and refresh analytics for both files — **9+ OpenSearch round trips minimum, ~17 ms each measured against the dev cluster** — all before answering. That tail now runs in a new `process_speaker_merge_background` Celery task on the CPU queue. `PUT /speakers/{uuid}` with `profile_action="update_profile"` likewise deferred its per-linked-speaker OpenSearch fan-out to the existing speaker-update task, and its duplicate profile-embedding recompute (the background task already performed the identical recompute) was deleted. Postgres stays synchronous in both paths, so responses remain authoritative.
- **Media-file formatting validates once**: `FormattingService.format_media_file` ran two full Pydantic passes plus a dump per row (~200 validations for a 100-item gallery page); it now validates once and applies the pre-formatted display fields with `model_copy(update=...)`. Measured **7.02 ms → 4.92 ms median per 100-row page (−30%)** with byte-identical JSON output. `format_transcript_segment` was measured too and deliberately left alone — the same change there was inside the noise (44.6 → 44.2 ms per 1000 segments).
- **Upload prep batches its lookups**: `add_file_to_collections` and `add_tags_to_file` issued two queries per named collection/tag. Both now resolve with `IN (...)`: **6 collections 12 → 2 SELECTs**, **5 tags 10 → 3**, and 20 tags still costs 3. The same helpers back yt-dlp playlist ingestion and watch-source auto-import, which call them once per imported file.

#### Frontend request-path & bundle hardening (issue #284 Phase 2 — A2.1-A2.3)

- **Locale bundle no longer ships all 8 languages to every visitor**: locale data was
  static-imported into one ~2.1 MB (527 KB gzip) chunk sitting in the entry graph, so every
  visitor downloaded every language to read one. Locales now load per-language, fetched on
  demand and merged in before rendering starts (no flash of unstyled content, since rendering is
  already gated behind locale initialization). Measured first-paint JS (entry + layout + home
  route): **4,283,527 B → 2,089,254 B raw (−51%), 1,120,007 B → 591,848 B gzip (−47%)**, plus one
  lazily-fetched ~242 KB locale chunk.
- **Transcript reading-progress no longer re-queries the DOM on every scroll event**: an
  unthrottled scroll handler ran a full-list DOM query plus a forced-layout read on every event.
  Replaced with an `IntersectionObserver` over the same rows (no scroll listener, no DOM query,
  no forced layout); both segment lists are now keyed, fixing a second latent bug where unkeyed
  pagination re-patched every row instead of appending and could attach edit/highlight state to
  the wrong segment.
- **The FFmpeg client-side wrapper no longer loads on every gallery visit**: it was
  static-imported into the home-route bundle for an opt-in, rarely-used video→audio extraction
  path. Now a dynamic import behind first use, in its own 13 KB chunk.
- **Video-file hashing during audio extraction moved off the main thread**: extraction had its
  own hashing call on the whole file buffer — on the largest files the app accepts (up to 15 GB
  video), risking an allocation failure and freezing the tab for the hash duration with no
  progress indication. Now reuses the existing worker-based hashing path uploads already use.
- **Long transcripts skip layout/paint for off-screen rows**: `content-visibility: auto` on
  transcript segments, chosen deliberately over JS windowing so infinite-scroll, search-scroll-to,
  seek-to-playhead and highlight-flash keep working unchanged.

#### Other

- **Backend read-path query reduction (measured)**: the new `db_queries_per_request` instrumentation surfaced duplicate queries on hot paths, which were then eliminated — file detail **18 → 11** queries (−39%) and the segments endpoint **13 → 6** (−54%). The dominant win was the content-redaction admin policy load going from 8 sequential `get_setting` SELECTs to a single batched `get_settings_map` SELECT (it runs on every transcript read), plus `selectinload`/`joinedload` on the speaker-and-profile relationships. `EXPLAIN` confirmed every hot lookup is already indexed, so no new index was warranted.
- **In-process settings cache**: a TTL cache (`SETTINGS_CACHE_TTL`, default 30 s) fronts `SystemSettings` reads with bust-on-write across every writer; **Redis read-side caching** is enabled for the tag list (the one provably-safe, user-keyed surface) with a full invalidation audit that also closed previously-missing tag/speaker cache-busting on several mutation paths. Cache hit/miss is exported as `cache_operations_total`.
- **Settings reads batched app-wide**: `get_settings_map` (one SELECT for N keys) adopted in the redaction, backup, watch, user-settings, and engine config paths.
- **Backend Docker image slimmed ~820 MB** (9.68 GB → 8.86 GB): removed `triton` (~540 MB) and the `gcc`/`g++` toolchain tied to opt-in `torch.compile` (~150 MB) from the runtime stage, dropped `pytest` from runtime requirements, and removed the direct pandas dependency.
- **Removed the TensorRT pip dependency + `LD_LIBRARY_PATH` entry (−4.5 GB)**: the Phase 6.3 TensorRT execution-provider experiment never produced an end-to-end win (per-shape engine-rebuild storms on pyannote), so the image returned to its pre-spike size. The ONNX Runtime CUDA EP is retained.
- **ONNX Phase 6.2 — CPU execution-provider integration**: the one shipping ONNX win, giving 1.87–2.12× on the CPU-only tier (the CUDA / CoreML / TensorRT EPs regressed and were not shipped).
- **Measured end-to-end throughput (engine benchmark)**: with this release's pipeline work, a single RTX A6000 sustains **45.9× aggregate realtime** at concurrency 4 on a bursty mixed corpus (~12× per file), and a dual-A6000 host clears a 58-hour mixed corpus in **~43 minutes (81.3× aggregate realtime)**. The concurrency-4 plateau is tail-limited (a few long files dominate the tail), not a compute ceiling. Full sweeps and methodology: `docs/BENCHMARK_RESULTS.md`.
- **Local diarization accuracy ties the best commercial engine**: on the hand-labeled reference clip, the local pipeline with the default-on boundary smoother reaches **0.27% WSER at ~41× realtime** — tied with the best of six commercial cloud engines (Gladia) and ahead of AssemblyAI, Speechmatics, AWS Transcribe, pyannote.ai, and Deepgram, offline and free (`docs/diarization-boundary-results/cloud-comparison.md`).

### Removed

- **Legacy byte-proxy media endpoints (breaking change)**: the deprecated `GET /api/files/{uuid}/video`, `/simple-video`, `/content`, `/download`, and `/download-with-token` endpoints have been removed. All media now streams directly from object storage via short-lived presigned MinIO URLs — playback uses `GET /api/files/{uuid}/stream-url` and downloads use `POST /api/files/{uuid}/prepare-download` (file-detail dropdown). Presigned URLs support HTTP range requests natively, so video seeking is unaffected. `GET /api/files/{uuid}/thumbnail` is retained as a resilient fallback for when presigned thumbnail minting fails. External API consumers that linked the removed routes should switch to the presigned-URL endpoints.
- **Engine Settings keys `gpu_split`, `precompute_vad`, `shared_volume_path`** removed from the admin API/panel (now env/deployment-only or unimplemented).
- **Per-GPU diarization VRAM-budget env vars** (`DIARIZATION_VRAM_BUDGET_MB`, `DIARIZATION_MIXED_PRECISION`, `DIARIZATION_ONNX_CPU`) removed, superseded by the fixed batch-16 policy.
- **`docs/performance-whitepaper/` untracked** (main.tex + main.pdf): WIP pending human review; remains on disk and in `.gitignore`.

### Fixed

#### Chat: bugs the E2E suite could not see until it had a model to talk to

Wiring the mock LLM into `backend/tests/e2e/test_chat.py` made its streaming tests run for the first time — they had been self-skipping without a provider while the file still reported green. They immediately found three real defects:

- **Editing a question did nothing.** `ChatThread` forwarded `regenerate` and `retry` up from `ChatMessage` but not `edit`, so the event died mid-chain: the editor closed, the question stayed as it was, and no request ever reached the backend.
- **Stop leaked a concurrency slot.** Releasing the slot from the wrapping generator's `finally` does not survive Starlette tearing that generator down on client disconnect — exactly what Stop and a closed tab do — so two aborted generations consumed both slots and locked the user out of chat. The release now runs inside `stream_reply`'s own shielded `finally`, in its own `finally` so a failing finalisation cannot skip it.
- **Concurrency slots could never recover.** The cap was a single counter whose TTL was refreshed on every acquire, so a slot leaked by a died-mid-stream request never aged out for an active user: usable concurrency degraded 2 → 1 → 0 permanently. Slots are now tracked individually and pruned by age, release is idempotent, and the stale-slot window is 5 minutes rather than 15. An upgraded deployment's legacy counter is retired on first contact — every sorted-set command against it raised `WRONGTYPE`, which the fail-open handler would have swallowed while silently disabling the cap.

Also fixed: the E2E API session never sent a CSRF token, so every mutation returned 403 — which broke arranging test state *and* `cleanup_conversations`, meaning each run had been leaving its conversations behind in dev data.

#### Chat: no LLM configured is no longer reported as a failure

- **Topic extraction and summarization notified per file when no provider was configured.** Having no LLM is a deployment choice, not a task outcome, so a user who simply had not set one up got a warning on every recording for something they could not act on from a notification — burying real failures. Topic extraction was worse: it announced "Preparing AI analysis…" *before* checking for a provider, and that notification is progressive, so it sat unresolved until a second replaced it — two entries per file for work that never started. The availability check now runs before anything is announced. Summarization still records `summary_status = "not_configured"` for the file detail page; only the push notification is gone. A configured provider that errors or returns nothing still notifies.

#### Chat: reranking no longer disables itself permanently

- **One transient failure retired the reranker for the process lifetime.** `get_reranker` set its "attempted" flag *before* the load, so a container starting before its model-cache volume was mounted ran unranked retrieval for its whole life, signalled by a single warning. It now retries on a cooldown, so a cache that appears later is picked up without a restart.

#### Chat settings and navigation

- **Chat occupied two sidebar rows** in Settings ("Chat" and "Chat & RAG"), leaving users to guess which held the knob they wanted. They are now tabs behind one entry, user defaults first and platform tuning second. The admin tab is server-gated, not merely hidden.
- **Global form CSS leaked into three chat surfaces**: checkboxes stretched to fill their row (733px in the settings modal), pushing labels to the far edge, and sidebar action icons collapsed to zero width inside inherited button padding, leaving shadow-only rectangles. Selected conversations also read as two colours in dark mode, where a global `button:hover` painted `rgba(255,255,255,0.1)` across only the title's width of a tinted row.
- **Plural labels rendered as "12 source"** — the keys used i18next v3 `_plural` suffixes on a v25 install, which silently falls back to the singular. Migrated to `_one`/`_other` across all eight locales.
- **The primary navigation kept a two-page shape.** Gallery appeared only when you were elsewhere, labelled "Back to Gallery", so the item set changed between routes and links shifted under the cursor; Gallery could never show an active state, and Search had no active binding at all. All four destinations are now permanent with `aria-current="page"`.

#### Redact-before-LLM now fails closed on every path that reaches a provider

- **`redact_before_llm` was inert on three of the four paths that send transcript text off-box.** Speaker identification passed no redaction config at all, `build_speaker_segments` sent raw `text[:200]`, and topic extraction sent raw text — so a user with the setting enabled still had unmasked transcript content posted to their provider. Summarization honoured the setting but swallowed config-resolution errors into "no policy".
- **The dominant leak was structural, not a coding slip.** Redaction detection is dispatched from the *same* post-processing step that dispatches summarization, speaker ID and topic extraction, onto a *different* queue, with nothing ordering them. `mask_segment` called with an empty span list masks nothing and returns its input — so those tasks routinely "masked" a transcript whose spans were still NULL and sent it verbatim. The call site looked correct; only the file's `redaction_status` could reveal otherwise.
- All four paths now resolve masking through a single guard (`services/redaction/llm_guard.py`) that gates on detection having completed, defers the task while a scan is in flight (dispatching one itself if the file was never scanned), and refuses to send when detection failed. `transcript_builders` fails closed — a masking error substitutes a placeholder, never the original text — and masks *before* truncating, so a 200-character window cannot slice a mask open.

#### Chat answers were capped below the configured budget

- `LLMConfig.response_tokens` was never assigned, so every request sent `max_tokens=4000` while callers — chat's prompt budget and summarization — reserved the *derived* value of up to 16,384. On a large-context model the prompt was under-filled by up to ~12k tokens of excerpts it had room for, and answers were capped lower than intended. The service now keeps the two in sync.

#### Stale default models

- The Anthropic default fell through to `claude-3-haiku-20240307` (deprecated) because no values file pins a model; it is now `claude-haiku-4-5`. `OPENROUTER_MODEL_NAME` likewise moves from `anthropic/claude-3-haiku` to `anthropic/claude-haiku-4.5` — note OpenRouter's slug uses a **dot** where the first-party ID uses dashes.

#### Chat: additional pre-ship defects

- **An answer with excerpts trimmed to zero by the token budget rendered as an ordinary,
  unqualified answer** (#384), with no indication it was ungrounded.
- **Citations could be shown for excerpts the model was never actually given.** The excerpt
  budget was computed *after* the citation list was already built, so when the budget resolved
  to 0 the prompt fell through to a bare question while numbered citations were already on the
  wire — reachable in ordinary use on small local models with a modest context window.
- **Scoping a chat conversation by tag silently dropped shared recordings that matched it.**
  Tag scoping filtered on the caller's own ownership, unlike collection scoping (which already
  resolved through the accessible-files permission check), so a tag spanning files shared with
  the caller was silently truncated to only their own.
- **Chat could keep citing a file after it was deleted or quarantined**, for up to the
  retrieval cache's 5-minute lifetime. A corpus-version marker mixed into the cache key now
  invalidates on every index write/delete.
- **Sending a chat message threw an unhandled error and did nothing, on any deployment served
  over plain HTTP that isn't `localhost`** — a non-secure browser context, where
  `crypto.randomUUID` doesn't exist. Also fixed: an invisible chat send-icon, and the same
  secure-context gap in three other components' clipboard calls.
- **Chat showed "Connect an AI provider to start chatting" even with a fully working, verified
  LLM configured**, whenever the user logged in without a full page reload (the normal path) —
  the status store only initialized once per browser session, before any login.

#### Transcript and speaker curation now render a single source of segment data (issue #352, PR #356)

- **Renaming a speaker saved to the database and then did nothing on screen** — only a full page reload showed the new name. Editing a segment's text and reassigning a segment's speaker were broken the same way, and both failed silently. The page rendered from `file.grouped_segments`, whose `GroupedTranscriptSegment` schema **embedded a full copy of every segment it grouped**, while every optimistic update patched `file.transcript_segments` — a different set of objects. Groups now carry `segment_uuids` and `TranscriptDisplay` resolves them against the flat list, so there is one segment object per segment and a patch cannot miss it. The payload shrinks rather than doubling, and the client-side grouping fallback is deleted (a second implementation of the grouping rule is what let the two representations diverge unnoticed). Measured on the dev stack with the write stubbed at the network boundary: before, **0 of 28 labels repainted, ever**; after, **28 of 28 in ~31 ms** (37 ms dark) after the PUT resolves. All segment mutations now go through `$lib/fileDetail/segmentSync`.
- **Files over 500 segments never rendered past the first page**: `GET /files/{uuid}/segments` returned no grouping, so infinite scroll advanced its "N of M loaded" counter while rendering nothing and jump-to-timestamp scrolled to a row that was never mounted. Verified on a 732-segment file: **500 rendered before, 732 after**. The endpoint now serves grouping (O(n) over already-materialized objects — no new query, so its two-query profile is unchanged), with `start_segment_index` made global (it restarted at 0 per page, which inverted the reading-progress bar) and `segment_limit` bounded at 2000 (a jump to the end of a 50k-segment transcript requested the whole thing in one call; the SPA now pages in a loop). The endpoint also gains a `TranscriptSegmentsPage` response model — it was an untyped `dict[str, Any]`.
- **An overlap run split across a page boundary could take down the entire transcript list.** Both halves carry the same `overlap_group_id`, and group rows were keyed by it — a duplicate key makes Svelte throw at render time, killing the whole list rather than one row. Reproducible on real data. Rows are now keyed by their first segment's uuid, the halves are stitched on append, and `TranscriptDisplay` claims each segment for exactly one group while resolving references, so the invariant holds for every payload source (initial load, refetch, redaction reload, pagination) rather than the pagination path alone. Note this is **not** a backend defect: every individual response is internally consistent; the collision exists only in the combined client-side list.
- **The `speaker_processing_complete` websocket handler was dead code**: `stores/websocket.ts` dispatched the event and returned before the notification store, and nothing listened for the resulting CustomEvent, so labels auto-applied to other speakers required a manual reload and the "auto-applied to N other speakers" toast had never fired for any user. Now wired up and gated to the current file, with the unreachable handler branches removed.
- **A failed speaker save left the new name on screen looking saved.** It is now reverted (`speakerProfile.errorWithLocal`, "changes are saved locally only", replaced by `saveFailedReverted` across all 8 locales; the dead `speakerProfile.localOnly` removed). Bulk save moves from `Promise.all` to `allSettled` and restores only the speakers that failed — one rejection previously discarded every other speaker's outcome.
- **Two latent bugs fixed by construction** when the per-path write loops were consolidated: a synthesized speaker written with an `id` key instead of `uuid` (fixed in bulk save, never in single rename, leaving the speaker unmatchable), and a colour-drift bug that interpolated a UUID into `SPEAKER_${id}` — the value the speaker-colour hash reads — so a renamed segment changed colour.
- Smaller curation fixes: the transcript search index went stale after a rename (searching the name just typed found nothing); an open speaker dropdown showed stale names; `handleSpeakersMerged` dropped `?redact=false`, silently re-masking a revealed transcript; `FileHeader`'s title write never invalidated the page's `file`; the export flow blocked on a comments fetch routed through a bridge whose target element exists nowhere in the app; cluster and profile renames re-skeletoned the entire grid instead of patching one card; delete/merge refetched the cluster list twice; speaker merge issued its merges serially; and the gallery's bulk "Speaker ID" was N serial POSTs (the bulk-action endpoint had no `identify_speakers` handler — now added).
- **Hybrid search returned one file for queries that densely matched a single transcript**: with hybrid + collapse + RRF, a query hitting every chunk of one file (e.g. a speaker name boosted on a heavily-labeled file) filled the entire rank window and starved every other file group — searching a speaker name returned 1 file from a 2,500-file library. The hybrid pass now backfills missing file groups from a plain BM25 collapse query (immune to window starvation), rescored strictly below the lowest hybrid score so backfilled hits never outrank hybrid-ranked ones. Live-verified: 1 → 201 files with the hybrid-ranked file still first. (Aggs-based diversification remains blocked by an OpenSearch 3.4 hybrid+collapse+RRF crash.)
- **Neural-search degradation guardrails**: investigating silently-BM25-only search found three infrastructure failure modes, all addressed — OpenSearch's percentage-based disk flood-stage watermarks tripped on a mostly-full shared drive and turned every index `read_only_allow_delete` (compose now sets absolute 10/20/30 GB free-space watermarks); a dead (`DEPLOY_FAILED`) embedding model silently degraded every hybrid query to BM25-only; and the ML memory circuit breaker flapped on a 4 GB heap carrying 1.3M kNN chunks (threshold raised to 95, persistent). The search-quality harness now asserts the active embedding model is actually DEPLOYED so this failure class can't go unnoticed again.
- **On-demand file analytics never computed (issue #272)**: `str(FileStatus.X)` renders `"FileStatus.X"`, so status guards comparing against bare value strings never matched — the completed-only gate on `_get_or_compute_analytics` was always-True at both call sites yet the compute path never fired correctly, and the redaction don't-run-mid-reprocess guard was dead (redaction could race a reprocess). All comparisons now use enum members, with regression tests pinning the now-active behavior.
- **`DELETE /api/files/{uuid}` unshadowed for completed files**: the upload-cancel route registered first and shadowed the full-delete handler, so deleting any non-PENDING file via the API returned 404 ("No pending upload found"). One route now handles both — PENDING uploads keep the lightweight cancel cleanup; everything else performs the full ownership-checked delete with storage + index cleanup. (The UI was unaffected: the gallery deletes through the bulk-action endpoint.)
- **Comment fallback routes 500'd**: `POST /api/comments` read a field its request schema didn't carry (every call 500'd — the error had been silenced with a `type: ignore`), and `GET /api/comments` expected `media_file_uuid` while the frontend fallback sends `media_file_id`. Both repaired; the legacy parameter name is still accepted.
- **PKI admin DNs configured via the admin UI were silently ignored**: `_is_pki_admin` read only the `PKI_ADMIN_DNS` env var, skipping the documented DB-over-env auth config precedence — admin DNs now resolve DB → env → default like the rest of the PKI settings.
- **Thumbnail fallback returned 500 for missing objects**: a MinIO `NoSuchKey` on the thumbnail fallback endpoint was flattened into a generic exception; it now maps to a clean 404.
- **Floating preview players unified + seek fixed**: the search and speaker pop-out players now share one `FloatingPreviewPlayer` component (identical chrome, hour-aware timestamps, title + speaker layout — fixing the speaker player's jumbled time display), timestamp jumps wait for `loadedmetadata` so deep seeks no longer restart playback from 0 on slow links, a refreshed presigned URL is hot-swapped preserving position and play state, and the speaker media-preview honors `MEDIA_URL_EXPIRE_SECONDS` (was hardcoded to 1 hour). The search preview also no longer auto-reopens on back-navigation, and its close button is translated in all 8 locales.
- **Concurrent speaker-attribute detection deduplicated**: rediarize/recovery flows could stack multiple identical gender-inference tasks for one file (minutes of CPU-bound wav2vec2 each, holding DB sessions idle-in-transaction); a Redis idempotency guard (2 h TTL, released on completion, fail-open without Redis) now skips duplicates while still chaining LLM speaker identification.
- **Blackwell image build un-broken**: `backend/.dockerignore` excluded `scripts/` wholesale, so `Dockerfile.blackwell` could never COPY its SM_121 patch script; that file is now whitelisted.
- **Installer re-runs are idempotent**: `setup-opentranscribe.sh` now creates `.env` immediately and writes each value as it is entered (an interrupted setup no longer loses progress), skips prompts already answered on re-run, and its HuggingFace gate instructions point at the correct `speaker-diarization-community-1` model agreement (was the outdated 3.1 URL).
- **`./opentr.sh rebuild-backend` preserves NAS/NVMe storage mounts**: it previously recreated the datastore containers without the NAS overlay, re-pointing them at empty default Docker volumes — the bind-mounted data was never at risk, but the stack behaved as if wiped until restarted correctly. The NAS overlay is now applied by every code path that recreates containers.
- **Out-of-range LLM temperature reported the wrong error**: the range check sat inside the float-conversion `try`, so its message was swallowed and re-raised as "must be a valid number".
- **Production images report their real version**: `/health` and the admin About panel showed `"unknown"` for Docker Hub images because the `VERSION` file was never inside the image build contexts. `Dockerfile.prod`/`Dockerfile.lite`/`Dockerfile.blackwell` now accept an `APP_VERSION` build arg (baked as env), and `scripts/docker-build-push.sh` passes the release version to every backend build.
- **Frontend prod-image healthcheck probes `127.0.0.1`**: under `read_only` container deployments nginx can't enable its IPv6 listener, so the healthcheck's `localhost → ::1` resolution was refused and the container reported permanently unhealthy while serving fine on IPv4.

#### Frontend hardening — issue #284 Phase 3 (A3.x)

- **security:** Closed the last unsanitized `{@html}` interpolation and a `window.open`
  opener-leak class (A3.2). An audit of every `{@html}` call site found one that bypassed the
  existing DOMPurify allowlist, safe only by coincidence (the interpolated value happened to be
  a formatted byte count); i18next's `escapeValue: false` means nothing else was escaping it.
  Also closes reverse-tabnabbing exposure from `window.open` calls missing `noopener`.
- **70 CSS custom properties were referenced across the app but never declared anywhere**
  (A3.x). `var(--x)` with no fallback is invalid at computed-value time, so the whole
  declaration silently drops — 15 sites painted no background at all, plus two more properties
  missing at 11/11 and 13/14 of their reference sites, across both light and dark themes. A
  sweep of every reference against actual declarations found and fixed all 70.
- **WebSocket reconnects synchronized into a thundering herd after any backend restart, and
  stale search responses could clobber a newer query's results** (A3.7). Reconnect backoff was
  un-jittered, so every client dropped by a restart retried on the same grid and hit the server
  as a synchronized burst on each tick; a jittered backoff now decorrelates clients. A second,
  related race let an in-flight search response for an earlier query overwrite the results of a
  newer one the user had already typed.
- **Every production page load ate an unnecessary 404** (A3.8). The app registered a service
  worker that never existed in the production build — the plugin that wrote it during the
  bundle step ran before the static-adapter's final output, which discarded it. Removed rather
  than repaired. The same pass also fixed an unhashed `theme.js` and a version-skew issue
  between built assets.

### Security

#### The container security gate never worked (issues #413, #414)

Two independent defects meant **no OpenTranscribe release had ever actually been scanned**, and neither produced any sign of trouble — the runs looked normal and reported success.

**It could not fail (#413).** `scripts/security-scan.sh` runs under `set -e`, and each scanner was invoked as `( scan_trivy ...; echo $? > .../trivy.status ) &`. A scanner returns non-zero when it *finds* something, which under `set -e` terminated the subshell on that very line — so the status file was written only when the scan came back clean. The collector then treated a missing status file as a pass. The result was a gate that passed **because** the scan failed, across all five tools (hadolint, dockle, sbom, trivy, grype). Each scanner now records its status through a `|| rc=$?` list, which `set -e` does not apply to, and a **missing** status is now a failure — "we have no idea what that scanner found" must never read as "clean".

**It scanned the wrong image (#414).** The `scan` stage exported `VERSION=`, but `security-scan.sh` reads `IMAGE_TAG` and defaults it to `latest`. The names never matched, so every scan silently fell back to `:latest` — locally, the *previous* release. The v0.5.0 gate was measuring the v0.4.1 image built four months earlier and reporting its CVEs as v0.5.0's. The stage now passes `IMAGE_TAG`, asserts all three images exist locally at that tag before scanning, and afterwards reads `ArtifactName` back out of each report and fails unless it ends in the version under test — the only check that would have caught the original bug.

**Gate overrides are now real and recorded.** `--force-<stage> "reason"` was documented but unimplemented. It exists now, the reason is **mandatory** (there is deliberately no bare `--force`), and an overridden gate is recorded in the ledger as `overridden` with the operator and reason rather than as a pass.

#### Known CVEs in v0.5.0 — accepted, with reasons

With the gate repaired, the real numbers for these images are visible for the first time:

| Image | CRITICAL | HIGH |
|---|---|---|
| backend | 20 | 171 |
| frontend | **0** | **0** |
| docs | 2 | 33 |

**Every one of the 20 backend criticals is unfixed upstream** — there is no patched version to move to. 16 are the perl stack (`libperl5.40`, `perl`, `perl-base`, `perl-modules-5.40`), present solely because `libimage-exiftool-perl` provides the media-metadata parsing the pipeline depends on; the remaining four (`libmbedcrypto16` ×2, `libglib2.0-0t64`, `libxml2`) are likewise unfixed in Debian trixie. `Dockerfile.prod` already runs `apt-get upgrade -y` on every build, and the image is fully patched against its repositories (installed == candidate for every package checked).

The risk is therefore **accepted and recorded** for this release rather than silently carried: the release ledger holds the operator and the full justification. Tracked in **#415** for re-check when Debian publishes fixes — a rebuild is all that will be needed.

#### Authentication audit (issues #353, #354, #355)

A production user reported that LDAP was enabled yet users could still self-register. Auditing
that turned up a set of defects across the authentication surface, listed here by class and
impact. Everything below is fixed in this release.

- **PKI certificate revocation checks could be defeated by a malformed or forged OCSP
  response.** OCSP signature verification "soft-failed" to *verified* from its catch-all
  handler, so a forged GOOD response — or any malformed one that made verification raise — was
  accepted as proof of non-revocation and skipped the CRL cross-check entirely, defeating
  `PKI_REVOCATION_SOFT_FAIL=false`. **A revoked client certificate could authenticate.** An
  unrecognised signature algorithm, an unsupported key type, and a downloaded CRL with no loaded
  issuer certificate (trusted with no signature check at all) had the same class of bug. All
  four now fail closed, falling through to CRL and then the configured soft-fail policy.
- **OIDC group sync could silently demote an admin or bypass allow/block lists when the
  provider withholds groups.** Entra ID omits the groups claim entirely above 200 memberships;
  Google never emits one on any token. Both looked identical to "this identity has no groups,"
  which silently demoted a group-derived admin and silently bypassed the allowed/blocked group
  lists. The claims parser now detects both provider signatures and fails loudly instead of
  resolving to an empty group list.
- **A forced password-change could permanently lock a user out on a deployment with no mail
  transport** (the shipped default). Three paths set the force-change flag but only one cleared
  it — an emailed reset link — so the route the forced-change screen itself calls updated the
  password but never cleared the flag, holding the user on the same screen after every
  successful change until their password-reuse-history budget was exhausted with no way back in.
- **Content redaction could fail open on four paths beyond LLM egress** — display and export,
  not the LLM-masking gap fixed elsewhere in this list: a formatting helper wrote raw DB text
  before applying the mask; a redaction-config resolution failure returned "redaction is off" to
  every downstream reader with no compliance audit event; the same failure on the subtitle-export
  path skipped the admin force-export-redacted floor entirely, so SRT/VTT/TXT exports could ship
  fully unredacted; and an in-place subtitle-masking failure silently left raw text in place
  under a swallowed exception. All four now fail closed.
- **A failed GDPR erasure of biometric voiceprint data was recorded as a completed erasure.**
  Voiceprint erasure logged an OpenSearch failure and returned without touching the caller's
  error list, so an Art. 17 erasure whose OpenSearch step failed — including simply "OpenSearch
  unavailable" — was recorded as SUCCESS while the speaker's voiceprint embeddings remained
  indexed. Failures now propagate into the audit outcome as PARTIAL. A snippet-redaction config
  failure in hybrid search had the same shape: it returned unmasked snippet text instead of
  withholding it (profanity/custom-wordlist scope only, not PII).
- **An unreachable OpenSearch cluster could be indistinguishable from "the index doesn't
  exist" — and that ambiguity drove index deletion.** Several index-introspection helpers caught
  every exception and returned a default empty/absent result, so a genuine connection, auth, or
  config failure looked identical to "empty index." The alias-migration path used exactly that
  signal to decide which of two speaker indices to delete when reconciling — a live index could
  be deleted believing it was empty. Every destructive branch now requires a confirmed count and
  aborts rather than assuming absence.
- **There was no server-side ceiling on upload size on the presigned upload path.** The
  advertised 15 GB limit lived only in the browser; the presigned flow PUTs bytes browser→MinIO
  directly, bypassing the API entirely. A server-side ceiling is now enforced twice — against
  the client-declared size at prepare time, and against the size MinIO actually observed at
  completion (the authoritative check) — with the object and DB row cleaned up on rejection.
- **A crafted watch-source filename could inject arbitrary extra inputs into an ffmpeg
  multi-part stitch.** The concat-list builder escaped single quotes, but the concat demuxer's
  list format has no escape for a newline — a filename containing one terminates its directive
  early and the remainder parses as attacker-chosen further directives. Watch-source filenames
  originate from untrusted remote SMB/S3 listings; such paths are now refused outright. In the
  same pass: `super_admin` was excluded from watch-source authorization checks (a raw
  `role == "admin"` comparison instead of the canonical admin check), so a super_admin could not
  view, list, or reassign another user's watch source.
- **`python-jose` replaced with `joserfc` across the entire JWT spine.** `python-jose` has had
  algorithm-confusion CVEs and is effectively unmaintained. The token-purpose-claim binding that
  closes the MFA half-token bypass above, both FIPS 140-2/140-3 algorithm branches, and the
  per-user revocation-epoch comparison were all preserved and verified by cross-library interop
  tests.
- **The `TESTING`-mode mock-user shortcut could swallow an explicit auth denial.** In a relaxed
  test environment, the broad exception handler around credential resolution caught the
  exceptions raised for "no such user" and "inactive user" and replaced them with a fabricated
  authenticated user — so a **deactivated account** with an otherwise-valid token still got a
  working session under `TESTING=true`, exactly where the test suite runs. Narrowed to only
  cover what it was written for: an unavailable database.
- **MFA could be bypassed with the token the login endpoint hands out before the second factor.**
  Access, refresh and MFA tokens are all signed with the same key, and the request-authentication
  path verified the subject, the JTI and the revocation list but never *what kind of token it
  was*. The short-lived MFA token — issued to a client that has supplied only a password — was
  therefore accepted as a full session. Every token now carries a purpose claim and every
  consumer verifies the one it expects; refresh tokens are likewise no longer accepted as
  sessions, including on the WebSocket handshake, where one already worked.
- **A cross-origin page could strip a victim's MFA enrolment.** The CSRF middleware exempted the
  whole `/api/auth/mfa/` prefix so the pre-authentication verify step could work, which also
  exempted the cookie-authenticated setup endpoint — an endpoint that regenerates the TOTP secret
  and clears every backup code, with no code required. Only the pre-authentication path is exempt
  now. The middleware also skipped any request without an access-token cookie, which left token
  refresh unprotected for the seven days the refresh cookie outlives it; the CSRF cookie's own
  lifetime was raised to match so the check applies uniformly rather than being waived.
- **PKI header trust failed open in a relaxed environment.** With PKI enabled and no trusted-proxy
  allowlist, a forwarded certificate DN was accepted from any source with only a warning, and a
  DN header alone could authenticate without a certificate ever being parsed. The reverse proxy is
  what terminates mTLS and vouches for that header, so it is now refused outright when no proxy is
  allow-listed, and a DN is only trusted from a configured proxy or alongside a validated
  certificate. Hardened deployments were never exposed — startup already refused that
  configuration — but the login route also gained the rate limit and lockout recording it lacked.
  **`PKI_TRUSTED_PROXIES` is now required for any PKI deployment, not just hardened ones.**
- **One rule, two implementations, and they disagreed.** Whether an account may authenticate with
  a local password was decided in two places; only one hard-blocked LDAP. Since the login flow
  tries the first and falls through to the second, an LDAP account with the per-user fallback flag
  set could authenticate against a locally stored hash — breaking the invariant that directory
  accounts never have one. There is now a single implementation, enforced when the flag is *set*
  as well as when it is read, and the admin password-reset endpoint no longer plants a hash on an
  account whose identity lives elsewhere.
- **Changing a credential or a privilege did not end existing sessions.** Only the self-service
  password reset revoked anything. Role changes, deactivation, account lock, MFA reset, and both
  admin and self-service password changes all left every other session live — so an attacker
  holding a session kept it through the victim's password change, which is the case revocation
  exists for. All of those paths revoke now. Because access tokens are stateless there was nothing
  to revoke for the current one, which made "log out everywhere" *weaker* than "log out"; a
  per-user revocation epoch closes that.
- **Multi-factor enforcement was advisory.** `MFA_REQUIRED` was reported by the status endpoint and
  read by nothing at login, so on a deployment that required MFA an unenrolled user simply received
  a full session — enforcement existed only in the frontend, and any API client ignored it. Login
  now issues a scoped enrolment challenge instead, and completing enrolment issues the session.
  Separately, the switch governing MFA replay protection defaulted to the fail-open setting: on a
  Redis error both TOTP codes and MFA tokens became replayable. It now defaults secure in a
  hardened environment. A backup code used to disable MFA was verified but never consumed, so the
  same code worked indefinitely.
- **Account lockout was neither atomic nor crash-safe.** The Redis path issued its optimistic lock
  on one connection and its write on another, so the transaction guarded nothing and concurrent
  failures could both record the same attempt count; its error path passed the wrong object to the
  in-memory fallback and turned a Redis blip into a 500 on every login. It is a server-side
  compare-and-set now, with a working fallback. The super_admin lockout exemption was computed only
  on *successful* attempts, so the emergency-access account still locked out and a success never
  cleared its counter.
- **The interactive API documentation was published unconditionally**, enumerating the entire admin
  and authentication surface to anonymous visitors in production. It is withheld when hardened
  (`ENABLE_API_DOCS=true` opts back in) and denied at the reverse proxy as well.
- **Account takeover via an unverified email change.** Changing your own address required no
  password, notified nobody, and was unaudited — change the address, request a password reset, own
  the account. It now requires the current password, notifies the previous address, and is audited.
- **Password-reset links were written to the application log** whenever SMTP was unconfigured —
  the default, and set in none of the shipped compose files — and again on any send failure. A
  reset URL is a single-use credential; it is no longer logged, and SMTP sends now time out
  instead of holding a request thread open indefinitely.
- **Directory login could not promote an admin, and demoted platform owners.** Converting a local
  account to LDAP wrote a role/flag combination the database forbids, so any user in an LDAP admin
  group got a server error on first login and could never convert; the same path also demoted an
  existing `super_admin`. All derived-privilege writes across the LDAP, OIDC and PKI sync paths now
  go through the single canonical derivation.
- **The role invariant was not actually enforced.** The constraints added in v369 make the
  superuser flag a mirror of the role, but the role column remained nullable — and PostgreSQL
  passes a constraint that evaluates to unknown, so a row could carry the superuser flag with no
  role at all and satisfy the very check meant to prevent it. Migration `v375` makes the column
  NOT NULL, does the same for `auth_type`, and adds the missing `auth_type` constraint (an
  unrecognised value silently exempted an account from MFA enrolment).
- **Assorted**: an inactive account produced a distinguishable response that both disclosed the
  account's existence and skipped lockout recording; lockout was keyed on the submitted identifier,
  giving accounts reachable by two identifiers two independent budgets; logout could leave valid
  cookies in place when Redis was down, and a failed federated logout was reported to the user as a
  clean sign-out; several authentication routes had no rate limit; audit records attributed every
  failed login to LDAP whenever LDAP was enabled, regardless of how the attempt was actually made.
- **An external identity could take over an existing account by email coincidence.** Every
  external path — LDAP, PKI, and the JIT seam — resolved a user by the provider's own identifier
  and then fell back to matching on email address. The address is an *attribute of the external
  source*, so anyone who could write it (a directory administrator, a self-service directory, or
  anyone who could get a certificate issued) could point it at an existing account and inherit it,
  including its content and its privileges. There is now **one** rule for all four paths: link on
  an email match only when the source asserts the address is verified, and **never** link a
  `super_admin`. A refusal fails the login rather than silently creating a duplicate account, and
  returns the *same* generic error as a bad credential so it cannot be used to probe which
  addresses exist. **See Upgrade Notes — this changes behaviour on providers that do not assert
  `email_verified`.**
- **OIDC token validation accepted the wrong credential on failure.** Validation tried the ID
  token and, if it did not verify, **fell back to the access token**, accepting whichever one did.
  That turns an ID token failing audience or issuer validation into a silent downgrade onto a
  credential RFC 9068 §6 forbids the relying party from inspecting, whose `aud` means something
  else entirely, and which several major providers issue as an opaque string no JWKS can verify at
  all. Only the ID token authenticates now; a missing or invalid one is a hard 401, and `openid`
  is forced into the requested scopes so a provider cannot be configured into issuing no ID token.
  The access token is still used as a bearer credential against `userinfo`, which is what it is
  for.
- **OIDC had no admission control at all.** JIT provisioning created an account
  **unconditionally**, and the only group-shaped setting (`oidc_admin_role`) *elevates* rather
  than *admits* — so pointing the integration at a corporate realm provisioned every identity in
  it. Allow/block group lists and an optional approval queue now gate provisioning, evaluated
  before the account row is created and before any email-match link, and re-evaluated on every
  login so a group removal locks the account out rather than only affecting new users. An empty
  allow-list admits everyone, which is what preserves existing deployments on upgrade.
- **The privilege ceiling on directory-driven grants is now enforced at three layers.** IdP group
  mappings can grant at most `admin`; `super_admin` is refused by the wire contract, by the
  service before anything is persisted, and by a database CHECK constraint. `super_admin` is
  local-only by design — it is the break-glass account for exactly the identity provider that is
  failing — and directory reconciliation never demotes one either.

- **Security controls no longer weaken silently when Redis is unavailable (issue #284 A0/A1 follow-up, issue #324)**: Redis holds the state several controls depend on — the token-revocation blacklist, account-lockout counters, MFA single-use replay protection, and auth rate limiting — and that state is *shared across replicas*. When Redis was unreachable each process fell back to its own in-memory store, which is empty on start and never shared, so **a token revoked on one replica was still honoured by every other**: "log out all devices" and password-reset revocation quietly stopped meaning anything, on the exact control you reach for during an incident. It logged at `warning` and scrolled past. Redis here is a **cache**, not the system of record, so the degraded path now consults the system of record instead: `refresh_token.revoked_at` is durable and shared by every replica, so revocation keeps working and nobody is logged out during an outage. An access token (which has no durable row of its own) is denied only on **positive evidence** — the user demonstrably had sessions and every one was revoked — because "this user has no refresh tokens" is not evidence of revocation and denying on absence would lock out valid users whose auth path never mints one. With no database session, or if the fallback query itself fails, the check **denies**. Degradation now logs at `CRITICAL` and increments a new `security_state_degraded_total{control,fallback}` Prometheus counter — **alert on it**. Behaviour is identical for self-hosted single-node and cloud (the gate is `ENVIRONMENT`, which defaults to `production`; only a developer laptop keeps the in-memory convenience), and there is deliberately **no flag to disable it** — an off-switch on a security control gets flipped during exactly the incident it guards against.
- **A password reset that could not revoke sessions reported success anyway (issue #324)**: `confirm_password_reset` called a helper that commits on success and calls `db.rollback()` on *any* error — and it ran **before** the caller's own commit, so a Redis outage or failed commit silently reverted the new password hash, the history row and the used-token markers, while the function still returned success. **The user was told their password had changed when it had not, and their existing sessions were left live** — the outcome FedRAMP AC-12 exists to prevent, arriving at the moment it matters most, since a reset is frequently triggered by a suspected compromise. Revocation now runs inside the caller's transaction and a failure aborts the whole reset with a real error. The two revocation entry points are split by contract so the mistake cannot recur: `revoke_all_user_tokens_in_transaction` (commits nothing, propagates) and `revoke_all_user_tokens` (best-effort, commits, documented as unsafe to call mid-transaction).
- **An undecryptable auth secret is now treated as unset instead of returned as ciphertext (issue #324)**: `AuthConfigService.get_config` handed back the stored ciphertext when decryption failed — the comment said so outright — and had a quieter second path where a decrypt returning falsy *without raising* left the ciphertext in place and logged **nothing at all**. These values are used as real credentials (an LDAP bind password, an OIDC client secret), so ciphertext is at best a baffling authentication failure and at worst an encrypted blob shipped to an external IdP or rendered in the admin UI. Both paths now return `None` and log, naming `ENCRYPTION_KEY` as the usual cause; the caller falls through to the env value and then the coded default — a known source rather than garbage. A test had been *asserting* the old behaviour, which is how it survived.
- **A failed content-redaction scan is no longer cached as "clean" (issue #324)**: detection is deliberately detect-once/cache-forever, but the run was marked `done` **unconditionally** while the detectors underneath swallowed their own failures (per-segment PII, batch toxicity at `debug` level, and the LLM detector). A transient failure — model not loaded, out of VRAM, LLM provider down — therefore produced empty spans and a `done` status, permanently recording a transcript as containing no PII, never re-scanned. Detector exceptions are now collected and the run is marked `failed` rather than `done`. Spans that *did* succeed are still committed (they are real findings; discarding them would be strictly worse) — only the status stops claiming the pass was complete. Language-gated skips are untouched, since those are deliberate rather than failures. **See Upgrade Notes for what `failed` does and does not do.**
- **An unverifiable password-history entry is no longer silently counted as "not a match" (issue #324)**: the reuse check treated a hash it could not verify as a non-match and logged at `debug`, so if every stored hash became unverifiable — plausible after a hashing-scheme or FIPS-mode change — the control **silently stopped enforcing anything** while looking identical to a clean pass. This one is deliberately **not** fail-closed, unlike the rest of the above: rejecting the new password would leave the user on their *current* password, which is a guaranteed reuse, so permitting a *possibly* reused old one is strictly better. The fix is visibility — `ERROR` when the check is degraded, `CRITICAL` when it was completely blind, naming `FIPS_MODE`/`FIPS_VERSION` as the usual cause.
- **Breaking — tags are now per-user; tag names no longer leak across accounts (migration `v374_add_tag_user_id`)**: `tag` had **no owner column** — `id, uuid, name (globally UNIQUE), source, normalized_name` — so tags were a shared vocabulary *by schema* and `_get_or_create_tag()` reused any row by name. `GET /api/tags/unused` was literally `db.query(Tag).filter(~Tag.id.in_(used_tag_ids))` with **no user filter at all**, so any authenticated user could enumerate every unattached tag name in the deployment; `GET /api/tags` leaked the same set through its `MediaFile.id IS NULL` arm. Tag names are user-authored free text — a client name, a case number, "Project Falcon Layoffs" — so this disclosed one account's work to every other. `tag` gains a nullable `user_id` (NULL = *system* tag, i.e. the seeded `Important`/`Meeting`/`Interview`/`Personal` vocabulary everyone sees; non-NULL = that user's own), the global `UNIQUE (name)` is replaced by partial unique indexes `uq_tag_user_name` (`(user_id, name) WHERE user_id IS NOT NULL`) and `uq_tag_system_name` (`(name) WHERE user_id IS NULL`), and every read applies one visibility rule: a tag is visible if it is a system tag, owned by the caller, **or** attached to a file in `PermissionService.get_accessible_file_ids_subquery` — which already covers files shared directly and via groups plus the org tenant gate, so sharing keeps working with no second rule. `_get_or_create_tag` now takes an owner and every writer passes one: interactive endpoints attribute to the caller, background writers (auto-labeling, watch-source imports, yt-dlp playlist/URL imports, upload tag application) to the **file owner** — an unattributed tag would be a system tag and therefore published to every account. Because names are only unique per owner, `remove_tag_from_file` and `remove_tags_from_file` resolve the tag by joining `file_tag` for that file rather than by name, and the gallery's ALL-tags filter counts `DISTINCT Tag.name` instead of `Tag.id`. `tag.user_id` is a plain FK, so account deletion (`admin`) and GDPR erasure now detach and delete the subject's tags before the `user` row.
- **Breaking — deployment hardening now fails closed (issue #284 A0.3/A0.4)**: every production security control was gated on `ENVIRONMENT in ("production", "prod")`, and **nothing ever set `ENVIRONMENT`** — not `.env.example`, not any compose file. `opentr.sh` uses a shell-local variable of the same name for its own dev/prod switch and exports `BUILD_ENV` instead, so `settings.ENVIRONMENT` was always its `"development"` default in *every* deployment, including `./opentr.sh start prod`. The default-secret refusal, `DEBUG` enforcement, `REDIS_PASSWORD` requirement, PKI proxy hard-stop, and the session-cookie `Secure` flag therefore never ran anywhere. `ENVIRONMENT` now defaults to `production`, every gate routes through a new `settings.is_hardened`, and relaxation requires explicitly naming one of `development`/`dev`/`testing`/`test`/`local` — an unset, empty, or misspelled value is treated as production. The dev stack declares `ENVIRONMENT=development` itself in `docker-compose.override.yml` (never loaded in prod), so `./opentr.sh start dev` is unaffected.
- **Legacy multipart upload no longer buffers the whole file in RAM (issue #284 A1.20)**: it extended a `bytearray` to EOF, so a multi-GB upload was held entirely in memory and OOMKilled the API process — taking every *other* in-flight request, WebSocket, and SSE stream down with it, so one user's large upload could end everyone's session. It now spools to a `SpooledTemporaryFile` (RAM up to 32 MB, then disk), closed on every exit path so a failed upload cannot leave a backing file behind. The fingerprint switched from `compute_from_bytes` to `compute_from_stream`, which samples 3x128 KiB regardless of size and therefore never re-materializes the file.
- **WebSockets are drained on shutdown, and an inert GPU setting now says so (issue #284 A1.21/A1.7)**: nothing closed WebSockets during shutdown — the lifespan cancelled its background tasks but left sockets open, so the process waited on them until the orchestrator's grace period expired and SIGKILLed it, dropping every stream mid-message. They now receive a clean `1001 going away`, which the frontend's reconnect logic treats as a retry. Separately, `--max-tasks-per-child` is a **prefork-pool** feature that Celery silently ignores under `--pool=threads` — which is what the GPU workers use by design, to keep model weights pinned in VRAM. Lowering `GPU_MAX_TASKS` to bound a VRAM leak therefore did nothing at all. The worker now warns at startup when a meaningful value is set on the threads pool, and `.env.example` states the constraint; `GPU_WORKER_POOL=prefork` remains the real lever, at the cost of reloading the model per task.
- **Degraded-mode fallbacks no longer latch (issue #284 A1.16/A1.19)**: the account-lockout store fell back to per-process in-memory tracking when Redis was unreachable **and never retried** — one transient blip and that replica counted failed logins in its own memory for the rest of its life. Behind a load balancer that is an auth-throttling bypass: each replica keeps its own counter, so an attacker gets N x the allowed attempts and lockouts stop being visible across replicas at all. It now re-probes Redis (rate-limited to one attempt per 30 s, so a hard outage doesn't add a connection attempt to every login). Separately, `PIPELINE_SCRATCH_SHARED=false` now disables the scratch-volume fast path: staging the preprocessed WAV to local scratch and skipping the MinIO upload is correct only when every worker shares one filesystem, and on a multi-node deployment the CPU pod stages audio the GPU pod cannot read — failing every file with a confusing missing-file error rather than anything pointing at the mount.
- **Task time limits and worker DB-pool sizing (issue #284 A1.2/A1.5)**: there were **no** global Celery time limits, so a hung CUDA call held the single GPU slot indefinitely and no later transcription could start. A deliberately generous 3 h soft / 3 h 15 m hard ceiling now bounds it — tight limits would truncate legitimate long transcriptions (media is capped at 4 h and hybrid/CPU runs are slow), and the limits sit under the 6 h `visibility_timeout` so a timeout kill cannot race redelivery. Note `soft_time_limit` uses SIGALRM, which is unreliable under `--pool=threads` (what the GPU workers use); there the real protection remains `visibility_timeout` plus DB-status crash recovery. Separately, `DB_POOL_SIZE`/`DB_MAX_OVERFLOW` defaulted to 20 + 40 = up to **60 connections per process**, which across ~10 worker services is ~660 against a Postgres whose default `max_connections` is 100. Workers now get a small pool (2 + 3), dropping the worker ceiling to 40; the API keeps the larger default as the latency-sensitive path.
- **Startup steps that assumed a single instance are now elected (issue #284 A1.3/A1.15)**: `_clear_stale_task_state()` deletes **all** `task_progress:*` keys and every coordination lock, so with more than one API replica the second to boot wiped progress and locks belonging to work the first was actively coordinating. It — and the OpenSearch index repair — now run once per boot window via a Redis `SET NX` election that fails **open** (Redis unreachable → run the step, since skipped cleanup is worse than duplicated). `startup_recovery_task` also gained the `with_task_lock` guard it was missing, so a rollout no longer re-dispatches recovery for the same stuck files once per replica.
- **Celery broker and migration-lock correctness (issue #284 A1.1/A1.4/A1.6/A1.17/A1.18)**: no `visibility_timeout` was configured, so the Redis broker kept kombu's 3600 s default — and because the transcription tasks are `acks_late=True`, **any run over an hour was redelivered and the same file transcribed twice on the GPU, concurrently**. The migration advisory lock did nothing: it was taken on a pooled connection that `engine.dispose()` then closed *before* `command.upgrade()` ran, and the matching unlock used a fresh session, which cannot release a session-scoped lock. Per-task `engine.dispose()` defeated connection pooling entirely. `rediss://` broker URLs crashed every worker because kombu requires an explicit SSL context. `backup.run` had no overlap lock, so a double tick started two concurrent `pg_dump`s.
- **Download SSE lost-wakeup race (issue #284 A1.22)**: readiness was checked *before* subscribing to the progress channel, so a prepare finishing in that window published to an empty channel and the stream waited forever for an event that had already happened. Now check → subscribe → **re-check**, which keeps the Redis-free fast path for an already-ready download while closing the gap.
- **`file_created` reached only one replica (issue #284 A1.14)**: it used the in-process `ConnectionManager`, so behind a load balancer a user connected to a different replica never saw the new file appear. Routed through Redis pub/sub.
- **SSRF: every server-side fetch of a user-supplied URL is now validated (issue #284 A0.1/A0.2/A0.10)**: `is_safe_url` existed but had exactly **one** caller — the yt-dlp ingest path. The LLM/ASR "test connection" and model-discovery endpoints, and the watch-source S3/SMB connectors, took an arbitrary host from any authenticated user and fetched it server-side with no validation, which with open self-registration is effectively anonymous reach into the deployment's private network and cloud instance metadata. The guard itself also had two holes: `0.0.0.0` passed every check (it is neither private, loopback, nor reserved) and multicast was unchecked. It now also handles IPv6-mapped IPv4, IPv6 ULA/link-local, AWS IMDSv6, malformed ports, and multi-A-record hosts where only one record is private. Rejection reasons are logged but never returned, since distinguishing "private IP" from "cannot resolve" turns the endpoint into a network scanner. `LLM_ALLOW_PRIVATE_ENDPOINTS` (default off) and `WATCH_ALLOW_PRIVATE_ENDPOINTS` (default on — a LAN NAS is the normal single-tenant case) re-enable private targets. yt-dlp URLs are re-validated at fetch time, closing the queue-delay DNS-rebinding window; full TOCTOU closure additionally needs egress restriction on the download worker.
- **Client IP is resolved through the trusted-proxy chain everywhere (issue #284 A0.5)**: three call sites disagreed. The rate limiter honoured `RATE_LIMIT_TRUSTED_PROXIES` but took the **first** `X-Forwarded-For` entry, which a client controls when more than one proxy sits in front; `_get_client_info` used the raw peer, so every audited login recorded the reverse proxy instead of the user; and the audit middleware trusted `X-Forwarded-For`/`X-Real-IP` **unconditionally**, letting any client forge its own address in the security audit trail. All three now share `utils/client_ip.resolve_client_ip`, which walks the chain right-to-left past trusted hops and ignores forwarding headers entirely when no trusted proxy is configured. `Dockerfile.prod` gains `--proxy-headers` with `--forwarded-allow-ips` scoped to private ranges (never `*`).
- **Authorization results are no longer masked as 500s (issue #284 A0.6)**: `clear_video_cache` and `refresh_analytics` wrapped their ownership lookup in a broad `except Exception`, re-wrapping a legitimate 403/404 as a 500 and then referencing the still-unassigned `file_id` in the handler's own log line — crashing a second time with `NameError`. Both now hoist the lookup out of the `try`, re-raise `HTTPException`, and stop echoing `str(e)` in the 500 body.
- **WebSocket auth reaches parity with HTTP (issue #284 A0.7)**: the local-JWT path skipped the `is_active` and token-revocation checks the HTTP path performs, so a revoked token or a deactivated account still opened a socket and kept receiving that user's events.
- **TOTP codes are single-use (issue #284 A0.13)**: `verify_totp` never tracked used codes, so a valid code stayed valid for its whole 30-second step plus the drift window, contrary to RFC 6238 §5.2. Codes are now claimed in Redis with `SET NX`, scoped per user, with a TTL covering the full acceptance envelope. Fails open when Redis is unavailable unless `MFA_REQUIRE_REDIS=true`.
- **`/register` is rate-limited and can be closed (issue #284 A0.11)**: it was the only auth route with no limiter, while creating accounts that are immediately active and GPU-capable. Adds the standard auth limiter plus `ALLOW_OPEN_REGISTRATION` (default true for self-host; set false when an external IdP owns identity).
- **Wildcard CORS with credentials refuses to boot, and `TESTING` shortcuts are inert in production (issue #284 A0.8)**: `TESTING=true` makes the auth layer fabricate a user from the token UUID when the DB lookup fails; every such shortcut now additionally requires `not settings.is_hardened`, so the flag cannot take effect in a real deployment even if it leaks into the environment.
- **Breaking — the well-known admin credential is no longer seeded in production (issue #284 A0.9)**: `initial_data.py` created `admin@example.com` / `password` as a **`super_admin`** on every boot, with no environment gate, so any public deployment shipped with a publicly-known platform-owner login. That credential is now created only in a relaxed environment (the e2e suite and local workflow depend on it). A hardened deployment uses `INITIAL_ADMIN_EMAIL` / `INITIAL_ADMIN_PASSWORD`, or — when no password is supplied — a generated 192-bit password logged **once** at `CRITICAL` on first startup. The seeder also no longer creates a bootstrap admin when any super-admin already exists.
- **Production placeholder-key guard hardened**: the backend's production startup check refused known weak defaults but did **not** recognize the `.env.example` placeholder (`CHANGE_ME_auto_generated_on_install`), so a hand-copied `.env` could boot production with publicly-known JWT/encryption keys. Both the `JWT_SECRET_KEY` and `ENCRYPTION_KEY` checks now also reject any `change_me` placeholder value.
- **Predictable secret fallback removed from the installer**: when neither `openssl` nor `python3` was available, `setup-opentranscribe.sh` derived all credentials — including the MinIO at-rest encryption key — from the current timestamp (`date +%s`), making them brute-forceable. The fallback now reads `/dev/urandom` (cryptographically secure, coreutils-only), and setup aborts rather than ever generating predictable secrets.
- **Offline installer now generates the MinIO encryption key**: `install-offline-package.sh` left `MINIO_KMS_SECRET_KEY` at its invalid placeholder (with `MINIO_KMS_AUTO_ENCRYPTION=on`), which prevents MinIO from starting; it now generates a real key (and a `FLOWER_PASSWORD`) like the main installer.
- **Windows installer no longer bakes secrets into the package at build time**: `build-windows-installer.sh` generated all credentials (Postgres/JWT/encryption/MinIO-KMS) when the package was *built*, so every installation of the same distributed installer shared identical secrets. The package now ships `.env` with placeholders plus a new `generate-secrets.ps1` (CNG `RandomNumberGenerator`, UTF-8 no-BOM, idempotent — only replaces placeholder values) that `run_opentranscribe.bat` invokes on first launch, giving each installation unique credentials. Verified end-to-end under PowerShell 7.
- **Startup secret-guard test coverage**: new `tests/test_production_secrets_guard.py` (12 tests) locks the production validation behavior — placeholder/weak JWT and encryption keys, missing Redis password, `DEBUG=true`, and the dev-mode exemption.
- **Magic-byte validation on presigned upload completion**: bytes uploaded via the presigned path go browser→MinIO directly (never through the API), so `/files/complete` now range-reads the object header and runs `validate_uploaded_file` before dispatching to the pipeline — rejecting disguised files (e.g. an executable renamed `.mp4`) with a 400 and deleting the object + row. This brings the presigned path to security parity with the legacy multipart handler. Fail-safe: only a confirmed bad signature rejects; a transient read error logs and proceeds.
- **Redaction worker hardening**: the `celery-redaction` service runs with `no-new-privileges`, capped intra-op threads, and lower OS priority.
- **AWS credential encryption**: the AWS Access Key ID is stored AES-256-GCM encrypted (never returned; the response exposes only `has_access_key_id`), matching the existing secret-key model.
- **Frontend dependency advisories cleared**: `npm audit fix` brought known advisories (axios, @sveltejs/kit, dompurify, et al.) to 0.
- **Frontend production hardening (issue #174)**: a `dependency-audit` CI job (`npm audit` + `pip-audit`); the theme bootstrap externalized from `app.html` to a static `/theme.js` and the **CSP tightened to drop `script-src 'unsafe-inline'`** — moved to SvelteKit `kit.csp` hash mode, with the now-redundant CSP `script-src` directives removed from `nginx.conf` / `nginx-pki.conf` (verified zero console CSP violations); server-side enforcement of the speaker-label length cap (`SpeakerUpdate` `max_length`); and a documented AWS production / hardening guide (`docs/deployment/AWS_PRODUCTION.md`) capturing the security audit (no bundle secrets, no prod source maps, DOMPurify on all `{@html}`, server-side authz) plus the ALB + AWS WAF + ACM TLS + Secrets-Manager reference architecture.
- **Infrastructure host ports bound to loopback**: postgres, redis, opensearch (and admin port), minio (API + console), and flower now publish their host ports as `127.0.0.1:<port>:<container>` instead of `0.0.0.0`. These services are reached internally over the compose network (`postgres:5432`, `minio:9000`, etc.); the host ports exist only for local tooling/tests and are no longer exposed to the LAN. The application frontend/nginx ports are unchanged.
- **`no-new-privileges` on the remaining auxiliary containers**: added `security_opt: [no-new-privileges:true]` to nginx, keycloak, step-ca, lldap (LDAP test), and the Samba test share (the core services already had it), preventing setuid privilege escalation inside those containers.
- **Installer `.env` locked to owner-only (0600)**: `install-offline-package.sh` previously left the generated `.env` world-readable (`644`) after a recursive `755`; it is now `chmod 600` *after* the recursive pass so the file holding all generated secrets isn't re-loosened.
- **`OPENSEARCH_ADMIN_PASSWORD` now generated by the installers**: both `setup-opentranscribe.sh` and `install-offline-package.sh` generate a complexity-compliant admin password (upper+lower+digit+special, ≥8) so enabling the OpenSearch security plugin doesn't fail its bootstrap password check. It is only consumed when `OPENSEARCH_SECURITY_ENABLED=true`.
- **API keys no longer echoed at the setup prompt**: `setup-opentranscribe.sh` reads LLM provider API keys with `read -s` (no terminal echo) instead of plain `read`, so keys don't appear on-screen or in the scrollback during interactive setup.
- **Role is now the single source of truth for admin privileges**: `role ∈ {user, admin, super_admin}` is the sole authorization concept — `is_superuser` became a derived mirror of `role == 'super_admin'`, enforced by a DB CHECK constraint (migration `v369`, which also promotes the legacy default admin to `super_admin` and reconciles every existing row). `POST /admin/users` now requires `super_admin` to create admin/super-admin accounts, closing a privilege-escalation path where a regular admin could create a `super_admin`; `is_superuser` is always derived server-side, never taken from the client. (Authorization was already server-enforced — the JWT role claim is ignored and privileges load from the DB — but the two divergent concepts were checked inconsistently.) External IdPs (LDAP/Keycloak/PKI) grant at most `admin`: `super_admin` is local-only, and an existing local super-admin is never demoted by IdP group sync — important when the platform owner also belongs to an IdP admin group. New invariant test suite proves the constraint, derive-on-create, the escalation block, and that a forged token role claim is ignored.
- **Non-FIPS deployments issued FIPS-profile credentials**: token algorithm selection and the MFA backup-code hash context keyed off `FIPS_VERSION` — which defaults to `140-3` on every deployment — instead of the `FIPS_MODE` master switch, so ordinary installs issued HS512 JWTs (with a JWT key shorter than the 64 bytes HS512 requires) and PBKDF2 backup codes. Both now gate on `FIPS_MODE`, matching `core/security.py`; backup codes hashed under the old branch remain verifiable.

### Documentation

- **Authentication documentation rewritten against the shipped behaviour.**
  `docs-site/docs/authentication/` now carries `overview`, `ldap`, `oidc`, `pki` and a new
  `groups` page (IdP group mapping), all wired into the sidebar — previously only `overview` was,
  and the detail pages were unreachable from navigation. `keycloak.md` becomes a stub pointing at
  `oidc.md` (Docusaurus has no client-redirects plugin here, so the stub *is* the redirect), and
  `docs/KEYCLOAK_SETUP.md` is likewise a one-hop stub to the new `docs/OIDC_SETUP.md` because ~17
  links across the repo — including `scripts/test-all-auth.sh` and the release blog post — point
  at it. `docs-site/docs/features/authentication.md` and `docs-site/docs/user-guide/admin-panel.md`
  were rewritten too: both still described `AUTH_TYPE` (a setting nothing ever read), a two-tier
  privilege model, and a Keycloak-only SSO story. Surfaces that exist without an admin screen —
  IdP group mappings, `require_email_verification`, directory sync — are documented as
  API/settings-only rather than implied to have a panel.
- **Superseded planning prose removed from `docs/`**: `ProjectPlan.md`,
  `FORK_IMPLEMENTATION_PLAN.md`, `FORK_COMPARISON_vfilon.md`,
  `FRONTEND_AUTH_IMPLEMENTATION_PLAN.md`, `RELEASE_PLAN_v0.4.0.md`,
  `E2E_TEST_EXPANSION_PLAN.md`, `SPEAKER_PROFILE_FIX_PLAN.md`, `IMPLEMENTATION_AUDIT_REPORT.md`,
  `DOCUMENTATION_IMPLEMENTATION_SUMMARY.md`, `DOCUMENTATION_STRATEGY.md`,
  `DECEMBER_2025_INTEGRATION.md`, `OPTIMIZATION_ROADMAP.md` (superseded by
  `GPU_PIPELINE_OPTIMIZATION_PLAN.md`) and a stray `run.txt`. Each was checked for inbound
  references first; the only surviving mentions are inside historical CHANGELOG entries for
  already-published releases, which are left alone as a matter of record.
- **Homepage version badge single-sourced from `VERSION`**: the docs site hard-coded `v0.4.0` in `src/pages/index.tsx` and had drifted a release behind. `docusaurus.config.ts` now reads the repo-root `VERSION` file at build time and exposes it via `customFields`, so the badge can never disagree with what shipped. The Docker build context is `docs-site/`, which cannot reach `../VERSION`, so the value is threaded through as an `OT_VERSION` build arg (`opentr.sh` and both compose overlays pass the existing `APP_VERSION`); an unreadable or malformed version omits the badge rather than rendering a stale or `unknown` value. `VERSION` was added to the `deploy-docs` workflow's trigger paths so a release bump actually redeploys the site.
- **Mobile layout fixes (phone / tablet)**: the homepage scrolled sideways on every phone viewport (~200 px of overflow on a 375 px screen). Two independent flexbox bugs caused it — the hero install command and the comparison table were both centered flex items whose default `min-width: auto` prevented them from shrinking, pushing their left edges off-screen where no scrolling could reach them. Both are now block-level scroll containers. Document-level horizontal overflow is 0 px at 375/393/412/768/1366/1440 px.
- **Comparison table usability**: the 12-column homepage table now scrolls horizontally with the "Feature" column pinned via `position: sticky`, so a value in the rightmost column is still attributable to its row on a phone. Sticky cells were initially transparent because Infima leaves `--ifm-background-color` as `transparent` in light mode; a new opaque `--ot-bg` token backs anything that must occlude scrolling content.
- **All markdown tables are scrollable and accessible**: a new `ScrollableTable` component (wired in through `src/theme/MDXComponents.tsx`) wraps every markdown table in a scroll container with edge-fade affordances. This replaces Infima's `table { display: block; overflow: auto }`, which scrolled but stripped the element's implicit table semantics for screen readers; tables are now real `display: table` elements again, and only genuinely scrollable ones become keyboard tab stops.
- **Broken-link enforcement**: `onBrokenLinks` and `onBrokenAnchors` are both `throw` (previously `warn`), so a bad cross-reference fails the build instead of shipping. The site currently builds clean.
- **Reference corrections**: repository links across `docs-site/docs/` now point at `attevon-llc/OpenTranscribe` following the org transfer (Docker Hub images remain `davidamacey/opentranscribe-*` — that account did not move); two blog posts linked the never-registered `docs.opentranscribe.io` and now use the live `docs.opentranscribe.app`; the "pin a specific version" example in the upgrade guide no longer suggests the long-superseded `v0.3.0` tag.
- **Dark-mode parity**: the comparison table's supported/unsupported markers used hard-coded hex values that lost contrast on dark backgrounds; both now follow theme tokens and carry accessible labels.
- **Screenshots & Visual Guide published** (`getting-started/screenshots`): a 15-section visual walkthrough covering login through upload, processing, transcripts, speaker management, AI features, collections, bulk operations, and administration. The page existed but was disabled behind an underscore prefix and was unreachable — its image component was defined as `export const Img = ({src}) => <Img src={useBaseUrl(src)} />`, which rendered itself and recursed until the stack blew, and its 51 captions were indented inside their JSX wrapper so MDX parsed them as markdown paragraphs, emitting `<p>` inside `<p>` (invalid HTML, React hydration failure on every viewport). Both are fixed, images are lazy-loaded with async decoding (51 full-size screenshots on one page), and its four dead category-index links (`/docs/user-guide`, `/docs/installation`, `/docs/configuration`, and `/docs/api`, which never existed) now point at real pages. This surfaces 50 screenshots that were on disk but referenced by no published page.
- **Removed the superseded `_intro.mdx` orphan**: same self-referential image component, imported by nothing, excluded from the build, and factually stale (claimed OpenSearch 3.3.1 against the shipped 3.4, and a 7-language UI against the current 8 locales). Its content is covered by the published `getting-started/introduction`.

### Breaking Changes

#### Deployment configuration moved from `admin` to `super_admin`

Six admin panels now require the `super_admin` role instead of `admin`: **ASR provider**,
**Engine configuration**, **Backups**, **Media Mirror**, **Watch sources**, and the
**Redaction policy** floor. They configure how the deployment runs, and four of them store
infrastructure credentials (S3 keys, SMB passwords, SMTP passwords) that a team-level admin has
no reason to read or replace.

**If a plain `admin` administers those panels today, promote them to `super_admin`** (Settings →
Users → Role) before upgrading, or move the work to an existing super_admin. Nothing else changes
tier: user accounts, tasks, search and speaker maintenance stay at `admin`.

#### An OIDC login no longer takes over a local account with the same email address

Signing in through an identity provider used to adopt any existing account whose `email` matched,
regardless of what the provider actually asserted about that address. It now does so only when the
provider sets `email_verified: true`, and **never** for a `super_admin` account.

**This closes the path on two common providers**: Authentik hardcodes `email_verified` to `false`,
and Entra ID omits the claim entirely (absent is treated as unverified — the check fails closed).
On those, an OIDC login that previously absorbed a pre-existing local account now fails with the
same generic error as a bad credential, and is audited with `error_code ACCOUNT_LINK_REFUSED`.

**Remedy — pick one, per account:**

1. **Link it deliberately.** Set that account's `oidc_subject` (LDAP: `ldap_uid`; PKI:
   `pki_subject_dn`) to the provider's identifier for the person. A subject match is never
   re-litigated, so the login proceeds normally afterwards.
2. **Change one of the two addresses**, so there is no coincidental match and the OIDC login
   provisions its own account.

Doing nothing leaves the person unable to sign in via OIDC while the duplicate address exists.
There is no setting to restore the old behaviour: the address is an attribute the external
directory controls, so "trust it unconditionally" is the account-takeover vector this closes.

#### The OIDC surface is renamed — configuration keys, routes and the admin tab

Config keys are `oidc_*`, the admin tab is **OIDC**, and the routes are `/api/auth/oidc/login`
and `/api/auth/oidc/callback`. **No identity provider needs reconfiguring** (the registered
redirect URI points at the SPA's `/login` page), and **every `KEYCLOAK_*` environment variable
keeps working permanently** — the legacy spelling even wins when both are set. Stored database
configuration is renamed automatically by migration `v377`.

What does break: a script that writes `PUT /api/admin/auth-config/keycloak`, reads a
`keycloak_*` key out of `GET /api/admin/auth-config`, or calls
`/api/auth/keycloak/{login,callback}` directly. `GET /api/auth/methods` reports `"oidc"` in
`methods`; its `keycloak_enabled` field is retained for **one minor release** so a cached SPA
bundle keeps rendering the SSO button, and will be removed after that.

#### `PKI_TRUSTED_PROXIES` is now required whenever PKI is enabled

Header-sourced PKI authentication is **refused** when no trusted proxy is allow-listed, instead of
being accepted with a warning. Hardened deployments already refused to *start* in that
configuration, so this is a change only for development and evaluation stacks that enabled PKI
through the admin UI. Set it to the address the backend sees the reverse proxy arrive from.

#### `POST /api/auth/token/refresh` now requires the CSRF header for cookie-authenticated clients

It mints a new session from the refresh cookie alone, which is exactly what a forged cross-site
request would target, so it is no longer CSRF-exempt. Browsers are unaffected — the SPA already
double-submits the token, and the CSRF cookie's lifetime was extended to match the refresh
cookie's so an idle session still has one. **A non-browser API client that sends cookies must now
send `X-CSRF-Token` too**; clients using `Authorization: Bearer` are exempt as before.

#### `GET /api/auth/methods` no longer always advertises `local`

`methods` previously contained `"local"` unconditionally. It now reflects
`local_enabled`, so a deployment whose identity lives entirely in an external IdP reports only the
methods it actually accepts. The response also gained `local_enabled` and `allow_registration`.

#### Interactive API docs are withheld in a hardened environment

`/api/docs`, `/api/redoc` and `/api/openapi.json` return 404 unless `ENABLE_API_DOCS=true`. They
remain available in development.

#### API — `GET /api/files/{uuid}` returns tag objects instead of tag names (issue #326)

`MediaFileDetail.tags` was `list[str]` — the serializer selected `Tag.name` and nothing else — while
`GET /api/tags`, `POST /api/tags`, and `POST /api/tags/files/{uuid}/tags` all returned `Tag`
objects. Three surfaces, two shapes. The file-detail payload now carries the **same object** the tag
endpoints serve, so there is one definition of a tag on the API.

**Before** (`GET /api/files/{uuid}`):

```json
{
  "uuid": "019ec90a-1b2c-7def-8000-000000000001",
  "filename": "quarterly-review.mp4",
  "tags": ["Important", "Meeting"]
}
```

**After** (`GET /api/files/{uuid}`):

```json
{
  "uuid": "019ec90a-1b2c-7def-8000-000000000001",
  "filename": "quarterly-review.mp4",
  "tags": [
    { "uuid": "019ec90a-3f41-7aaa-8000-0000000000a1", "name": "Important", "source": "manual" },
    { "uuid": "019ec90a-3f41-7aaa-8000-0000000000a2", "name": "Meeting", "source": "auto_ai" }
  ]
}
```

`uuid` (string, UUID) and `name` (string) are always present; `source` is nullable and is `"manual"`
for a user-applied tag, `"auto_ai"` for one applied by the auto-labeling LLM.

**Why**: `source` is the only thing that distinguishes an AI-applied tag from a manual one, so
dropping it meant the file-detail page could not badge AI tags after a reload without a second
request; and `uuid` matters now that `v374_add_tag_user_id` makes tag names unique only **per
owner**, so a name is no longer a stable identifier for a tag row.

**Scope — exactly one endpoint changed.** `GET /api/files` (the gallery/list endpoint) has **no
`tags` field at all**, before or after; its response schema `MediaFile` never carried one, and none
was added here (the list serializer would need a per-row tag query). `GET /api/tags`, `POST
/api/tags`, `GET /api/tags/unused`, `POST /api/tags/files/{uuid}/tags`, and `DELETE
/api/tags/files/{uuid}/tags/{tag_name}` are all unchanged — they already returned objects, and the
delete route is still keyed by tag **name**.

**Not changed**: routes (no route added, removed, or re-pathed), permissions, and tag visibility.
The serializer returns exactly the tags attached to the file, as it did before; `endpoints/tags.py`
already made every tag on an accessible file fully visible to that caller via the
"attached to a file in the accessible-files subquery" arm of `_visible_to`, so the extra fields
disclose nothing `GET /api/tags` did not already return to the same user. The OpenSearch search
index still stores tags as a `keyword` array of names, so `tags` on a **search hit** is still
`string[]`.

**Frontend**: this deletes the shim that made the mismatch survivable — `TagsEditor` no longer
coerces incoming strings into objects with fabricated `temp-<name>` uuids, and `TagsSection` is
typed `MediaFileDetail` instead of `any`.

### Upgrade Notes

- **⚠️ ACTION REQUIRED — the backend will REFUSE TO START if your `.env` lacks production
  secrets.** Security enforcement changed from fail-open to fail-closed (issue #284 A0.3):
  `ENVIRONMENT` now defaults to `production` and the gate is `settings.is_hardened` rather than
  `ENVIRONMENT in ("production", "prod")`. In v0.4.x, `ENVIRONMENT` defaulted to `development`,
  so a deployment that never set it — which is the documented normal case, since `.env.example`
  ships it commented out — **skipped every production secret check**. v0.5.0 enforces them.

  The upgrade fails with `ValueError: REDIS_PASSWORD is required in production environment` and
  the stack stays down. Caught by the release rehearsal (v0.4.1 → v0.5.0); see issue #410.

  **Before upgrading**, ensure your `.env` has all of:

  ```bash
  # Add a Redis password if you do not have one:
  echo "REDIS_PASSWORD=$(openssl rand -hex 16)" >> .env
  ```

  and that `JWT_SECRET_KEY` / `ENCRYPTION_KEY` are not the shipped placeholders, `DEBUG` is not
  enabled, and — if OIDC is configured — `OIDC_VERIFY_AUDIENCE` is set. A single-user install on
  a trusted network that wants the old behaviour can instead set `ENVIRONMENT=development`, but
  that disables every hardening control and is not recommended for anything reachable.

- **ACTION REQUIRED if a plain `admin` manages deployment settings**: the ASR provider, Engine
  configuration, Backups, Media Mirror, Watch sources and Redaction policy panels now require
  `super_admin`. Promote those accounts (Settings → Users → Role → Super Admin) before upgrading,
  or hand the work to an existing super_admin. **Creating additional super_admins is now possible
  from the UI** — the role select previously offered only `user` and `admin`, so the tier could not
  be granted at all without direct API access.
- **ACTION REQUIRED if you use OIDC with Authentik or Entra ID (or any provider that does not
  assert `email_verified`): an OIDC login will no longer take over a pre-existing local account
  with the same email address.** Authentik hardcodes `email_verified` to `false` and Entra ID
  omits the claim, and an absent claim is treated as unverified, so on those providers the
  takeover path is now closed. Affected users get the same generic login failure as a bad
  credential (deliberately — a distinct message would tell an attacker which addresses exist);
  look for `error_code ACCOUNT_LINK_REFUSED` in the audit log to identify them.
  **Remedy, per account:** either link it deliberately — set that account's `oidc_subject` (or
  `ldap_uid` / `pki_subject_dn`) to the provider's identifier for that person, after which the
  match is on the subject and is never re-litigated — or change one of the two email addresses so
  there is no coincidence and the login provisions its own account. There is no flag to restore
  the old behaviour: the address is an attribute the external directory controls, and trusting it
  unconditionally is an account-takeover path. `super_admin` accounts are **never** linked by
  email, verified or not.
- **The OIDC configuration surface is renamed, and existing setups keep working.** Your
  `KEYCLOAK_*` environment variables need no change — ever — and stored database configuration is
  renamed automatically by migration `v377`. **No identity-provider reconfiguration is needed**:
  the registered redirect URI points at the SPA's `/login` page, not at the backend routes that
  were renamed. Update only if you have a script that writes
  `PUT /api/admin/auth-config/keycloak`, reads `keycloak_*` keys from the config API, or calls
  `/api/auth/keycloak/*` — those become `oidc`. `GET /api/auth/methods` reports `"oidc"`; the
  `keycloak_enabled` field is kept for one more minor release so a cached SPA bundle keeps
  working.
- **New auth settings are all opt-in and default to today's behaviour.** `oidc_allowed_groups`
  empty = admit everyone (previously JIT provisioned every identity unconditionally);
  `require_account_approval` false = accounts usable immediately; `directory_sync.enabled` false
  with `dry_run` true; `pki_allow_password_fallback` true; `password_max_age_days` only forces a
  change for accounts that actually have a recorded `password_changed_at`. **If you point OIDC at
  a corporate realm, set `oidc_allowed_groups`** — otherwise every identity in that realm can
  provision an account.
- **Directory sync is worth arming deliberately.** Enable it with `directory_sync.dry_run=true`,
  read `directory_sync.last_result` for a few days, and only then set `dry_run=false`. The pass
  **disables accounts and revokes their sessions**; `directory_sync.max_disables_per_run`
  (default 10) is what stops a wrong search base or group DN from disabling the deployment in one
  run. There is no admin panel for it yet — it is six `SystemSettings` rows.
- **Password resets, invitations and verification links need a working mail transport.**
  Designate one email configuration to carry authentication mail (Settings → Watch Sources →
  Email configurations, then the auth-mail designation), or leave it undesignated and configure
  the `SMTP_*` environment transport. **A stock deployment has neither**, so invitations and
  self-service password resets will not be delivered until you set one up. Reset links are no
  longer written to the application log as a fallback.
- **ACTION REQUIRED for PKI deployments: set `PKI_TRUSTED_PROXIES`.** Header-sourced PKI
  authentication is refused when no trusted proxy is allow-listed, where it was previously accepted
  with a warning. Hardened deployments already refused to start without it, so this affects
  development and evaluation stacks that enabled PKI through the admin UI. Set it to the address
  the backend sees your reverse proxy arrive from (e.g. `127.0.0.1,10.0.0.0/8`).
- **Existing sessions end on upgrade.** Access tokens now carry a purpose claim and tokens minted
  before the upgrade do not have one, so they are rejected and users sign in again once. This is
  deliberate: accepting an unmarked token would leave the MFA-bypass path open for the lifetime of
  every token already in circulation.
- **Self-registration and local password login are now real switches.** The admin UI has shown an
  "Allow self-registration" toggle since v0.4.0 that was wired to nothing — the endpoint read the
  `ALLOW_OPEN_REGISTRATION` environment variable instead, and that variable had no mapping to the
  database key, so flipping the switch did nothing. It works now, as does a new `local_enabled`
  companion. **If you set `ALLOW_OPEN_REGISTRATION=false` in `.env` as a workaround, that still
  applies** (the environment remains the fallback), but the database value now takes precedence
  once you save the panel — check Settings → Authentication → Local reflects what you intend.
  For an LDAP- or OIDC-owned deployment the combination is `local_enabled=false`,
  `allow_registration=false`; an **active `super_admin` is always exempt** from `local_enabled` so
  you cannot lock yourself out of the screen that undoes it.
- **API clients that authenticate with cookies must send `X-CSRF-Token` on token refresh.** Bearer
  clients and browsers are unaffected.
- **`GET /api/auth/methods` may no longer list `local`**, and gained `local_enabled` /
  `allow_registration`. Anything asserting `"local" in methods` should assert on the flag instead.
- **Interactive API docs are withheld when hardened.** Set `ENABLE_API_DOCS=true` to restore
  `/api/docs`, `/api/redoc` and `/api/openapi.json` on a hardened deployment.
- **Every session ends once more at the login-banner and session-timeout rollout.** If
  `login_banner_enabled` is on, every user is asked to acknowledge the banner again the first time
  they load the app after upgrading — the acknowledgment is now enforced server-side, and an
  acknowledgment recorded before the wording last changed no longer counts. Session idle and
  absolute timeouts do **not** retroactively invalidate anything: their columns are nullable and
  un-backfilled, treated as "no cap recorded", and stamped on each session's first rotation.
- **Migrations `v378_idp_group_mapping`, `v379_rename_keycloak_config_to_oidc`,
  `v380_oidc_identity_columns` and `v381` apply automatically on backend startup** (dev) or via
  `alembic upgrade head` (production). `v379` moves the stored auth-config keys and carries the
  ciphertext across **unchanged** — no decrypt/re-encrypt, so `ENCRYPTION_KEY` is not needed for
  the rename. `v380` is a **single transaction** on purpose: a half-applied state would either
  lock out every OIDC user or exempt them from MFA enrolment. It also drops a duplicate
  `auth_type` CHECK constraint that had been re-asserted by three earlier revisions — one rule,
  one owner — and its consistency test pins that exactly one remains.
- **Migration `v377_harden_user_auth_invariants` applies automatically on backend startup** (dev)
  or via `alembic upgrade head` (production). It makes `user.role` and `user.auth_type` NOT NULL and
  adds an `auth_type` CHECK. Rows carrying a NULL role are repaired to `user` with the superuser
  mirror recomputed, and an unrecognised `auth_type` is repaired to `local` — both backfills only
  ever remove privilege, never grant it. A correctly-migrated database has neither and the backfills
  are no-ops.
- **Flower's persistent database moved off the code directory.** It was mounted at `/app`, which is
  where the application code lives, so the named volume shadowed the image's code and pinned Flower
  to whatever it contained when the volume was first created — a rebuilt image was never picked up.
  It now lives under `/app/temp`. Existing deployments can delete the old `flower_data` volume
  (`docker volume rm <project>_flower_data`); it holds only task history.
- **The Queue Dashboard link now works on every deployment, and is admin-only.** `/flower/` was
  served only by the optional nginx overlay, so the button 404'd on prod and PKI stacks; it is now
  proxied by the frontend image too, gated by an `auth_request` check against the app session, and
  hidden from non-admins. Flower exposes task names and arguments, so it was previously reachable
  by any logged-in user (and, where the overlay injected credentials, by anyone who could load the
  origin).

- **Redaction runs that hit a detector failure are now marked `failed`, and are NOT retried automatically**: previously such a run was recorded as `done` with empty spans, so a transcript that was never fully scanned looked permanently clean. It is now recorded honestly as `failed`, with the failed detector names logged at `ERROR`. Two consequences worth knowing:
  - **Nothing blocks.** `failed` never withholds a transcript (only `pending`/`processing` do), so users see their transcripts exactly as before. You may simply start seeing `failed` where you previously saw `done` — that is the fix surfacing pre-existing failures, not a new fault.
  - **Re-detection is a deliberate action, not automatic.** The lazy re-dispatch on read only fires for files that have *never* been scanned (status `NULL`), so a `failed` file stays failed until you re-run detection — `redaction.reindex_all`, or the admin re-detect path. This is intentional: retrying on every read would let a persistently failing detector (missing model, no VRAM, dead LLM provider) hammer the `celery-redaction` worker with a job that cannot succeed. Bounded automatic retry with an attempt counter is deliberately left as future work. **Treat `failed` as "a human should look"** — check the worker logs for the detector names.
- **Alert on `security_state_degraded_total`**: a non-zero rate on this new Prometheus counter means a security control is running without its shared state store (see Security). On AWS, run **ElastiCache for Redis with Multi-AZ and automatic failover** rather than a single Redis container, so the degraded window is seconds of failover instead of the length of an outage. Note Redis is also the Celery broker, so a Redis outage stops transcription regardless — highly available Redis protects throughput and security posture together. Managed Redis with encryption in transit: point `REDIS_URL` at `rediss://` and Celery enables TLS for both the broker and the result backend. Full detail in `docs-site/docs/operations/production-deployment.md`.
- **ACTION REQUIRED — production deployments must set real secrets before upgrading**: hardening now fails closed (see Security), so a deployment still running a default or placeholder `JWT_SECRET_KEY` / `ENCRYPTION_KEY`, or with no `REDIS_PASSWORD`, will **refuse to start** instead of booting insecurely as it silently did before. Set real values (the installer generates them; `.env.example` documents each) before upgrading. To relax intentionally — a LAN-only or evaluation stack — set `ENVIRONMENT=development` explicitly. `./opentr.sh start dev` needs no change.
- **ACTION REQUIRED — production admin login changes**: the `admin@example.com` / `password` super-admin is no longer seeded outside development. Existing accounts are untouched and you keep signing in as before. On a **new** production deployment, either set `INITIAL_ADMIN_EMAIL` / `INITIAL_ADMIN_PASSWORD`, or grep the backend startup log for `GENERATED password` to retrieve the one-time generated credential and change it after first login.
- **Breaking — removed media endpoints**: external consumers of `GET /api/files/{uuid}/video`, `/simple-video`, `/content`, `/download`, and `/download-with-token` must migrate to `GET /api/files/{uuid}/stream-url` (playback) and `POST /api/files/{uuid}/prepare-download` (downloads).
- **Breaking — bulk export endpoint**: `POST /api/files/bulk-export` (sync streamed ZIP) is replaced by `POST /api/files/bulk-export/prepare` + the SSE `GET /api/files/bulk-export-stream`.
- **Breaking — file-detail `tags` payload (issue #326)**: **if your script reads `tags` from `GET /api/files/{uuid}` as strings, read `tag.name` instead** — e.g. `file.tags.map(t => t.name)` in JS, `[t["name"] for t in file["tags"]]` in Python. `"Important" in file["tags"]` and `", ".join(file["tags"])` are the two patterns that break. There is no migration and no server-side action: this is a response-shape change only, routes/permissions/tag-visibility are unchanged, and `GET /api/files` (list) still sends no `tags` field. Full before/after JSON in Breaking Changes above.
- **Breaking — file fingerprints regenerated**: the server-side `imohash` fingerprint now uses the real `imohash` package (murmur3 over sampled windows + size) instead of the previous hand-rolled blake2b stand-in, so **every existing `media_file.imohash` value changes**. A one-time recompute runs automatically on first startup after upgrade (`asyncio` task gated by the `imohash_package_recompute_complete` system-settings flag, same pattern as the thumbnail/embedding migrations) and overwrites all rows via fast ranged reads — no manual action required. Cross-pipeline dedup (watch sources, re-upload detection) is unreliable for not-yet-recomputed rows until it finishes; an admin "Recompute File Fingerprints" button is available to re-trigger it.
- **New required service**: deployments must run the new `celery-redaction` worker — redaction detection runs once per transcript regardless of user settings. It is included in the standard compose overlays; no action is needed when using `./opentr.sh`.
- **Breaking (unreleased-master only) — v367 schema rewritten**: the cloud-seams migration was rewritten in place to be vendor-neutral (`external_id`/`external_org_id`; billing columns removed from core). No tagged release shipped the old shape; deployments tracking unreleased master (or commercial pins) are repaired automatically by the new `v371` migration, which renames the legacy columns idempotently on startup — no manual action required.
- **Breaking — tag ownership split (`v374_add_tag_user_id`)**: `tag.name` is **no longer globally unique**, so anything reading the table directly (a report, an export script, a `SELECT ... FROM tag WHERE name = ?`) can now get several rows back and must scope by `user_id` or join through `file_tag`. The migration backfills automatically on backend startup and needs no manual step: every tag attached to at least one file is claimed by the **lowest-numbered** owning user, and a tag attached to files owned by *several* users is **split** — each additional owner gets their own copy (same name/source/normalized name, fresh uuid) with only their `file_tag` rows repointed at it, so no file loses a tag and nobody inherits another account's row. Tags attached to no file stay ownerless and become **system tags**, visible to everyone — this is what keeps the seeded picker defaults in place. A seeded default that happened to be in use (someone had tagged a file "Meeting") is claimed like any other attached tag; the seeder recreates the ownerless row on the same startup, so the shared vocabulary is whole again and the claiming user keeps their attachment. Expect the tag list to *shrink* for most users after upgrade — that is the fix: you now see your own tags, the system vocabulary, and tags on files shared with you, instead of everyone's. `DELETE /api/tags/cleanup` (admin) still sweeps deployment-wide but now skips system tags, which are unattached by nature. The `downgrade()` is deliberately partial: it drops the column and indexes but does **not** restore the global `UNIQUE (name)` when the split produced duplicate names, since merging them would silently re-share one user's tag with another. One residue to be aware of: `GET /api/tags` is read-through cached in Redis for 5 minutes (`TTL_TAGS`), so a user whose list was cached just before the upgrade can still be served the pre-fix (leaky) payload until that key expires — flush `cache:tags:*` if you want the fix to take effect instantly.
- **Database migrations** `v360_add_file_pipeline_timing`, `v361_add_media_file_imohash`, `v362_add_pipeline_timing_markers`, `v363_add_asr_access_key_id`, `v364_add_content_redaction`, `v365_add_prompt_shared_by`, `v366_add_watch_sources`, `v367_add_cloud_seams` (rewritten), `v368_uuid_native_type_guard`, `v369_superuser_role_invariant`, `v370_add_media_file_quarantine`, `v371_repair_cloud_seams_columns`, `v372_add_audit_organization_id`, `v373_add_cluster_organization_id`, `v374_add_tag_user_id`, `v375_add_chat_tables`, `v376_add_chat_projects`, `v377_harden_user_auth_invariants`, `v378_idp_group_mapping`, `v379_rename_keycloak_config_to_oidc`, `v380_oidc_identity_columns`, `v381_approval_state`, `v382_scim_tokens`, and `v383_saml_auth_type` apply automatically on backend startup (idempotent). No manual `alembic` step is required in dev.
- **New env vars** are optional (sensible coded defaults): redaction tuning (`REDACTION_*`, `DOWNLOAD_REDACTION_MODELS`, `PRELOAD_REDACTION_MODELS`), derived-cache retention (`DERIVED_CACHE_RETENTION_DAYS`), hybrid mode (`WHISPER_HYBRID_MODE`, `WHISPER_HYBRID_CPU_MODEL`), engine/multi-GPU (`ENGINE_GPU_SPLIT`, `ENGINE_TRANSCRIBER_BACKEND`, `ENGINE_DIARIZER_BACKEND`, `GPU_TRANSCRIBE_DEVICE_ID`, `GPU_DIARIZE_DEVICE_ID`), DB pool (`DB_POOL_SIZE`, `DB_MAX_OVERFLOW`), `SEARCH_LARGE_TRANSCRIPT_CHUNKS`, `FFMPEG_THREADS`, and timing (`ENABLE_BENCHMARK_TIMING`). Boundary-correction and redaction behavior is primarily DB/admin-UI driven. `MEDIA_URL_EXPIRE_SECONDS` default changed 300→21600 (6h).
- **Optional multi-GPU split**: enable with `ENGINE_GPU_SPLIT=true` and launch via `./opentr.sh start dev --with-gpu-split` (runs dedicated `gpu-transcribe` / `gpu-diarize` workers). Without those workers, leave it off — tasks would otherwise wait on an unstaffed queue.
- **Hybrid mode** auto-activates on small-VRAM CUDA GPUs and on macOS (`WHISPER_HYBRID_MODE=auto`); force with `true`/`false`. No action needed for standard A6000-class GPUs.
- **Watch sources**: to watch a local folder, mount it via `WATCH_HOST_PATH` (the only watch env var; defaults to `./watch`) and start with `./opentr.sh start dev --with-watch` — every other watch setting (per-source connections/credentials/schedules and the global tuning knobs) is DB-backed and managed live from the admin UI with no restart. New backend dependencies `smbprotocol`, `msal`, and `watchdog` are added to `requirements.txt` (installed automatically on image build). New optional test container: `./opentr.sh start dev --with-smb-test`. **Email notifications are experimental** — delivery has not yet been verified against a live SMTP/M365/Exchange provider; test your configuration before relying on it.

## [0.4.1] - 2026-04-14

### Overview

Patch release fixing LDAP group filtering for Active Directory Distinguished Names (issue #188) and adding Keycloak-as-PKI-broker compliance for government/FedRAMP deployments.

### Fixed

- **LDAP group DN parsing** ([#188](https://github.com/davidamacey/OpenTranscribe/issues/188)): Group lists containing full Active Directory DNs (e.g. `CN=Whisper_Users,CN=Users,DC=domain,DC=local`) were silently broken because the code split on commas — which are structural delimiters inside DNs. Group lists now use **semicolons** as the multi-group separator. A single full DN with no semicolons is treated as one group correctly. Existing simple group names (no `=` characters) continue to work unchanged.
- **PKI admin DN parsing**: `PKI_ADMIN_DNS` suffered the same comma-split bug. Fixed to use semicolon-delimited parsing via the same shared helper.
- **Government cert display name**: Government X.509 certificates carry space-separated CNs in the form `LastName FirstName emailusername`. `extract_display_name_from_gov_dn()` now parses this 3-token format and renders it as `First Last`.

### Added

- **Keycloak-as-PKI-broker support**: When Keycloak acts as the X.509/PKI broker (government CAC/PIV deployments), cert claims injected into the OIDC token are now extracted and stored on the user record. Both short claim names (`cert_dn`, `cert_serial`) and Keycloak's `x509_cert_*` aliases are handled automatically.
- **PKI admin promotion via Keycloak**: Users authenticating through Keycloak with a cert DN listed in `PKI_ADMIN_DNS` are promoted to admin even if they lack the Keycloak realm role — matching the standalone PKI auth behaviour.
- **Documentation**: New "Government / FedRAMP: Keycloak as X.509 PKI Broker" section in `docs/KEYCLOAK_SETUP.md` covering authenticator setup, cert claim mapping table, DN format, and `PKI_ADMIN_DNS` configuration.

### Upgrade Notes

- **LDAP group list format change** — If you previously used comma-separated group names that happened to work (e.g. `GroupA,GroupB` where neither name contained `=`), update to semicolons: `GroupA;GroupB`. Full AD DNs **must** use semicolons: `CN=Group1,DC=domain,DC=local;CN=Group2,DC=domain,DC=local`.
- `PKI_ADMIN_DNS` also switches to semicolon delimiters if you have multiple DNs.
- No database migrations required.

## [0.4.0] - 2026-03-22

### Overview

Major release combining enterprise-grade authentication, native transcription pipeline, neural search, GPU optimizations, cloud ASR providers, comprehensive speaker intelligence, Progressive Web App support, a frontend security hardening sprint, and dozens of features built from processing 1,400+ real-world recordings over two months of development (281 commits). This release significantly improves security, performance, search capabilities, and mobile usability.

### Added

#### Enterprise Authentication System
- **Multi-Method Authentication**: Support for 4 simultaneous authentication methods:
  - Local authentication with bcrypt hashing
  - LDAP/Active Directory integration with auto-provisioning
  - OIDC/Keycloak with identity federation and social login
  - PKI/X.509 certificate authentication with OCSP/CRL revocation checking
- **Super Admin Configuration UI** - Comprehensive settings interface for managing authentication methods without restart
- **Multi-Factor Authentication (MFA)** - RFC 6238 compliant TOTP with Google Authenticator, Authy, Microsoft Authenticator compatibility
- **Password Policies** - FedRAMP IA-5 compliant password requirements with complexity, history, and expiration
- **Account Lockout** - NIST AC-7 compliant protection with configurable failed attempt thresholds and progressive lockout
- **Rate Limiting** - Per-IP and per-user rate limiting for authentication and API endpoints
- **Audit Logging** - Comprehensive authentication audit trail in structured JSON/CEF format with OpenSearch integration
- **Session Management** - JWT token-based sessions with refresh token rotation and concurrent session limits
- **Database-Driven Configuration** - All auth settings stored encrypted (AES-256-GCM) in database, accessible via admin UI

#### PyAnnote v4 Migration & Optimization
- **Automatic Migration System** - Admin UI for seamless migration from PyAnnote v3 to v4 with progress tracking
- **Speaker Overlap Detection** - Identifies and visualizes overlapping speakers with confidence scoring
- **Warm Model Caching** - Eliminates 40-60 second cold-start delays by pre-loading models on startup
- **Fast Speaker Assignment** - Efficient speaker assignment using WhisperX's built-in speaker mapping
- **Flexible Embedding Mode** - Per-file toggle between PyAnnote v3, v4, or auto-detection
- **Native Word-Level Timestamps** - Always-on word-level timestamps for all 100+ languages via cross-attention DTW (no separate alignment model needed)
- **Asynchronous Embedding Extraction** - Non-blocking speaker embedding processing

#### OpenSearch Native Neural Search
- **ML Commons Integration** - Native OpenSearch neural search using ML Commons plugin
- **Server-Side Embeddings** - Embedding generation moved from client to server for better performance
- **Hybrid Search** - Combines BM25 full-text with neural semantic search using RRF merging
- **Model Registry** - 6 embedding models organized by quality tier (smallest/fastest to largest/most accurate)
- **Offline/Airgapped Support** - Model downloading scripts for environments without internet access
- **Dynamic Model Management** - Add/remove embedding models via admin UI

#### Unified Transcription Pipeline
- **Native Word-Level Timestamps** - Word timestamps now provided natively by faster-whisper cross-attention DTW for all 100+ languages (previously only ~42 languages via wav2vec2 alignment)
- **Unified Pipeline** - Single streamlined transcription pipeline replaces the previous parallel_pipeline/whisperx_service split
- **User-Configurable VAD Settings** - Exposed Voice Activity Detection threshold and minimum silence duration as user-tunable settings
- **Word Timestamp Validation** - Post-processing validation and correction of word-level timestamps to prevent drift and ensure monotonicity

#### Performance Improvements
- **Default Model Change** - Switched from large-v2 to large-v3-turbo (6x faster transcription)
  - Note: large-v3-turbo cannot translate; use large-v3 for translation needs
- **Batch Size Optimization** - Intelligent batch sizing based on available VRAM
- **Neural Model Endpoints** - RESTful API for model lifecycle management
- **GPU Memory Leak Fixes** - Gated model preloading with `PRELOAD_GPU_MODELS` env var to prevent 15 GB CPU worker leak; forced CPU for speaker clustering under 500 speakers to prevent 44 GB prefork child leak
- **Vectorized Speaker Assignment** - NumPy matmul replaces O(n×m) linear scan, 13x speedup (80s → 6s for 4.7-hour files)
- **TF32 Acceleration** - Enabled at worker startup and after diarization for Ampere+ GPUs
- **GPU Pipeline Benchmarks** - 40.3x single-file realtime, 54.6x peak at concurrency=8, perfect linear scaling 1–12 workers on RTX A6000

#### Cloud ASR Providers
- **Multi-Provider Cloud ASR** - 8 cloud speech providers: Deepgram, AssemblyAI, OpenAI Whisper API, Google, AWS Transcribe, Azure Speech, Speechmatics, Gladia (#150)
- **pyannote.ai Integration** - Cloud diarization via pyannote.ai API (`/v1/diarize`)
- **Independent Diarization Provider Architecture** - `diarization_source` selector with four modes: ASR built-in, local (PyAnnote GPU), pyannote.ai cloud, or off — independent of transcription provider choice
- **API-Lite Deployment Mode** - CPU-only image (~2 GB vs 8.9 GB) for organizations without GPUs; cloud-transcribed files still get local speaker embedding extraction for cross-file matching
- **Custom Vocabulary** - Domain-specific hotwords (medical, legal, corporate, government) used as faster-whisper hotwords and cloud provider keyword boosting
- **Admin-Pinned ASR Model** - Admins control local Whisper model selection; model loaded once at startup, shared across all workers; per-user override removed
- **Per-Transcription Model Selection** - Users can override the admin-pinned model per upload (#153)

#### Speaker Intelligence
- **Speaker Pre-Clustering** - GPU-accelerated speaker clustering groups speakers across files based on voice similarity (#144)
- **Global Speaker Management Page** - Dedicated page for cross-file speaker profile management
- **Gender Classification** - Neural network gender prediction from voice characteristics using Apache 2.0 licensed model; results stored on profiles for cross-video consistency
- **Gender-Informed Cluster Validation** - Cross-gender cluster assignment requires higher similarity threshold; minority-gender members flagged for review
- **Speaker Profile Avatars** - Avatar images for speaker profiles
- **Jump-to-Timestamp Links** - Speaker editor includes links to timestamps in transcript (#147)
- **Speaker Metadata Parsing** - Cross-reference pipeline with metadata hints display for LLM-assisted speaker identification (#141)
- **Unassign and Blacklist** - Remove speaker assignments and blacklist erroneous profiles
- **Outlier Analysis** - Detect and flag outlier embeddings in speaker clusters
- **Play/Pause Toggle** - Inline audio playback in speaker cluster views
- **OpenSearch Cosine Score Fix** - OS `cosinesimil` returns `(1+cos)/2`; all 8 kNN score read locations now convert to raw cosine (`2.0 * score - 1.0`)
- **Profile Embedding Fix** - `add_speaker_to_profile_embedding` now delegates to `update_profile_embedding` for correct centroid averaging

#### Search Improvements
- **Hybrid Search Overhaul** - Fixed OpenSearch 3.4 `ArrayIndexOutOfBoundsException` crash when using `aggs` + `hybrid` + `collapse` + RRF pipeline (was silently falling back to BM25-only)
- **Score Gate Removed** - Replaced hard suppression with soft demotion (`_apply_semantic_demotion`); semantic results no longer dropped
- **Dynamic Over-Fetch** - Cap raised from 200 to 1000 via `SEARCH_MAX_OVERFETCH` env var for large indexes
- **BM25 Improvements** - Fuzziness AUTO, cross-fields, phrase slop; rank_constant 40→30
- **Stop/Cancel Reindex** - Cancel in-flight reindex operations from Admin UI (#5994)
- **Search Reliability** - Word-boundary regex for RRF collapse fallback; synthetic highlights for semantic results

#### Collaboration & Sharing
- **User Groups & Collection Sharing** - Create user groups and share collections with groups or individual users (#148)
- **Speaker Profile Sharing** - Share speaker profiles via collection sharing infrastructure
- **Config/Prompt Sharing** - Share LLM configs, prompts, media sources, and org contexts between users
- **Per-Collection AI Prompts** - Different AI summarization prompts for different collections (#146)
- **Bidirectional Prompt-Collection Links** - Prompts show linked collections on their cards

#### Upload & Media
- **TUS 1.0.0 Resumable Uploads** - Resumable chunked uploads with MinIO multipart storage; survives network interruptions (#10)
- **Collection & Tag Selection at Upload** - Select collections and tags during file upload (#145)
- **URL Download Quality Settings** - Configure video resolution, audio-only mode, and bitrate for yt-dlp downloads (#122)
- **File Retention / Auto-Deletion** - Admin-configurable file retention with automatic deletion (#134)

#### Export & Settings
- **Configurable TXT Export** - Persistent export preferences including speaker grouping options
- **Disable AI Summary** - Option to skip AI summarization per upload (#152)
- **Disable Speaker Diarization** - Option to skip diarization per upload (#151)
- **Stepper Reprocess UI** - Step-by-step reprocessing with stage picker for selective pipeline stages (#143)
- **Organization Context** - Inject domain knowledge into all LLM prompts for context-aware summaries (#142)

#### Infrastructure & Monitoring
- **Flower Monitoring Upgrade** - Industry-standard Celery/Flower integration with persistent task history, queue visibility, and worker status
- **Multi-GPU Stats with Stepper UI** - Real-time per-GPU stats display with stepper interface
- **Resumable Upload Sessions** - TUS protocol session management in database
- **Progressive Web App (PWA) & Mobile Overhaul** - Installable PWA, 2-column mobile grid, hamburger nav, full-screen modals, scroll locking, touch-optimized UI (#155)
- **Security Hardening** - CSP headers, private MinIO buckets, AES-256-GCM encryption, non-root containers, FIPS 140-3 readiness
- **Auto-Labeling** - AI suggests tags and collections from transcript content with fuzzy deduplication (#140)
- **Codebase Modularization** - 9 new shared backend modules, 6 new UI components, speaker task splits, dead code removal
- **Embedded Documentation** - New `opentranscribe-docs` container serving the Docusaurus documentation site; accessible at `/docs/` through the app's NGINX proxy (and `http://localhost:3030/docs/` directly); fully offline-capable for air-gapped deployments

#### Authentication Additions (v0.3.3 integrated)
- **Keycloak Federated Logout** - Session termination propagates to Keycloak OIDC end-session endpoint (#125)
- **Super Admin PKI + Local Password Fallback** - PKI-authenticated super admins can retain local password as fallback (#127)

#### Upload Modal Redesign
- **6-Step Stepper Flow** - Replaced the accordion-inside-modal upload UX with a linear stepper: Media → Tags → Collections → Speakers → Options → Submit. Conditional Extract step appears automatically for large video files
- **Unified Across All Upload Sources** - File, URL, and recording uploads now share steps 2-6, so URL downloads and recordings gain full access to tags/collections/speaker settings (previously file-only)
- **Remember Previous Values** - Upload modal pre-fills tags, collections, speaker settings, whisper model, and skip-summary from the last upload. One-click "Review with defaults" shortcut lets power users jump straight to submit
- **Clickable Stepper Navigation** - Users can click any previously-visited step to go back and edit. Dot + label is a single clickable button per step (Fitts's Law / Apple HIG 44×44pt touch-target compliance)
- **Decomposed Monolith** - The 4,603-line `FileUploader.svelte` split into a 1,294-line coordinator plus 9 focused components under `frontend/src/components/upload/` (each under ~470 lines). New `upload-shared.css` provides a unified chip/dropdown pattern reused across tags and collections
- **Conditional Extraction Step** - Large video files (>100MB by default) trigger an inline Extract step with radio-button choice (Extract Audio Only vs Upload Full Video). Extraction runs on final Submit, not at selection time, so users can still change their mind while stepping through tags/collections
- **Backdrop-Click No Longer Closes** - Modal only closes via X button or Escape key, preventing data loss from stray clicks on in-progress upload state

#### Skeleton Loaders on Major Pages
- **Structural Loading States** - Replaced generic `<Spinner size="large">` on home gallery, search results, file detail page, and speaker clusters/profiles/inbox with skeleton components that mirror the final layout. Perceived load time ~20% faster per Nielsen Norman research
- **Reusable Skeleton Components** - New `FileDetailSkeleton.svelte` (full 2-column layout with header/video/transcript), `ui/CardGridSkeleton.svelte` (parametric with media/profile/search variants), and `ui/ListRowSkeleton.svelte` (avatar + title + actions rows)
- **Gallery Click Feedback** - Clicking a file card now dims + scales it instantly (opacity 0.72, scale 0.985) with `pointer-events: none` to prevent double-clicks. Prefetch kicks off on `mousedown` ~50-100ms before the click handler runs

#### Collection & Share Modal Polish
- **Help Text and Empty States** - Create/Edit Collection modals gained intro banners explaining what collections are, field hints with `maxlength` indicators, and proper `aria-labelledby` wiring
- **Universal Content Analyzer Default** - New collections auto-select the system-default prompt (via `is_system_default` lookup), matching the behavior users typically want without requiring manual selection
- **Share Modal Intro and Permission Guide** - Share Collection modal now includes an introductory explanation, a collection name banner with folder icon, and a visible permission-level reference card showing Viewer/Editor labels inline with descriptions (previously only in tooltips). Empty state added for collections with no existing shares
- **Manage Collections Visual Fix** - Fixed nested-card glitch where the inner `.collections-panel` had its own surface background inside the outer modal container, producing a visible "card in a card" look

#### Unified Color System
- **2-Color Toolbar** - Gallery toolbar replaced a 7-color rainbow (blue, purple, green, amber, red, gray, purple) with a consistent 2-color system per Apple HIG: primary blue for the main action (Upload, Process), surface/gray for all secondary actions (Collections, Select, Organize), red for destructive only (Delete)
- **Purple Removed from UI** - All purple button and badge colors (`#8b5cf6`, `#7c3aed`, `#a855f7`) replaced across 9 components: gallery toolbar, speaker cluster Split button, shared-permission badge, AI suggestion indicators, AI tag/collection chips, LLM analysis badge, and search source-speaker badge. Speaker diarization palette and FedRAMP CUI classification banner retain purple as intentional domain-specific colors
- **AI Accent Color Variable** - New `--ai-accent-color: var(--primary-color)` in `theme.css` replaces scattered purple `#a855f7` fallbacks. AI-suggested tags, collections, and LLM analysis indicators now inherit the primary blue through the CSS cascade
- **Dark Mode Hover Direction Fixed** - `--primary-hover` changed from `#93c5fd` (lighter) to `#3b82f6` (darker). Hover should always darken per Apple HIG — the old lighter hover made buttons appear to deactivate on interaction. Same fix applied to `--link-hover`

### Security

#### Frontend Session Hardening
- **Flash of Authenticated Content (FOAC) fix** - `+layout.svelte` now gates all protected content behind `authReady && isAuthenticated && !isPublicPath`, showing a loading screen in route-mismatch states while async redirects are in flight. Previously, unauthenticated users hitting `/` briefly saw the gallery slot render before the redirect fired, leaking ~1-2 frames of protected UI and triggering `/files` API calls
- **Centralized User State Cleanup** - New `frontend/src/lib/session/clearUserState.ts` is the single source of truth for session teardown. Clears 17+ subsystems on every login/logout transition: toast, websocket, uploads, gallery filters, search results, sharing, LLM status, settings modal, transcript, groups, downloads, notifications, recording (with media track cleanup), thumbnail cache, media URL cache, speaker colors, plus user-scoped localStorage keys. Preferences (theme, locale, view mode, recording settings) are explicitly preserved. Replaces ad-hoc cleanup previously scattered across `auth.ts`
- **Session-Scoped Request Cancellation** - Session-scoped `AbortController` in `lib/axios.ts` attached to every request via interceptor (except `/auth/login`, `/auth/logout`, `/auth/token/refresh` which must always complete). `logout()` now calls `abortAllRequests()` before `clearUserState()`, closing the race window where a late API response could repopulate a cleared store with stale data from the previous session. New `isRequestCancelled()` helper exported for catch blocks to suppress error toasts on cancelled requests
- **bfcache Invalidation on Back Button** - `+layout.svelte` now listens for `pageshow` events with `event.persisted === true` and forces `window.location.reload()` to discard the restored DOM/JS snapshot. Prevents users from hitting the back button after logout and seeing the previously-protected page restored from memory on shared devices
- **Toast Cross-Session Leak Fixed** - `toastStore.clear()` is called from every login success path (local, Keycloak callback, PKI, MFA) and from `logout()` via `clearUserState()`. Previously, notifications from User A's session could persist into User B's login screen or the next user's session
- **Keycloak Redirect URL Validation** - `loginWithKeycloak()` now parses and validates the `authorization_url` returned by `/auth/keycloak/login` (requires `http:` or `https:` protocol) before calling `window.location.href`. Prevents open-redirect or `javascript:`/`data:` URL injection if upstream config drifts

#### XSS Hardening
- **DOMPurify-Backed HTML Sanitization** - New `lib/utils/sanitizeHtml.ts` provides `sanitizeHighlightHtml()` (whitelist allows `mark`, `span`, `br`, `ul`, `li`, `em`, `strong`, `div`, `p` with `class` and `data-match-index` attributes) and `sanitizeToPlainText()`. Added `dompurify` and `@types/dompurify` as dependencies
- **Defense-in-Depth Across 8 Render Sites** - Wrapped every `{@html}` directive that renders API-sourced or LLM-generated content with `sanitizeHighlightHtml()`: TopicsList, TranscriptDisplay, TranscriptModal, SearchTranscriptModal, SearchOccurrence, SearchResultCard, SummaryDisplay
- **Bypassable Regex Sanitizer Replaced** - `SearchOccurrence.svelte` and `SearchResultCard.svelte` previously used `html.replace(/<(?!\/?mark[\s>])[^>]*>/g, '')` which was bypassable via `</mark><script>alert(1)</script><mark>` payloads (the regex only matched opening tags). Now uses DOMPurify with a strict tag whitelist

#### Build & Configuration Hardening
- **Production Source Maps Disabled** - `vite.config.ts` now uses `sourcemap: mode !== 'production'`, ensuring `.js.map` files are only generated for dev/preview builds. Previously, production builds shipped source maps exposing variable names, API endpoint URIs, error messages, and full business logic to any visitor via DevTools or automated crawlers
- **Defense-in-Depth Home Page Guard** - `routes/+page.svelte` `onMount` now early-returns if `!get(isAuthenticated)`, preventing `fetchFiles()` and WebSocket subscriptions from running if the component is somehow mounted unauthenticated (belt-and-suspenders beyond the layout-level route guard)

### Changed

- **Default Whisper Model** - Changed from `large-v2` to `large-v3-turbo` for significantly faster transcription with maintained accuracy
  - New default: `WHISPER_MODEL=large-v3-turbo` (6x faster, excellent for English and most languages)
  - For translation to English: Use `WHISPER_MODEL=large-v3` (large-v3-turbo cannot translate)
  - For maximum accuracy: Use `WHISPER_MODEL=large-v3` (slightly better accuracy than turbo)
- **PyAnnote Embedding Dimension** - v4 uses 256-dim embeddings instead of 192-dim for better voice matching
- **Speaker Embedding Storage** - Database schema updated to support v3/v4 dual-mode during migration
- **Authentication Configuration** - Moved from environment variables to database for better security and manageability
- **Model Caching** - Improved caching strategy with warm-start support and automatic prefetching
- **Word-Level Timestamps** - Now native for all 100+ languages via cross-attention DTW (previously only ~42 languages supported via wav2vec2 alignment model)
- **Transcription Pipeline** - Consolidated into a single unified pipeline; removed separate parallel pipeline and WhisperX service layer

### Removed

- **wav2vec2 Alignment Model** - No longer needed; word-level timestamps are now native via faster-whisper cross-attention DTW
- **`whisperx_service.py`** - Removed separate WhisperX service abstraction (functionality merged into unified pipeline)
- **`parallel_pipeline.py`** - Removed parallel pipeline module (replaced by unified pipeline)
- **`pyannote_compat.py`** - Removed PyAnnote compatibility shim
- **`fast_speaker_assignment.py`** - Removed custom speaker assignment utility (using WhisperX built-in assignment)
- **`batched_alignment.py`** - Removed batched alignment utility (alignment no longer needed)
- **`ENABLE_ALIGNMENT` env var** - Deprecated and ignored (alignment is always-on natively)
- **`TRANSCRIPTION_ENGINE` env var** - Deprecated and ignored (single unified engine)

### Breaking Changes

- **Authentication Configuration**: Auth settings now configured via Super Admin UI (Settings → Authentication) instead of environment variables. Database configuration takes precedence if set.
- **PyAnnote Migration**: Existing installations may need to migrate speaker embeddings for optimal overlap detection (optional but recommended)
- **wav2vec2 Alignment Model Removed**: The separate wav2vec2 alignment model is no longer used. Word-level timestamps are now provided natively by faster-whisper cross-attention DTW. The `ENABLE_ALIGNMENT` and `TRANSCRIPTION_ENGINE` environment variables are deprecated and silently ignored.

### Fixed

- Speaker overlap detection accuracy improved
- Neural search relevance and ranking improved (hybrid search was silently falling back to BM25-only due to OpenSearch 3.4 crash)
- Authentication rate limiting prevents brute force attacks
- PKI certificate validation with OCSP/CRL revocation checking
- OpenSearch cosine similarity scores now correctly converted from OS range `(1+cos)/2` to raw cosine
- Speaker profile centroid embeddings now correctly averaged across all constituent embeddings
- GPU memory leaks fixed (CPU worker CUDA context initialization, prefork child VRAM leak)
- HuggingFace gated model authentication for PyAnnote diarization
- Login flicker and empty-state flash on navigation eliminated
- YouTube bot-bypass anti-blocking with 2026 yt-dlp best practices (Deno JS runtime, client rotation)
- Admin bypass and shared editor access across all API endpoints
- Alembic migration chain linearized after branch merges
- LDAP user bcrypt crash when verifying non-local passwords
- **WebSocket notification queue leak** - `clearAll()` now called on logout; previously persisted in localStorage across sessions, exposing User A's notification history to User B on shared devices
- **Upload queue persistence leak** - `localStorage['upload_queue']` is now cleared on logout via new `uploadsStore.reset()`; previously leaked file UUIDs, metadata, and processing status across sessions
- **Dropdown clipping in upload modal** - Removed nested `overflow-y: auto` on the stepper body that was clipping tag and collection dropdowns. Primary modal container now handles all scrolling with `z-index: 200` on the dropdown list
- **Double-card visual in Manage Collections** - `.collections-panel` previously had its own `surface-color` background + border inside the outer modal container, producing a visible "card in a card" look. Root set to `background: transparent` when rendered inside the modal
- **Debug console.logs removed** - `AuthenticationSettings.svelte` no longer logs full auth config on every load; `files/[id]/+page.svelte` no longer logs every 5 minutes on video URL refresh
- **Dead code removed** - Deleted unused `routes/Tasks.svelte.old` (868 lines) and the unused `AudioExtractionModal.svelte` (replaced by inline stepper step)
- **Avatar lazy-loading** - Profile and cluster avatars on the Speakers page now use `loading="lazy"` and `decoding="async"`, preventing synchronous load-block on page init
- **Dark mode hover direction** - `--primary-hover` was lighter than `--primary-color` in dark mode (`#93c5fd` vs `#60a5fa`), making buttons appear to deactivate on hover. Fixed to `#3b82f6` (darker) for consistent interaction feedback across both themes

### Upgrade Notes

#### Standard Upgrade (Non-Breaking)

```bash
# Pull latest images
docker compose pull

# Restart services (automatically runs migrations)
docker compose up -d
```

After upgrading, users should **hard-reload the frontend** (Ctrl+Shift+R / Cmd+Shift+R) to pick up the new service worker and clear any stale cached assets. The service worker will automatically cache the new build on next visit.

The system will automatically detect the authentication configuration mode and function correctly. To use new authentication features:

1. Log in as super admin
2. Navigate to Settings → Authentication
3. Enable desired authentication methods
4. Configure each method in its dedicated section

#### PyAnnote v4 Migration (Optional)

To take advantage of new speaker overlap detection and improved performance:

1. Navigate to Settings → Embeddings
2. Click "Migrate to PyAnnote v4"
3. Monitor progress with the real-time progress bar
4. No restart required

#### Model Selection for Your Language

- **English audio**: Keep default `large-v3-turbo` for fastest transcription
- **Non-English (no translation needed)**: Keep default `large-v3-turbo` for 6x faster speed
- **Translation to English**: Switch to `large-v3` (turbo cannot translate)
  - In Settings → Transcription → Model Selection, choose `large-v3`
- **Maximum accuracy needed**: Switch to `large-v3` for best overall accuracy
  - In Settings → Transcription → Model Selection, choose `large-v3`

#### wav2vec2 Model Cache Cleanup (Optional)

The wav2vec2 alignment model is no longer used. You can reclaim ~360MB of disk space by removing it from your model cache:

```bash
# Remove wav2vec2 alignment model cache (~360MB)
rm -rf ${MODEL_CACHE_DIR:-./models}/torch/hub/checkpoints/wav2vec2_*
```

No reprocessing of existing transcriptions is needed -- existing word-level timestamps are preserved.

#### Environment Variable Cleanup (Optional)

The following environment variables are deprecated and silently ignored. You may remove them from your `.env` file:

```bash
# These can be safely removed from .env:
# ENABLE_ALIGNMENT=true        (alignment is now always-on natively)
# TRANSCRIPTION_ENGINE=whisperx (single unified engine, setting ignored)
```

### Contributors

Special thanks to the community members whose code contributions and issue reports shaped this release:

**Code Contributors:**
- [@vfilon](https://github.com/vfilon) (Vitali Filon) — Implemented the entire LDAP/Active Directory authentication feature (PR #117): initial auth engine, username attribute support, auth_type handling, password change restrictions for non-local users, conditional settings UI, documentation, and migration detection logic (9 commits)
- [@imorrish](https://github.com/imorrish) (Ian Morrish) — Submitted PR #117, contributed the Postgres password reset guide to the troubleshooting docs (PR #1)

**Issue Reports Implemented:**
- [@imorrish](https://github.com/imorrish) — #129 scrollable speaker dropdown, #138 filename in AI summary template, #145 collection/tag selection at upload, #146 per-collection default AI prompt
- [@it-service-gemag](https://github.com/it-service-gemag) — #151 disable diarization per upload, #152 disable AI summary per upload, #153 per-transcription Whisper model selection
- [@Politiezone-MIDOW](https://github.com/Politiezone-MIDOW) — #134 file retention and auto-deletion system
- [@coltrall](https://github.com/coltrall) — #137 Docker daemon detection in installation script
- [@SQLServerIO](https://github.com/SQLServerIO) (Wes Brown) — #109 pagination for large transcripts (file detail page hang with thousands of segments)

---

## [0.3.3] - 2025-01-13

### Overview
Community contributions release featuring Russian language support, protected media authentication for corporate video portals, and various bug fixes and improvements.

Special thanks to [@vfilon](https://github.com/vfilon) for contributing all four PRs in this release!

### Added

#### Internationalization
- **Russian Language Support** - Added Russian (Русский) as the 8th supported UI language (#114)
- **Protected Media Translations** - Added translations for protected media feature to all 7 non-English languages

#### Protected Media Authentication (#115)
- **Plugin Architecture** - New extensible plugin system for authenticated media downloads from corporate/internal video portals
- **MediaCMS Provider** - Built-in support for MediaCMS installations requiring authentication
- **Frontend UI** - Username/password fields appear automatically when entering URLs from configured protected media hosts
- **Security** - Credentials are transmitted securely and never stored in the database

#### URL Utilities (#116)
- **Centralized URL Construction** - New `getFlowerUrl()`, `getAppBaseUrl()`, and `getVideoUrl()` utilities for consistent URL handling across dev and production environments

### Fixed

- **VRAM Monitoring** - Added validation for VRAM monitoring keys to prevent KeyError on non-CUDA devices (#113)
- **Loading Screen** - Fixed "app.loadingApplication" raw key displaying during initial page load by using hardcoded text before i18n initializes

### Changed

- **Flower Dashboard** - Refactored URL construction to use centralized utility function
- **Video Playback** - Updated video URL construction to work correctly behind nginx reverse proxy

### Upgrade Notes

Standard Docker Compose update:
```bash
docker compose pull
docker compose up -d
```

To use protected media authentication, configure allowed hosts in `.env`:
```bash
MEDIACMS_ALLOWED_HOSTS=media.example.com,mediacms.internal
```

---

## [0.3.2] - 2025-12-17

### Overview
Patch release fixing critical bugs in the one-liner installation script that prevented successful setup on fresh installations.

**Note:** This is a scripts-only release. No Docker container rebuild required.

### Fixed

#### Setup Script Fixes
- **Scripts Directory Creation** - Fixed curl error 23 ("Failure writing output to destination") when downloading SSL and permission scripts by creating the `scripts/` directory before download attempts
- **PyTorch 2.6+ Compatibility** - Applied `torch.load` patch to `download-models.py` for PyTorch 2.6+ compatibility, mirroring the fix already present in the backend (from Wes Brown's commit 8929cd6)
  - PyTorch 2.6 changed `weights_only` default to `True`, causing omegaconf deserialization errors during model downloads
  - The patch sets `weights_only=False` for trusted HuggingFace models

### Upgrade Notes

For existing installations, no action required - Docker containers already have the PyTorch fix.

For new installations, the one-liner setup script will now work correctly:
```bash
curl -fsSL https://raw.githubusercontent.com/davidamacey/OpenTranscribe/master/setup-opentranscribe.sh | bash
```

---

## [0.3.1] - 2025-12-16

### Overview
Patch release with enhanced setup scripts, HTTPS/SSL support improvements, and comprehensive documentation updates for v0.2.0 and v0.3.0 features.

### Added

#### Setup Script Enhancements
- **HTTPS/SSL Setup Command** - New `./opentranscribe.sh setup-ssl` interactive command for easy SSL configuration
- **Version Command** - New `./opentranscribe.sh version` to check current version and available updates
- **Update Commands** - New `update` (containers only) and `update-full` (containers + config files) commands
- **NGINX Auto-Detection** - Automatic NGINX overlay loading when `NGINX_SERVER_NAME` is configured
- **NGINX Health Check** - Added NGINX health monitoring to `./opentr.sh health`

#### Documentation
- **NGINX Setup Guide** - Comprehensive `docs-site/docs/configuration/nginx-setup.md` with homelab and Let's Encrypt instructions
- **Universal Media URL Docs** - Updated documentation to reflect 1800+ platform support via yt-dlp
- **Garbage Cleanup Docs** - Added documentation for auto-cleanup of erroneous transcription segments
- **System Statistics FAQ** - Added FAQ entry explaining how to view system resource usage
- **Large Transcript Pagination FAQ** - Added FAQ entry about automatic pagination for long transcripts

### Changed

- **Setup Script** - Downloads NGINX configuration files during initial setup
- **Management Script** - Displays HTTPS URLs when NGINX/SSL is configured
- **Documentation** - Updated all README and Docusaurus docs to cover v0.2.0 and v0.3.0 features

### Upgrade Notes

For existing installations, run the full update to get new scripts:
```bash
./opentranscribe.sh update-full
```

Or manually update scripts:
```bash
curl -fsSL https://raw.githubusercontent.com/davidamacey/OpenTranscribe/master/opentranscribe.sh -o opentranscribe.sh
chmod +x opentranscribe.sh
```

---

## [0.3.0] - 2025-12-15

### Overview
Major feature release integrating valuable contributions from the [@vfilon](https://github.com/vfilon) fork, along with critical UUID/ID standardization fixes and production infrastructure improvements.

### Added

#### Universal Media URL Support
- **1800+ Platform Support** - Expand beyond YouTube to support virtually any video platform via yt-dlp
- **Dynamic Source Detection** - Automatically detect source platform from yt-dlp metadata
- **User-Friendly Error Handling** - Clear messages for authentication-required platforms
- **Platform Guidance** - Helpful messages for common platforms (Vimeo, Instagram, TikTok, etc.)
- **Recommended Platforms** - YouTube, Dailymotion, Twitter/X highlighted as best supported

#### NGINX Reverse Proxy with SSL/TLS (Closes [#72](https://github.com/davidamacey/OpenTranscribe/issues/72))
- **Production-Ready SSL** - Full NGINX reverse proxy configuration for HTTPS deployments
- **docker-compose.nginx.yml** - Optional overlay for production environments
- **SSL Certificate Generation** - Script for self-signed certificates (`scripts/generate-ssl-cert.sh`)
- **WebSocket Proxy** - Full WebSocket support through NGINX
- **Large File Uploads** - 2GB upload support for large media files
- **Service Proxying** - Flower dashboard and MinIO console accessible through NGINX
- **Browser Microphone Recording** - Enabled on remote/network access via HTTPS

#### Infrastructure Improvements
- **GPU Overlay Separation** - `docker-compose.gpu.yml` for optional GPU support on cross-platform systems
- **Task Status Reconciliation** - Better handling of stuck tasks with multiple timestamp fallbacks
- **Auto-Refresh Analytics** - Analytics refresh when segment speaker changes
- **Ollama Context Window** - Configurable `num_ctx` parameter for Ollama LLM provider
- **Model-Aware Temperature** - Temperature handling based on model capabilities
- **Explicit Docker Image Names** - Cache efficiency with named images

#### Documentation
- **NGINX Setup Guide** - Comprehensive `docs/NGINX_SETUP.md` documentation
- **Fork Comparison** - `docs/FORK_COMPARISON_vfilon.md` with detailed analysis
- **Implementation Plan** - `docs/FORK_IMPLEMENTATION_PLAN.md` checklist
- **Test Videos** - `docs/testing/media_url_test_videos.md` with platform test URLs

### Changed

#### Backend
- **Service Rename** - `youtube_service.py` → `media_download_service.py` for platform-agnostic naming
- **URL Validation** - Generic HTTP/HTTPS URL pattern instead of YouTube-specific
- **Minio Version** - Updated minimum version to 7.2.18

#### Frontend
- **Media URL UI** - Renamed `youtubeUrl` → `mediaUrl` throughout FileUploader
- **Notification Text** - Changed "YouTube Processing" → "Video Processing" (all 7 languages)
- **Platform Info** - Added collapsible "Supported Platforms" section with limitations warning
- **WebSocket Token Encoding** - Added `encodeURIComponent()` for auth tokens

### Fixed

#### UUID/ID Standardization (60+ files)
- **Speaker Recommendations** - Fixed recommendations not showing for new videos
- **Profile Embedding Service** - Fixed returning UUID as `profile_id` when integer expected
- **Consistent ID Handling** - Backend uses integer IDs for DB, UUIDs for API responses
- **Frontend UUIDs** - All entity references now use UUID strings consistently
- **Comment System** - Fixed UUID handling in comments
- **Password Reset** - Fixed password reset flow
- **Transcript Segments** - Fixed segment update UUID handling

### Contributors

Special thanks to:
- **[@vfilon](https://github.com/vfilon)** - Original fork contributions (Universal Media URL concept, NGINX configuration, task reconciliation)

### Upgrade Notes

Users running self-hosted deployments should pull the latest images:
```bash
docker pull davidamacey/opentranscribe-frontend:v0.3.0
docker pull davidamacey/opentranscribe-backend:v0.3.0
```

For NGINX/SSL setup, see `docs/NGINX_SETUP.md`.

---

## [0.2.1] - 2025-12-13

### Overview
Security patch release addressing critical container vulnerabilities identified in security scans.

### Security

#### Container Base Image Updates
- **Frontend**: Upgraded `nginx:1.29.3-alpine3.22` → `nginx:1.29.4-alpine3.23`
- **Backend**: Upgraded `python:3.12-slim-bookworm` → `python:3.13-slim-trixie` (Debian 12 → Debian 13)

#### Resolved Critical CVEs (4 → 0)
- **CVE-2025-47917** (libmbedcrypto) - CRITICAL - Fixed in 3.6.4-2
- **CVE-2023-6879** (libaom3) - CRITICAL - Fixed in 3.12.1-1
- **CVE-2025-7458** (libsqlite3) - CRITICAL - Fixed in 3.46.1-7
- **CVE-2023-45853** (zlib) - CRITICAL - Fixed in 1.3.1

#### Frontend Security Fixes
- Fixed 3 HIGH severity libpng vulnerabilities
- Fixed 2 MEDIUM severity libpng vulnerabilities
- Fixed 1 MEDIUM severity busybox vulnerability
- Remaining: 3 tiff CVEs (no Alpine fix available)

#### Additional Improvements
- Added `HEALTHCHECK` instructions to both frontend and backend Dockerfiles
- Updated Python from 3.12 to 3.13
- Updated pip to latest version (25.3)

### Changed
- Backend now runs on Debian 13 "trixie" (released August 2025)
- Python site-packages path updated from 3.12 to 3.13

### Upgrade Notes
Users running self-hosted deployments should pull the latest images:
```bash
docker pull davidamacey/opentranscribe-frontend:v0.2.1
docker pull davidamacey/opentranscribe-backend:v0.2.1
```

---

## [0.2.0] - 2025-12-12

### Overview
Community-driven multilingual release! This version features significant contributions from the open source community, including 7 pull requests from [@SQLServerIO](https://github.com/SQLServerIO) (Wes Brown) and a critical multilingual feature request from [@LaboratorioInternacionalWeb](https://github.com/LaboratorioInternacionalWeb).

### Added

#### Multilingual Transcription Support
- **100+ Language Support** - Expanded from 50+ to 100+ languages via WhisperX
- **Configurable Source Language** - Auto-detect or manually specify source language for improved accuracy
- **Translation Toggle** - Choose to keep original language or translate to English (default: keep original)
- **Word-Level Alignment Indicators** - UI shows which languages (~42) support word-level timestamps
- **LLM Output Language** - Generate AI summaries in 12 languages (EN, ES, FR, DE, PT, ZH, JA, KO, IT, RU, AR, HI)

#### UI Internationalization (i18n)
- **7 UI Languages** - English, Spanish, French, German, Portuguese, Chinese, Japanese
- **Language Settings** - User-configurable UI language preference
- **Locale Store** - Persistent language preference with localStorage
- **Translation System** - Comprehensive i18n system across all frontend components

#### Speaker Management Enhancements
- **Speaker Merge UI** - Visual interface to combine duplicate speakers with segment preview
- **Segment Reassignment** - Automatic segment speaker reassignment during merge
- **Per-File Speaker Settings** - Configure min/max speakers at upload or reprocess time
- **User-Level Speaker Preferences** - Save default speaker detection settings (always prompt, use defaults, use custom)

#### LLM Integration Improvements
- **Anthropic Model Discovery** - Native /v1/models API for dynamic model listing
- **Model Auto-Discovery** - Extended to support vLLM, Ollama, and Anthropic providers
- **Edit Mode API Key Support** - Stored API keys work in edit mode (no need to re-enter)
- **Updated Default Models** - Anthropic: claude-opus-4-5-20251101, Ollama: llama3.2:latest
- **Improved Configuration UX** - Toast notifications replace inline errors, better API key toggle positioning

#### User Settings
- **Transcription Settings** - User-level transcription preferences stored in database
- **Garbage Cleanup Settings** - User-configurable automatic cleanup of erroneous segments
- **Automatic Database Migrations** - Migrations run automatically on startup

#### Admin & System
- **System Statistics** - CPU, memory, disk, and GPU usage visible to all authenticated users
- **Admin Password Reset** - Secure password reset with validation
- **Compact Action Buttons** - Icon-only action buttons with tooltips in admin UI

### Changed

- **Provider Consolidation** - `claude` provider deprecated in favor of `anthropic`
- **LLM Provider Enum** - Reordered with legacy CLAUDE at end
- **Error Display** - Converted inline errors to toast notifications in LLM config modal

### Fixed

- **Large Transcript Pagination** - Fixed page hanging with thousands of segments ([PR #110](https://github.com/davidamacey/OpenTranscribe/pull/110))
- **Garbage Segment Cleanup** - Automatic detection and removal of erroneous transcription segments ([PR #107](https://github.com/davidamacey/OpenTranscribe/pull/107))
- **UUID Admin Endpoints** - Fixed admin endpoints to use UUID instead of integer ID ([PR #106](https://github.com/davidamacey/OpenTranscribe/pull/106))
- **PyTorch 2.6+ Compatibility** - Updated for newer PyTorch versions ([PR #102](https://github.com/davidamacey/OpenTranscribe/pull/102))
- **vLLM Endpoint Configuration** - Fixed summaries not working with vLLM in OpenAI mode ([Issue #100](https://github.com/davidamacey/OpenTranscribe/issues/100))
- **API Key Whitespace** - Added .trim() to all API key validations
- **Race Conditions** - Fixed race conditions when editing existing LLM configurations
- **Speaker Dropdown Visibility** - Fixed flickering and visibility issues

### Code Quality

- **Reduced Cyclomatic Complexity** - Refactored 47 functions across 27 files
- **ESLint Integration** - Improved frontend linting and type safety
- **Removed Unused Code** - Cleaned up unused error variables and CSS classes

### Contributors

Special thanks to our community contributors:
- [@SQLServerIO](https://github.com/SQLServerIO) (Wes Brown) - 7 pull requests
- [@LaboratorioInternacionalWeb](https://github.com/LaboratorioInternacionalWeb) - Multilingual feature request

## [0.1.0] - 2025-11-05

### Overview
First official release of OpenTranscribe! This release marks the transition from internal development to public availability. What started as a weekend experiment in May 2025 has evolved into a full-featured, production-ready AI transcription platform over 6 months of dedicated development.

### Added

#### Core Transcription Features
- **WhisperX Integration** - High-accuracy speech recognition with faster-whisper backend
- **Word-Level Timestamps** - Precise timing for every word using cross-attention DTW
- **Multi-Language Support** - Transcribe in 50+ languages with automatic English translation
- **GPU Acceleration** - 70x realtime speed with large-v2 model on NVIDIA GPUs
- **CPU Fallback** - Complete CPU-only mode for systems without GPUs
- **Apple Silicon Support** - MPS acceleration for M1/M2/M3 Macs
- **Batch Processing** - Process multiple files concurrently with intelligent queue management

#### Speaker Diarization & Management
- **Automatic Speaker Detection** - PyAnnote.audio integration for speaker identification
- **Cross-Video Speaker Recognition** - AI-powered voice fingerprinting to match speakers across different media files
- **Speaker Profile System** - Global speaker profiles that persist across all transcriptions
- **Voice Similarity Analysis** - Advanced embedding-based speaker matching with confidence scores
- **LLM-Enhanced Speaker Identification** - Content-based speaker name suggestions using conversational context
- **Manual Verification Workflow** - Accept/reject AI suggestions to improve accuracy over time
- **Speaker Analytics** - Talk time distribution, cross-media appearances, and interaction patterns
- **Configurable Speaker Limits** - Support for 1-20 speakers by default, scalable to 50+ for large conferences
- **Auto-Profile Creation** - Automatic speaker profile creation when speakers are labeled
- **Retroactive Speaker Matching** - Cross-video matching with automatic label propagation

#### Media Support & Processing
- **Universal Format Support** - Audio (MP3, WAV, FLAC, M4A, OGG, AAC) and Video (MP4, MOV, AVI, MKV, WEBM)
- **YouTube Integration** - Direct URL processing with automatic video download
- **YouTube Playlist Support** - Extract and queue all videos from playlists for batch transcription
- **Large File Support** - Upload files up to 4GB (supports GoPro and high-quality video content)
- **Interactive Media Player** - Plyr-based player with click-to-seek transcript navigation
- **Audio Waveform Visualization** - Interactive waveform with precise timing and click-to-seek
- **Browser Microphone Recording** - Built-in microphone recording with real-time audio level monitoring (works over localhost or HTTPS)
- **Background Recording** - Record audio in the background while using other application features
- **Recording Controls** - Pause/resume recording with duration tracking and quality settings
- **Custom File Titles** - Edit display names for media files with real-time search index updates
- **Metadata Extraction** - Comprehensive file information using ExifTool
- **Subtitle Export** - Generate SRT/VTT files for accessibility
- **File Reprocessing** - Re-run AI analysis while preserving user comments and annotations
- **Auto-Recovery System** - Intelligent detection and recovery of stuck or failed file processing

#### Upload & File Management
- **Advanced Upload Manager** - Floating, draggable upload interface with real-time progress tracking
- **Concurrent Upload Processing** - Multiple file uploads with intelligent queue management
- **Drag-and-Drop Support** - Intuitive file upload interface with direct media file upload
- **Video File Size Detection** - Automatic detection of large video files with client-side audio extraction option to reduce upload size and processing time
- **Client-Side Audio Extraction** - Extract audio from video files in the browser before upload for faster processing and reduced bandwidth
- **Duplicate Detection** - Hash-based verification to prevent duplicate uploads
- **Automatic Recovery** - Retry logic for failed uploads with exponential backoff
- **Background Upload Processing** - Seamless integration with background task queue
- **YouTube URL Upload** - Direct video processing from YouTube URLs without manual download
- **YouTube Playlist Batch Upload** - Process entire YouTube playlists via URL with automatic queuing

#### AI-Powered Features
- **LLM Integration** - Support for 6+ providers (OpenAI, Anthropic Claude, vLLM, Ollama, OpenRouter, Custom)
- **AI-Powered Summaries** - Generate comprehensive summaries with customizable formats and structures
- **BLUF Format Summaries** - Bottom Line Up Front structured summaries with action items, key decisions, and follow-ups
- **Custom AI Prompts** - Create and manage unlimited AI prompts with ANY JSON structure
- **Flexible Schema Storage** - JSONB storage supporting multiple prompt types simultaneously
- **Intelligent Section Processing** - Automatic context-aware processing (single or multi-section) based on transcript length
- **Section-by-Section Analysis** - Handles transcripts of any length with intelligent chunking at speaker/topic boundaries
- **LLM Configuration Management** - User-specific LLM settings with encrypted API key storage
- **Provider Testing** - Test LLM connections and validate configurations before use
- **AI-Powered Topic Generation** - Automatic topic extraction from transcript content for intelligent tag suggestions
- **AI-Generated Collections** - Intelligent collection suggestions based on content analysis and topic clustering
- **Smart Tag Recommendations** - AI-powered tag suggestions based on transcript content, speakers, and themes
- **Real-Time Topic Extraction** - AI-powered topic extraction with granular progress notifications
- **Speaker Name Suggestions** - LLM-powered speaker identification based on conversation context
- **Local & Cloud Processing** - Support for both privacy-first local models and cloud AI providers

#### Search & Discovery
- **Hybrid Search** - Combine keyword and semantic search capabilities using OpenSearch 3.3.1
- **Full-Text Indexing** - Lightning-fast content search with Apache Lucene 10
- **9.5x Faster Vector Search** - Significantly improved semantic search performance
- **25% Faster Queries** - Enhanced full-text search with lower latency
- **75% Lower p90 Latency** - Improved aggregation performance
- **Advanced Filtering** - Filter by speaker, date, tags, duration, and more with searchable dropdowns
- **Smart Tagging** - Organize content with custom tags and categories
- **Collections System** - Group related media files into organized collections for better project management
- **Speaker Usage Counts** - Track which speakers appear most frequently across your media library
- **Inline Collection Editing** - Tag-style interface for managing file collections
- **Searchable Dropdowns** - Enhanced filter UI for better usability

#### Analytics & Insights
- **Advanced Content Analysis** - Comprehensive speaker analytics including talk time, interruptions, and turn-taking patterns
- **Speaker Performance Metrics** - Speaking pace (WPM), question frequency, and conversation flow analysis
- **Meeting Efficiency Analytics** - Silence ratio analysis and participation balance tracking
- **Real-Time Analytics Computation** - Server-side analytics with automatic refresh capabilities
- **Cross-Video Speaker Analytics** - Track speaker patterns and participation across multiple recordings

#### User Interface & Experience
- **Progressive Web App** - Installable app experience with offline capabilities
- **Responsive Design** - Optimized for desktop, tablet, and mobile devices
- **Interactive Waveform Player** - Click-to-seek audio visualization with precise timing
- **Floating Upload Manager** - Draggable upload interface with real-time progress
- **Smart Modal System** - Consistent modal design with improved accessibility
- **Timestamp-Based Comments** - Add user comments anchored to specific timestamps in videos and transcripts
- **Comment Navigation** - Click comments to jump to the corresponding moment in the media playback
- **Annotation System** - Rich annotation capabilities with timestamp markers throughout the transcript
- **Enhanced Data Formatting** - Server-side formatting service for consistent display of dates, durations, and file sizes
- **Error Categorization** - Intelligent error classification with user-friendly suggestions and retry guidance
- **Smart Status Management** - Comprehensive file and task status tracking with formatted display text
- **Auto-Refresh Systems** - Background data updates without manual page refreshing
- **Theme Support** - Seamless dark/light mode switching
- **Keyboard Shortcuts** - Efficient navigation and control via hotkeys
- **Full-Screen Transcript View** - Dedicated modal for reading and searching long transcripts
- **Smart Notification System** - Persistent notifications with unread count badges and progress updates
- **WebSocket Integration** - Real-time updates for transcription, summarization, and upload progress

#### Infrastructure & Performance
- **Docker Compose Architecture** - Base + override pattern for different environments
  - `docker-compose.yml` - Base configuration (all environments)
  - `docker-compose.override.yml` - Development overrides (auto-loaded)
  - `docker-compose.prod.yml` - Production overrides
  - `docker-compose.offline.yml` - Offline/airgapped overrides
  - `docker-compose.gpu-scale.yml` - Multi-GPU scaling configuration
- **Multi-GPU Worker Scaling** - Optional parallel processing on dedicated GPUs (4+ workers per GPU)
- **Specialized Worker Queues** - GPU (transcription), Download (YouTube), CPU (waveform), NLP (AI features), Utility (maintenance)
- **Parallel Waveform Processing** - CPU-based waveform generation runs simultaneously with GPU transcription
- **Non-Blocking Architecture** - LLM tasks don't delay next transcription (45-75s faster per 3-hour file)
- **Configurable Concurrency** - GPU(1-4), CPU(8), Download(3), NLP(4), Utility(2) workers for optimal resource utilization
- **Model Caching System** - Simple volume-based caching (~2.6GB total) with natural cache locations
- **PostgreSQL Database** - Reliable relational database with JSONB support for flexible schemas
- **MinIO Object Storage** - S3-compatible storage for media files
- **OpenSearch 3.3.1** - Full-text and vector search with Apache Lucene 10
- **Redis Message Broker** - High-performance task queue and caching
- **Celery Distributed Tasks** - Background AI processing with multiple specialized queues
- **Flower Monitoring** - Real-time task monitoring and management dashboard
- **NGINX Production Server** - Optimized reverse proxy for production deployments
- **Complete Offline Support** - Full airgapped/offline deployment capability

#### Security & Privacy
- **Non-Root Container User** - Backend containers run as non-root user (appuser, UID 1000)
- **Automatic Permission Management** - Startup scripts automatically fix model cache permissions
- **Principle of Least Privilege** - Reduced security risk from container escape vulnerabilities
- **Security Scanning Integration** - Trivy and Grype integration for vulnerability detection
- **Role-Based Access Control** - Admin/user permissions with file ownership validation
- **Encrypted API Key Storage** - User-specific LLM settings with secure key storage
- **Session Management** - Secure JWT-based authentication
- **Local Processing** - All data stays on your infrastructure (except optional cloud LLM calls)

#### Developer Experience
- **Comprehensive Utility Scripts** - `opentr.sh` and `opentranscribe.sh` for all operations
- **Hot Reload Support** - Development mode with automatic code reloading
- **Database Backup/Restore** - Easy data migration and disaster recovery
- **Service Health Checks** - Container orchestration with health monitoring
- **Docker Build Scripts** - Automated multi-platform builds with security scanning
- **Version Management** - Centralized VERSION file for consistent versioning
- **Code Quality Tooling** - ESLint, TypeScript strict mode, Black, Ruff
- **Comprehensive Documentation** - Docusaurus documentation site with screenshots and guides
- **TypeScript Integration** - Type-safe frontend development
- **API Documentation** - OpenAPI/Swagger automatic API docs

#### Documentation & Resources
- **Complete Documentation Site** - docs.opentranscribe.app with comprehensive guides
- **Visual Screenshots** - Step-by-step visual guides for all features
- **Installation Guides** - Multiple deployment options (Docker Hub, source, offline)
- **Configuration Reference** - Detailed environment variable documentation
- **Troubleshooting Guide** - Common issues and solutions
- **Developer Resources** - Contributing guidelines and architecture documentation
- **Blog** - Release announcements and development updates
- **One-Line Installer** - Quick setup script with hardware detection

### Changed
- **License** - Migrated from MIT to GNU Affero General Public License v3.0 (AGPL-3.0) to protect open source and ensure network copyleft
- **Version Numbering** - Starting at 0.1.0 with path to v1.0.0
- **Documentation Structure** - Migrated to dedicated Docusaurus site for better organization

### Technical Stack

#### Frontend
- Svelte 5.39.9 - Reactive UI framework
- TypeScript 5.9.3 - Type-safe development
- Vite 6.1.7 - Build tool and dev server
- Plyr 3.8.3 - Media player
- Axios 1.12.2 - HTTP client
- FFmpeg.wasm 0.12.15 - Browser-based media processing
- date-fns 4.1.0 - Date formatting
- imohash 1.0.3 - Fast file hashing

#### Backend
- Python 3.11+ - Programming language
- FastAPI - Modern async web framework
- SQLAlchemy 2.0 - ORM with type safety
- Alembic - Database migrations
- Celery - Distributed task queue
- Redis - Message broker and caching
- PostgreSQL - Relational database
- WhisperX - Speech recognition with native word-level timestamps
- PyAnnote.audio - Speaker diarization
- OpenSearch 3.3.1 - Search engine (Apache Lucene 10)
- MinIO - S3-compatible object storage
- Sentence Transformers - Semantic embeddings
- NLTK - Natural language processing
- ExifTool - Metadata extraction
- yt-dlp - YouTube download

#### AI/ML Stack
- faster-whisper - Optimized Whisper inference
- PyAnnote segmentation-3.0 - Speaker segmentation
- PyAnnote speaker-diarization-3.1 - Speaker identification
- faster-whisper cross-attention DTW - Native word-level timestamps
- Sentence Transformers all-MiniLM-L6-v2 - Semantic search (~80MB)
- Multiple LLM provider support (OpenAI, Claude, vLLM, Ollama, OpenRouter)

#### Infrastructure
- Docker & Docker Compose - Containerization
- NGINX - Reverse proxy
- Flower - Celery monitoring
- GitHub Actions - CI/CD

### Performance Benchmarks
- **Transcription Speed** - 70x realtime with large-v2 model on GPU
- **Vector Search** - 9.5x faster than previous generation
- **Query Performance** - 25% faster with 75% lower p90 latency
- **Multi-GPU Scaling** - 4 parallel workers can process 4 videos simultaneously
- **Model Cache Size** - ~2.6GB total for all AI models

### Deployment Options
- **Quick Install** - One-line installer with hardware detection
- **Docker Hub** - Pre-built images for instant deployment
- **Source Build** - Full source code with development environment
- **Offline/Airgapped** - Complete offline deployment support
- **Multi-Platform** - AMD64 and ARM64 support

### Breaking Changes
- None (first release)

### Migration Notes
- This is the first public release - no migration required
- For future releases, we will strive for backwards compatibility
- Breaking changes will be clearly announced in release notes

### Known Issues
- None critical at release time
- See GitHub Issues for community-reported items

### Contributors
- David Macey (@davidamacey) - Project Lead
- OpenTranscribe Community - Testing and feedback

### Links
- **Documentation**: https://docs.opentranscribe.app
- **GitHub Repository**: https://github.com/davidamacey/OpenTranscribe
- **Docker Hub Backend**: https://hub.docker.com/r/davidamacey/opentranscribe-backend
- **Docker Hub Frontend**: https://hub.docker.com/r/davidamacey/opentranscribe-frontend
- **Issues**: https://github.com/davidamacey/OpenTranscribe/issues
- **License**: https://github.com/davidamacey/OpenTranscribe/blob/master/LICENSE

---

## Future Roadmap

Looking ahead to v1.0.0, we plan to add:
- Real-time transcription for live streaming
- Enhanced speaker analytics and visualization
- Better speaker diarization models
- Google-style text search
- LLM powered RAG Chat with transcript text
- Other refinements along the way!

We welcome community feedback and contributions as we work towards the v1.0.0 release!

[0.1.0]: https://github.com/davidamacey/OpenTranscribe/releases/tag/v0.1.0
