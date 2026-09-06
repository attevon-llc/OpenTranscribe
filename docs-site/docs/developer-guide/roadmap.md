---
title: Release Themes
sidebar_label: Release Themes
description: What each OpenTranscribe release is for, and how we know when it is done.
---

# Release themes

**For what is in each release right now, see the [Roadmap](/roadmap).** That page is generated
from the issue tracker, so its issue lists and progress counts are always current.

This page is the part the generated roadmap has no field for: what each release is **for**, and
**how we know it is done**. It deliberately contains no issue lists, no counts and no dates, so
there is nothing on it that can go stale when an issue moves.

Every release has one **theme**, a **goal** in a sentence, and **exit criteria** you can check by
looking at the product and saying yes or no. A release that cannot state its exit criteria is not
a release. It is a pile of issues with a version number on it.

---

## How the version numbers work

OpenTranscribe is pre-1.0 and follows [Semantic Versioning](https://semver.org/). Until 1.0 the
minor number is the unit of release, and each one carries a single theme.

| | Meaning |
|---|---|
| `0.x.0` | A themed release. One goal, stated below. |
| `0.x.y` | A patch on a shipped `0.x.0`. Defects and security fixes only, no new capability. |
| `1.0.0` | The commitment point: a stable API surface, a formal compliance posture, and enough platform breadth to run outside a Linux and CUDA box. |

Three rules keep the numbers honest.

**A release gets one theme, not a date's worth of work.** When a milestone picks up a second
unrelated theme it splits into a new number instead of growing. Version numbers are cheap and
coherence is not, which is why the ladder runs past `0.9.0`. To be clear, `0.10.0` sorts *after*
`0.9.0`. That is correct SemVer, not a typo.

**Small bites, scheduled.** v0.5.0 accumulated an enormous amount of good work and took far too
long to ship as a result. Every release after it is deliberately sized to be finishable.

**Dates come from dependencies, not from ambition.** A release is never dated before one it
depends on. Where a date looks aggressive, argue with the scope rather than the date.

## Between the themed releases: patch releases

A themed `0.x.0` is 6–8 weeks apart. Dependency updates, security fixes and one-line corrections
do not keep that schedule, and holding them until the next minor is how they rot: as of
2026-09-06 the oldest open dependency PR was **18 days** old and one of them bundled **33 backend
updates** into a single change, which is far harder to review and to revert than five small ones
would have been.

So `0.x.y` patch releases run **between** the minors, on their own rhythm.

**What goes in a patch**

- Dependency bumps, especially security ones. Small and frequent beats large and quarterly: a
  bundle of 33 is not reviewable, and when it breaks something you cannot tell which line did it.
- Security fixes that should not wait for a theme.
- Defect fixes with no schema change and no new capability.
- Documentation and packaging corrections.

**What does not**

- New capability. That belongs to a theme, or the version numbers stop meaning anything.
- Alembic migrations. A patch must be safe to skip and safe to roll back; a migration is neither.
- Anything that changes an interface a user or integration depends on.

**The rule that keeps it honest:** a patch release must be **revertible by pulling the previous
image**. If reverting needs a migration, a data fix, or a config change, it was never a patch and
should not have been numbered as one.

Cutting one uses the same `scripts/release.sh` pipeline as a minor. Nothing is hand-run.

## Requirements every release carries

These are not one release's work. They apply to all of them, and a release is not done until they
hold.

- **Translation.** Any user-facing copy added or changed ships in all **12** locales, with `ar`
  checked in RTL. `npm run check:i18n` enforces key *parity* only: a key present in all 12 files
  with English text passes and ships untranslated. It also runs in **CI only**, not as a
  pre-commit hook, so run it locally before pushing.
- **Light and dark parity.** Every UI change is looked at in both themes, in a browser. A
  type-check is not a look.
- **The tests could fail.** New tests are watched failing before the fix. `audit-tests.py` and its
  frontend sibling stay at zero unallowlisted findings.
- **Docs move with the code.** A feature that ships without its docs page is not done.

---

## v0.5.0: Ship what is already built

**Theme:** get the largest body of finished work in the project's history out the door.

**Goal:** publish it without adding one more feature to it.

Feature complete and code frozen. Both the backend and the frontend were substantially rewritten:
native diarization replaced PyAnnote as the default and now runs concurrently with transcription,
chat was rebuilt around corpus-scale retrieval, search gained a unified foundation and working tag
search, content redaction landed across every display and export surface, and the whole
local/LDAP/OIDC/SAML/PKI/MFA/SCIM identity plane arrived at once. A large share of the work went
into speed and refinement of things that already worked.

**The tracker is empty** — the milestone closed at 285 issues. What remains is release execution,
not development.

**Exit criteria**

- Tagged, with a GitHub Release marked `latest`.
- The CPU-only lite image is published with **both** architecture legs, and `--lite` completes an
  install using it.
- Every published container is CVE scanned **on every architecture leg**. A green scan over an
  image nobody looked at is the specific failure this gate exists to catch.
- The upgrade and fresh-install rehearsals both pass against the published images.

:::note The lite image, and why it is not an open issue
`setup-opentranscribe.sh` ships a user-facing `--lite` flag that pulls
`opentranscribe-backend-lite`, and that image has never been pushed — lite is also the only
backend an arm64 user can run. It is nonetheless **not** tracked work: the build wiring is
complete, so the remaining scope is one publish action inside the pipeline.

The reminder is held by the pipeline rather than by an issue. `published-repos.sh` is sourced by
both `90-promote.sh` and `95-finish.sh`, so if the lite image is missing, **`promote` and `finish`
fail** rather than letting a GitHub Release go out `--latest` beside an image that is not there.

An `object not found` from Docker Hub for that repository means *the release has not run yet*. It
is not a credentials problem — `docker push` creates the repository on first push.
:::

---

## v0.6.0: Interface polish, and chat measured

**Theme:** stabilize and polish what v0.5.0 shipped. No new subsystems.

**Goal:** close the gap between "the capability exists" and "a person can find it and use it
without being told how", and replace assumptions about answer quality with measurements.

A release the size of v0.5.0 leaves a wake: features that work but are hard to reach, admin
capabilities that exist only as `curl` commands, and retrieval behaviour built behind a flag and
never measured. This release is that wake and nothing else.

**Exit criteria**

- Every screen in the UX review has been opened in a browser, in **both light and dark mode**, and
  matches its target. Type-checking is not evidence here.
- An admin can quarantine and release a file, and view and reset a locked account, entirely from
  the UI.
- Each default-off retrieval flag has a **recorded measurement** and an explicit on-or-off
  decision. "We did not get to it" is a fine outcome; an unmeasured flag quietly shipping on is
  not.

---

## v0.7.0: Run it in public

**Theme:** operating OpenTranscribe as a service someone else can touch.

**Goal:** with the interface polished, publish an inert public demo so people can see the app and
watch it upgrade.

The demo comes straight after the UI pass on purpose. A demo of an unpolished interface argues
against the project. This release also does the configuration and governance cleanup that makes
hosting one safe.

**Exit criteria**

- The demo cannot be used to exfiltrate data, run up inference cost, or mutate anything that
  persists between visitors.
- Every setting a non-developer needs is reachable from the admin UI, and `.env.example` no longer
  advertises settings nothing reads.
- A published release visibly upgrades the demo instance.

:::note Depends on v0.5.0
The demo runs the CPU-only lite image, so it cannot be stood up until that image is published.
:::

---

## v0.8.0: Native inference, and small GPUs

**Theme:** get the Python/PyTorch stack out of the inference path.

**Goal:** replace local inference end to end, and prove the result on the cards people actually
have.

These belong in one release because they are one problem. Diarization and transcription both run
through a resident PyTorch process today, and that single fact drives image size, cold start,
CUDA-version coupling and VRAM headroom. Replacing diarization alone leaves the larger half in
place, so this release covers both, plus an alternative local engine, and then demonstrates the
win rather than asserting it: tuning constants chosen on a 12 GB card get replaced with
measurements, and a 4 GB tier proves the headroom is real.

It also carries the verification backlog of things that can build successfully and test green
while still being wrong, because the development host could not exercise them.

**Exit criteria**

- A 4 GB card completes a full transcribe and diarize run, and the tier is documented on the
  hardware requirements page.
- No PyAnnote code path remains reachable in any deployment shape, including `gpu-scale`,
  `gpu-split` and `lite`, where it has historically survived as the de-facto engine.
- Any replacement transcription engine reports **WER and word-timing accuracy** against the
  current one, not just a speed number. Word timings feed boundary correction, diarization
  alignment and the transcript editor, so a faster engine with worse timings is a regression.
- Language coverage does not regress. The current stack claims 100+ languages; an engine that
  only does well in English is not an optimization.
- The speaker-index migration can be **rolled back** on a live install, demonstrated rather than
  asserted.
- Eviction and concurrency defaults cite a **measurement**, not a chosen number.
- Image size and cold-start time are measured before and after, and published.
- **Live transcription holds up under a real meeting, not a file replay**, and live speaker
  identification resolves against existing profiles rather than inventing new ones. This lands
  here because it is the payoff of the engine work, not a separate capability: it needs a
  streaming-capable engine and real-time embedding matching, which is exactly what replacing the
  inference stack provides.
- Every row of the architecture-blocked verification matrix is either verified on real hardware or
  restated with the specific hardware still missing. A row may stay open. It may not stay vague.

---

## v0.9.0: Federal government and FIPS

**Theme:** the compliance posture public-sector deployment requires.

**Goal:** be deployable by an organization whose procurement asks for evidence, not assurances.

**Exit criteria**

- The compliance posture is **evidenced**. An auditor can be handed artifacts, not assertions.
- The standing accepted-risk CVE position is **re-measured** rather than quoted, and the
  accept-or-remove decision is restated against current numbers.

:::warning FIPS module validation currently carries a NO-GO recommendation
Real FIPS *module* validation was investigated and found to be blocked by an architectural problem
rather than an effort problem: three OpenSSL instances end up in one process, and the
`cryptography` wheel embeds its own, so host FIPS mode can never reach it. Read the issue before
planning work against it.
:::

---

## v0.10.0: Bring your own model, and your own format

**Theme:** provider and output breadth.

**Goal:** let an operator use the LLM they already pay for, and get transcripts and summaries out
in the shape their workflow wants.

**Exit criteria**

- Each provider goes through the same connection test, model discovery, streaming and usage
  recording path as the existing ones. No provider-special-cased code in the chat service.
- The **local versus remote redaction keying holds for every new provider**: a remote provider
  receives masked text, and the classification fails closed. A new provider that defaults to
  "local" is a data egress bug, not a config nit.
- A note template is selectable per file and round-trips through export.

---

## v0.11.0: Document ingestion

**Theme:** transcripts are not the only thing worth searching and chatting with.

**Goal:** re-land the document ingestion vertical and make documents first-class in the gallery,
search and chat.

**This gets its own release number on purpose.** The vertical was removed from `master` so v0.5.0
could ship, and re-landing it restores roughly 70 commits and a chain of migrations. That is a
migration-bearing change to the data model, so it gets its own rehearsal, its own upgrade test and
its own blast radius, rather than a slot in a release that is also doing five other things.

**And it comes late on purpose.** Documents are the feature that ties everything else together:
they land in the gallery, search, chat and the watch-source plane at once. That makes them the
wrong thing to build on a core that has not been proven yet. The order is deliberate — get
transcription and diarization right, get search and chat measurably right, then widen the library
to a second content type. Adding documents while the core is still being reworked means debugging
two moving things at once, and every document defect becomes ambiguous: core, or plane?

:::warning Deferring is not free
The `feat/doc-ingestion` branch's divergence surface overlaps the UI work scheduled ahead of it —
the navbar, the search stores, and all 12 locale files. The branch must be merged from `master`
weekly, and immediately after v0.6.0 and v0.8.0 ship, or this becomes a rewrite rather than a
merge. If those merges are not going to happen, that is an argument for moving this **earlier**,
not for accepting the rewrite. See #552 for the measured divergence and the checklist.
:::

**Exit criteria**

- A document can be uploaded, watch-imported, deduplicated, searched, cited in chat and taken
  down, through the same surfaces a media file uses.
- The upgrade rehearsal passes across the full migration chain from a v0.10.0 install.
- No document-path code sits behind a flag that was never turned on.

---

## v0.12.0: The library knows things

**Theme:** intelligence across the corpus rather than within a single file.

**Goal:** answer questions about the library as a whole. Who speaks, about what, how much, and
what changed.

**Exit criteria**

- Persona profiles are derived from **deterministic metrics first**. Any LLM-derived claim is
  surfaced with a confidence score and is never auto-applied.
- Speaker matching has a measured latency curve at realistic corpus size, and a fixture that fails
  if a refactor changes a *decision*, not just a score.
- A transcript edit is reversible, attributable, and **reindexes search**. That last one is the
  defect class that has recurred most often in this codebase.

---

## v0.13.0: OpenTranscribe in your workflow

**Theme:** reach out of the app and into the tools around it.

**Goal:** meetings arrive on their own, and other software can ask OpenTranscribe questions.

**Exit criteria**

- A calendar meeting produces a transcript in the library with no manual upload step.
- The agent-facing surface is a **logic-free adapter** over existing endpoints, capped at the
  `user` tier, rate limited, and it honours quarantine and legal hold. Those are three planes
  current backend code does not check uniformly.
- A third-party hook can run after transcription without forking the pipeline.

---

## v1.0.0: Commit to it

**Theme:** interfaces, platform support and performance become promises.

**Goal:** run natively on the hardware people own, transcribe live, and stand behind a stable API.

:::note Why Apple Silicon waits for the desktop app
Native Mac transcription is **blocked on the standalone application**, not on the engine work in
v0.8.0. Docker on Apple Silicon cannot reach the GPU: there is no Metal passthrough to a Linux
container, so a containerised deployment is CPU-only on a Mac no matter which engine it runs.
Reaching the Neural Engine and the GPU at all requires a process running natively on macOS, which
is what the desktop app provides. That is why the two ship together here rather than with the
other local-engine work.
:::

**Exit criteria**

- The public API surface is documented and versioned, and breaking it requires a major bump.
- OpenTranscribe runs natively on Apple Silicon, using the GPU and Neural Engine, without Docker
  in the inference path.

---

## How to change this page

Do not add issue numbers, counts or dates here. Those live in the tracker and are rendered by
[/roadmap](/roadmap), which is generated.

1. Change the **issue's milestone** on GitHub, and its `Target` on the
   [Roadmap project board](https://github.com/orgs/attevon-llc/projects/1).
2. Run `python3 scripts/generate-roadmap.py` so `/roadmap` reflects it. CI fails if you forget.
3. Update the theme, goal or exit criteria **here** only if the *meaning* of the release changed.
4. If a milestone has picked up a second theme, **split it into a new number** rather than
   widening its goal statement. New numbers are free.

A release whose exit criteria you cannot write is a release that is not scoped yet.
