---
title: Roadmap
sidebar_label: Roadmap
description: What each OpenTranscribe release is for, what ships in it, and how we know it is done.
---

# Roadmap

This page answers three questions the issue tracker on its own cannot: what is each
version number *for*, what ships in it, and how do we know when it is done.

Every release below has one **theme**, a **goal** in a sentence, and **exit criteria** you
can check by looking at the product and saying yes or no. A release that cannot state its
exit criteria is not a release. It is a pile of issues with a version number on it.

:::info Source of truth
The issues and milestones on GitHub are authoritative. This page is the narrative layer
over them, and it explains *why* the grouping is what it is. If the two disagree, the
tracker is right and this page is stale. Say so rather than working around it.

Reconciled against GitHub on **2026-09-06**.
:::

---

## How the version numbers work

OpenTranscribe is pre-1.0 and follows [Semantic Versioning](https://semver.org/). Until
1.0, the minor number is the unit of release and each one carries a single theme.

| | Meaning |
|---|---|
| `0.x.0` | A themed release. One goal, stated below. Roughly 2 to 20 issues. |
| `0.x.y` | A patch on a shipped `0.x.0`. Defects and security fixes only, no new capability. |
| `1.0.0` | The commitment point: a stable API surface, a formal compliance posture, and enough platform breadth to run outside a Linux and CUDA box. |

Two rules keep the numbers honest.

**A release gets one theme, not a date's worth of work.** When a milestone picks up a
second unrelated theme it splits into a new number instead of growing. That is why the
ladder runs past `0.9.0` into `0.10.0` and beyond. Version numbers are cheap and coherence
is not. To be clear, `0.10.0` sorts *after* `0.9.0`. That is correct SemVer, not a typo.

**Dates come from dependencies, not from ambition.** A release is never dated before one it
depends on. Where a date looks aggressive, argue with the scope rather than the date.

---

## Release ladder at a glance

| Version | Theme | Open | Target |
|---|---|---|---|
| [**v0.5.0**](#v050-ship-what-is-already-built) | Ship what is already built | 1 | 2026-09-13 |
| [**v0.6.0**](#v060-the-app-feels-finished) | The app feels finished | 23 | 2026-10-31 |
| [**v0.7.0**](#v070-run-well-on-the-gpu-you-actually-have) | Run well on the GPU you actually have | 4 | 2026-11-21 |
| [**v0.8.0**](#v080-document-ingestion) | Document ingestion | 4 | 2026-12-19 |
| [**v0.9.0**](#v090-finish-the-native-diarizer-migration) | Finish the native diarizer migration | 2 | 2027-01-29 |
| [**v0.10.0**](#v0100-bring-your-own-model-and-your-own-format) | Bring your own model, and your own format | 6 | 2027-02-25 |
| [**v0.11.0**](#v0110-the-library-knows-things) | The library knows things | 4 | 2027-03-25 |
| [**v0.12.0**](#v0120-opentranscribe-in-your-workflow) | OpenTranscribe in your workflow | 5 | 2027-04-29 |
| [**v0.13.0**](#v0130-run-it-in-public) | Run it in public | 4 | 2027-05-27 |
| [**v1.0.0**](#v100-commit-to-it) | Commit to it | 6 | 2027-07-30 |

---

## v0.5.0: Ship what is already built

**Theme:** get the largest body of finished work in the project's history out the door.

**Goal:** publish it without adding one more feature to it.

This release is feature complete and code frozen. **284 issues are closed against it.**
Everything in it is written, merged and tested, which is why the milestone looks nearly
empty while the release itself is by a wide margin the biggest one so far. What remains is
release execution, not development.

It is easier to describe v0.5.0 by what it did *not* leave alone. Both the backend and the
frontend were substantially rewritten, most subsystems were rebuilt rather than extended,
and a large share of the work went into speed and refinement of things that already
worked.

### Transcription and diarization

A **native diarization engine** replaces PyAnnote as the default on-box backend.
Transcription and diarization now run **concurrently instead of back to back**, which is
43% faster on a 66.5 minute test clip. **Diarization boundary correction** (#193) adds a
default-on word boundary smoother, plus an experimental acoustic backchannel re-check, and
both measurably cut speaker mislabelling at turn seams.

The **combined transcription engine** was refactored and gained an optional multi-GPU split
and a **hybrid mode**, running transcription on CPU and diarization on GPU or MPS, which is
what makes small cards and Apple Silicon usable. CrisperWhisper model support landed, and
the Engine Configuration admin UI was cleaned up.

The **cloud ASR provider suite** is production verified end to end: AWS Transcribe,
Speechmatics, AssemblyAI, Gladia and pyannote.ai.

### Chat and search

Chat was **rebuilt around corpus-scale retrieval**. Digests, map-reduce and a query planner
replace the old single-pass approach, which could not answer a question spanning more than
a handful of files. Alongside it: a live **query execution trace panel**, an honest
**retrieval quality notice** rather than false confidence, automatic **LLM context window
discovery**, and a per-model **reasoning display** for models that think before answering.

Search got a **unified in-app foundation** (PR #282), **tag search that actually works**
(it was broken), **multilingual search and chat** as a one-click switch that was measured
rather than assumed, **tag management and sharing**, a **community Q&A panel extractor**
for summarization, and **Amazon Bedrock** as a first-class LLM provider.

### Privacy, identity and security

**Content redaction** adds PII, profanity and toxicity detection with read-time masking
across every display and export surface, served by a dedicated `celery-redaction` worker.

A full **authentication and identity overhaul** covers local, LDAP, OIDC, SAML, PKI, proxy,
MFA and SCIM. **GDPR hardening** adds an erasure ledger, legal-hold re-erasure and
restore-path coverage. **FedRAMP AC-2** account inactivity expiration is enforced.

A **security hardening wave** of roughly twenty fixes closed SSRF gaps, a quarantined-file
data leak, session-cap and session-switching defects, tenant isolation gaps in comments and
groups, an irreversible admin-deletion gap, and FIPS and PKI validation holes. Most of
these were found by adversarial review rather than by users, which is the point.

### Platform and operations

A rebuilt **media download architecture** with presigned URL streaming, async bulk export
and bounded derived-asset caching. Production installs get a **backup and restore command**
with release-rehearsal coverage of both the upgrade and the rollback path. **Watch sources**
gained per-file management and email notifications. Deployments gained **fresh isolated
environments** and **in-app scheduled backups**. The docs site, i18n (12 locales, four of
them new here) and the release pipeline (`scripts/release.sh`, now 12 gated stages) all saw
heavy investment.

:::warning This release contains breaking changes
Read the Upgrade Notes in the [CHANGELOG](https://github.com/attevon-llc/OpenTranscribe/blob/master/CHANGELOG.md) before pulling.
:::

### What is left

| Issue | Why it is still open |
|---|---|
| [#667](https://github.com/attevon-llc/OpenTranscribe/issues/667) | The CPU-only `lite` image has to be **published** as a multi-arch (amd64 and arm64) artifact. The build wiring is done. The registry push has never run. |

### Exit criteria

- `v0.5.0` is tagged and a GitHub Release exists marked `latest`.
- `davidamacey/opentranscribe-backend-lite:v0.5.0` exists on Docker Hub **with both
  architecture legs**, and `setup-opentranscribe.sh --lite` completes an install using it.
- Every published container has been CVE scanned **on every architecture leg**. A green scan
  over an image that was never scanned is the exact failure this gate exists to catch.
- The upgrade and fresh-install rehearsals both pass against the published images.

:::warning The one real risk left in this release
`setup-opentranscribe.sh` ships a user-facing `--lite` flag that pulls
`opentranscribe-backend-lite`, and that repository **does not exist on Docker Hub yet**. If
v0.5.0 publishes without the lite leg, every `--lite` install breaks on day one. Per
[#680](https://github.com/attevon-llc/OpenTranscribe/issues/680), lite is also the only
backend an arm64 user can run. That is why #667 is P0 and blocking.
:::

---

## v0.6.0: The app feels finished

**Theme:** stabilize and polish what v0.5.0 shipped. No new subsystems.

**Goal:** close the gap between "the capability exists" and "a person can find it and use it
without being told how".

A release the size of v0.5.0 leaves a wake. Features that work but are hard to reach. Admin
capabilities that exist only as `curl` commands. Retrieval behaviour that was built behind
a flag and never measured. This release is that wake and nothing else.

**What it ships**

- **The September 2026 UX and UI review pass** ([#747](https://github.com/attevon-llc/OpenTranscribe/issues/747) to [#758](https://github.com/attevon-llc/OpenTranscribe/issues/758), plus [#760](https://github.com/attevon-llc/OpenTranscribe/issues/760)): gallery toolbar and filter
  panel, the file detail page reorganized around speaker editing, the upload wizard, the
  notification panel, the navbar, speaker management, and multi-select parity across tags
  and collections.
- **Admin surfaces that today exist only in the backend:** quarantine and takedown
  ([#576](https://github.com/attevon-llc/OpenTranscribe/issues/576), currently three
  backend-only endpoints an admin has to `curl`), locked account management
  ([#570](https://github.com/attevon-llc/OpenTranscribe/issues/570)), and discoverable local
  LLM base URLs instead of hand-typed Docker hostnames
  ([#644](https://github.com/attevon-llc/OpenTranscribe/issues/644)).
- **RAG answer quality, measured** ([#462](https://github.com/attevon-llc/OpenTranscribe/issues/462), [#464](https://github.com/attevon-llc/OpenTranscribe/issues/464), [#506](https://github.com/attevon-llc/OpenTranscribe/issues/506), [#523](https://github.com/attevon-llc/OpenTranscribe/issues/523), [#526](https://github.com/attevon-llc/OpenTranscribe/issues/526), [#532](https://github.com/attevon-llc/OpenTranscribe/issues/532)): the deferred experiments from the
  [#461](https://github.com/attevon-llc/OpenTranscribe/issues/461) chain. Four of these are
  **already built behind default-off flags**, so the work is running the measurement that
  decides whether the flag flips on, not writing new code.

**Exit criteria**

- Every screen in the UX review has been opened in a browser, in **both light and dark
  mode**, and matches its issue's target. Type-checking is not evidence here.
- An admin can quarantine and release a file, and view and reset a locked account, entirely
  from the UI.
- Each default-off retrieval flag has a **recorded measurement** and an explicit on or off
  decision. "We did not get to it" is a fine outcome. An unmeasured flag quietly shipping
  on is not.

:::note Open question: this release currently carries two themes
23 issues across UI polish and retrieval measurement is the largest milestone on the board,
and by the one-theme rule it is a candidate to split, with UI polish staying at v0.6.0 and
retrieval measurement becoming its own number. It is left combined for now because the
retrieval work is measurement rather than construction. Revisit if v0.6.0 starts to slip.
:::

---

## v0.7.0: Run well on the GPU you actually have

**Theme:** correctness and performance on hardware that is not this project's development box.

**Goal:** stop treating "one big CUDA card" as the assumed deployment, and close the
verification gaps only other hardware can settle.

OpenTranscribe's tuning constants were picked on a 12 GB card and generalized by assumption.
This release replaces the assumptions with measurements. It also works through the backlog
of things that can build successfully and test green while still being wrong, because the
development host could not exercise them.

| Issue | What |
|---|---|
| [#369](https://github.com/attevon-llc/OpenTranscribe/issues/369) | Calibrate transcriber eviction and concurrency from measurement instead of magic numbers, for limited-VRAM cards |
| [#511](https://github.com/attevon-llc/OpenTranscribe/issues/511) | Build and measure a **4 GB laptop GPU tier** for native diarization |
| [#274](https://github.com/attevon-llc/OpenTranscribe/issues/274) | Blackwell base image upgrade (`nvidia/pytorch` 26.x), including a torchaudio strategy |
| [#713](https://github.com/attevon-llc/OpenTranscribe/issues/713) | The architecture-blocked verification matrix: what genuinely needs real arm64, Blackwell, or multi-GPU hardware |

**Exit criteria**

- A 4 GB card completes a full transcribe and diarize run, and the tier is documented on the
  hardware requirements page.
- Eviction and concurrency defaults cite a **measurement**, not a chosen number.
- Every row of #713's matrix is either verified on real hardware or restated with the
  specific hardware still missing. A row may stay open. It may not stay vague.

:::tip Why this moved up
This work used to be split across v0.7.0 and v0.9.0, which put a prerequisite
([#511](https://github.com/attevon-llc/OpenTranscribe/issues/511), the small-GPU tier)
*after* the release that depends on it. It is now one release, ahead of both. Two of #713's
premises have since turned out to be false as well: an arm64 host and a second GPU are both
reachable, so several rows are verifiable now rather than blocked.
:::

---

## v0.8.0: Document ingestion

**Theme:** transcripts are not the only thing worth searching and chatting with.

**Goal:** re-land the document ingestion vertical and make documents first-class citizens of
the gallery, search and chat, as a release of its own.

**This gets its own release number on purpose.** The vertical was removed from `master` so
v0.5.0 could ship ([PR #551](https://github.com/attevon-llc/OpenTranscribe/pull/551)), and
re-landing it restores roughly 70 commits and migrations `v394` through `v400`. That is a
migration-bearing change to the data model. It deserves its own rehearsal, its own upgrade
test and its own blast radius, not a slot inside a release that is also doing five other
things.

| Issue | What |
|---|---|
| [#552](https://github.com/attevon-llc/OpenTranscribe/issues/552) | Re-land the vertical from `feat/doc-ingestion`. Merge from master only, never rebase, never squash |
| [#516](https://github.com/attevon-llc/OpenTranscribe/issues/516) | Unified gallery: type filter, uniform status, recovery and takedown parity |
| [#546](https://github.com/attevon-llc/OpenTranscribe/issues/546) | `Document.file_hash` is written by both ingest paths and read by neither, so documents are never deduplicated |
| [#547](https://github.com/attevon-llc/OpenTranscribe/issues/547) | Watch-imported documents are unrepresentable in the watch-source API |

**Exit criteria**

- A document can be uploaded, watch-imported, deduplicated, searched, cited in chat and
  taken down, through the same surfaces a media file uses.
- The upgrade rehearsal passes across the `v394` to `v400` migration chain from a v0.7.0 install.
- No document-path code sits behind a flag that was never turned on.

---

## v0.9.0: Finish the native diarizer migration

**Theme:** remove PyTorch and PyAnnote from the diarization path for good.

**Goal:** make the native diarizer the only diarizer, and delete the fallback rather than
leaving two implementations doing the same job.

v0.5.0 made native diarization the default. This release removes the alternative, which is
the harder half, because the fallback is what has been quietly catching every case the
native path does not handle yet.

| Issue | What |
|---|---|
| [#572](https://github.com/attevon-llc/OpenTranscribe/issues/572) | Remove PyTorch and PyAnnote diarization once `diar-native` has parity |
| [#659](https://github.com/attevon-llc/OpenTranscribe/issues/659) | A real rollback path for the v3 to v4 speaker-index alias swap |

**Exit criteria**

- No PyAnnote code path remains reachable in any deployment shape, including `gpu-scale`,
  `gpu-split` and `lite`, where it has historically survived as the de-facto engine.
- The v3 to v4 speaker-index swap can be **rolled back** on a live install, demonstrated
  rather than asserted.
- Image size and cold-start time are measured before and after, and published.

:::warning Depends on v0.7.0
Parity includes small cards. Removing the fallback before the 4 GB tier
([#511](https://github.com/attevon-llc/OpenTranscribe/issues/511)) is measured would leave
low-VRAM users with no working diarizer at all.
:::

---

## v0.10.0: Bring your own model, and your own format

**Theme:** provider and output breadth.

**Goal:** let an operator use the LLM they already pay for, and get transcripts and summaries
out in the shape their workflow wants.

| Issue | What |
|---|---|
| [#379](https://github.com/attevon-llc/OpenTranscribe/issues/379) | Google Gemini as a first-class LLM provider |
| [#380](https://github.com/attevon-llc/OpenTranscribe/issues/380) | Groq, for low-latency inference |
| [#382](https://github.com/attevon-llc/OpenTranscribe/issues/382) | GCP Vertex AI, SDK-based and enterprise-shaped, in the style of the Bedrock provider |
| [#378](https://github.com/attevon-llc/OpenTranscribe/issues/378) | Tinfoil, a confidential-computing provider |
| [#562](https://github.com/attevon-llc/OpenTranscribe/issues/562) | Custom note templates per meeting type (standup, 1:1, interview) |
| [#564](https://github.com/attevon-llc/OpenTranscribe/issues/564) | Markdown file export format |

**Exit criteria**

- Each provider goes through the same connection test, model discovery, streaming and usage
  recording path as the existing ones. No provider-special-cased code in the chat service.
- The **local versus remote redaction keying holds for every new provider**: a remote
  provider receives masked text, and the classification fails closed. A new provider that
  defaults to "local" is a data egress bug, not a config nit.
- A note template is selectable per file and round-trips through export.

---

## v0.11.0: The library knows things

**Theme:** intelligence across the corpus rather than within a single file.

**Goal:** answer questions about the library as a whole. Who speaks, about what, how much,
and what changed.

| Issue | What |
|---|---|
| [#550](https://github.com/attevon-llc/OpenTranscribe/issues/550) | Speaker Persona Profiles: corpus-level speech, topic and role profiles grounded in deterministic conversation metrics |
| [#624](https://github.com/attevon-llc/OpenTranscribe/issues/624) | Profile speaker-matching kNN performance at scale, plus a decision-parity fixture |
| [#20](https://github.com/attevon-llc/OpenTranscribe/issues/20) | Analytics dashboard in the gallery view |
| [#46](https://github.com/attevon-llc/OpenTranscribe/issues/46) | Transcript version control and change tracking |

**Exit criteria**

- Persona profiles are derived from **deterministic metrics first**. Any LLM-derived claim is
  surfaced with a confidence score and is never auto-applied.
- Speaker matching has a measured latency curve at realistic corpus size, and a fixture that
  fails if a refactor changes a *decision*, not just a score.
- A transcript edit is reversible, attributable, and **reindexes search**. That last one is
  the defect class that has recurred most often in this codebase.

---

## v0.12.0: OpenTranscribe in your workflow

**Theme:** reach out of the app and into the tools around it.

**Goal:** meetings arrive on their own, and other software can ask OpenTranscribe questions.

| Issue | What |
|---|---|
| [#365](https://github.com/attevon-llc/OpenTranscribe/issues/365) | Recall.ai meeting capture: meetings, transcripts, speakers and metadata into the same library |
| [#563](https://github.com/attevon-llc/OpenTranscribe/issues/563) | Calendar integration, CalDAV first |
| [#565](https://github.com/attevon-llc/OpenTranscribe/issues/565) | Pre-meeting briefs from the calendar plus past transcripts |
| [#560](https://github.com/attevon-llc/OpenTranscribe/issues/560) | MCP server for transcript and RAG chat access by external AI agents |
| [#561](https://github.com/attevon-llc/OpenTranscribe/issues/561) | Plugin and post-processing hook architecture for the transcription pipeline |

**Exit criteria**

- A calendar meeting produces a transcript in the library with no manual upload step.
- The MCP surface is a **logic-free adapter** over existing endpoints, capped at the `user`
  tier, rate limited, and it honours quarantine and legal hold. Those are three planes
  current backend code does not check uniformly.
- A third-party hook can run after transcription without forking the pipeline.

---

## v0.13.0: Run it in public

**Theme:** operating OpenTranscribe as a service someone else can touch.

**Goal:** stand up a public, inert demo instance, and clean up the configuration and
governance surface that makes running one safe.

| Issue | What |
|---|---|
| [#628](https://github.com/attevon-llc/OpenTranscribe/issues/628) | Public read-only demo deployment, in the style of Immich's inert demo |
| [#566](https://github.com/attevon-llc/OpenTranscribe/issues/566) | Move UI-worthy env vars into the admin UI, then drop them from `.env.example` |
| [#468](https://github.com/attevon-llc/OpenTranscribe/issues/468) | Contributor License Agreement enforcement |
| [#775](https://github.com/attevon-llc/OpenTranscribe/issues/775) | The standing accepted-risk record for the unfixed exiftool and perl CRITICAL CVEs, and its re-check point |

**Exit criteria**

- The demo instance cannot be used to exfiltrate data, run up inference cost, or mutate
  anything that persists between visitors.
- Every setting a non-developer needs is reachable from the admin UI, and `.env.example` no
  longer advertises settings nothing reads.
- The CVE position is **re-measured** rather than quoted, and the accept-or-remove decision
  is restated against current numbers.

---

## v1.0.0: Commit to it

**Theme:** the point where interfaces, compliance posture and platform support become promises.

**Goal:** run natively on the hardware people own, transcribe live, and stand behind a formal
compliance and stability posture.

| Issue | What |
|---|---|
| [#98](https://github.com/attevon-llc/OpenTranscribe/issues/98) | HIPAA, SOC 2 and GDPR certification requirements |
| [#574](https://github.com/attevon-llc/OpenTranscribe/issues/574) | Real FIPS 140-3 and 140-2 **module** validation, not merely approved algorithms, for FedRAMP High and DoD IL4+ |
| [#48](https://github.com/attevon-llc/OpenTranscribe/issues/48) | Native Apple Silicon transcription, MLX-Whisper or whisper.cpp |
| [#366](https://github.com/attevon-llc/OpenTranscribe/issues/366) | NVIDIA NeMo (Parakeet and Canary) as a first-class local engine |
| [#69](https://github.com/attevon-llc/OpenTranscribe/issues/69) | Live transcription with real-time speaker identification |
| [#283](https://github.com/attevon-llc/OpenTranscribe/issues/283) | Cross-platform standalone desktop app (Tauri, SQLite with sqlite-vec, local inference) |

**Exit criteria**

- The public API surface is documented and versioned, and breaking it requires a major bump.
- The compliance posture is **evidenced**. An auditor can be handed artifacts, not assertions.
- OpenTranscribe runs natively on Apple Silicon without Docker GPU passthrough.

:::warning #574 currently carries a NO-GO recommendation
Real FIPS module validation was investigated and found to be blocked by an architectural
problem rather than an effort problem. Three OpenSSL instances end up in one process, and
the `cryptography` wheel embeds its own, so host FIPS mode can never reach it. The issue
stays on v1.0.0 as the tracking record, but the recommendation written on it is to **not**
pursue module validation as currently scoped. Read the issue before planning work against it.
:::

---

## How to change this page

Do not hand-edit the tables to reflect work you are about to do. The sequence is:

1. Change the **issue's milestone** on GitHub, and its `Target` on the
   [Roadmap project board](https://github.com/orgs/attevon-llc/projects/1).
2. Update this page's theme, goal or exit criteria if the *meaning* of the release changed.
3. If a milestone has picked up a second theme, **split it into a new number** rather than
   widening its goal statement. New numbers are free.

A release whose exit criteria you cannot write is a release that is not scoped yet.
