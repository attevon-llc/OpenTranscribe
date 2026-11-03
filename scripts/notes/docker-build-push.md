# scripts — `docker-build-push.sh`

Moved intact from `scripts/CLAUDE.md`. Skill: `.claude/skills/docker-build-push/SKILL.md`.

## docker-build-push.sh

- **The tag grammar, and its ONE home** (issue #680). Capability lives in the **repository** — a
  manifest index cannot span repos — and is **restated in the tag**:

  | repo | capability | legs built | index | `:latest` |
  |---|---|---|---|---|
  | `opentranscribe-backend` | `cuda` | `vX.Y.Z-cuda-amd64` | `vX.Y.Z` | digest-copy of the index |
  | `opentranscribe-backend-lite` | `cpu` | `vX.Y.Z-cpu-amd64`, `vX.Y.Z-cpu-arm64` | `vX.Y.Z` | digest-copy |
  | `opentranscribe-frontend` / `-docs` | `multiarch` | (no cap legs) | `vX.Y.Z` | digest-copy |
  | `opentranscribe-backend` (`:blackwell`) | `blackwell` | `vX.Y.Z-blackwell-arm64` | — | `:blackwell` |

  `vX.Y.Z-cuda-arm64` is **reserved and not built**: no aarch64 CUDA torch wheel at the pinned
  version, `onnxruntime-gpu` has zero aarch64 wheels, and diar-native publishes no CUDA arm64
  sidecar. Do not transcribe this table anywhere else — ask the script:
  **`./scripts/docker-build-push.sh list-platforms`** prints `component<TAB>capability<TAB>platforms`
  and is the single source of truth (`COMPONENT_PLATFORMS` / `COMPONENT_CAPABILITY`).
  `scripts/release/80-publish.sh` and `40-build.sh` derive from it rather than hardcoding arches.
- **`PLATFORMS` is now an explicit OVERRIDE, not a default.** It used to default to
  `linux/amd64,linux/arm64` for **every** component, which is the mechanism issue #680 names for
  how a degraded arm64 backend got published beside a good amd64 one under one tag with nothing
  to notice. Unset, each component builds only its own declared platforms; set, it forces every
  component being built onto that list (`PLATFORMS=linux/amd64 $0 backend` for a quick
  single-arch build).
- **ALWAYS `USE_REMOTE_BUILDER=true`** when a build includes `linux/arm64` (today: `lite`).
  It defaults to `false`, and without it arm64 builds under QEMU — 2–3 h instead of 15–30 min.
  It hard-exits if the `opentranscribe-multiarch` builder is missing (`setup-remote-builder.sh setup`).
- `SKIP_SECURITY_SCAN=true` for quick iteration; `$0 lite` / `$0 backend` for one component;
  `$0 auto` builds only git-changed components (`backend` changes build **both** backend and lite,
  since both come from `backend/`). `blackwell` is built **only on request** — never by
  `all`/`auto`, and never published.
- ⚠️ **"Scanned, findings tolerable" and "never scanned" are DIFFERENT outcomes and must stay
  that way** (issue #681). `security-scan.sh` exits **1** for findings and **2** for could-not-scan
  (unknown component, image unobtainable, a sub-scan that died without recording a verdict);
  `run_security_scan` treats 2 as fatal **regardless of `FAIL_ON_SECURITY_ISSUES`**, because that
  flag is a statement about which findings are acceptable to ship, not about shipping an image
  nobody looked at. Collapsing the two into one `else` branch is what made an unscannable
  component print `All security scans completed successfully!` — and `docs` was already in that
  state, since it was in `BUILT_COMPONENTS` with a `security-scan.sh` arm but no registry-pull
  branch, so the default path "scanned" an image it never fetched.
- ⚠️ **The scan unit is a LEG, not a component** (issue #667). A multi-arch tag is an index over
  several artifacts, and a CVE report on one leg says nothing about the others. The registry
  path used to `docker pull --platform linux/amd64` and scan that, so `lite`'s arm64 leg — the
  **only** backend an arm64 host can install, since `opentranscribe.sh` defaults arm64 to
  `DEPLOYMENT_MODE=lite` — would have shipped never having been looked at, with an amd64 report
  beside it looking like coverage. `security-scan.sh <component>` now scans **every platform the
  component declares** (7 legs across the 4 published components today); `SCAN_PLATFORM=linux/arm64`
  narrows to one for iteration. The platform set is read from `docker-build-push.sh list-platforms`,
  never transcribed.
  - **Reports are keyed `<component>-<arch>-<tool>`, unconditionally** — including for
    single-platform components. A scheme where the arch appears only sometimes is one where a
    missing leg looks like an ordinary report.
  - **`resolve_platform_image` VERIFIES what it got, and that is the point.** `docker pull
    --platform linux/arm64` against a tag with no arm64 leg does **not** reliably fail: measured
    against `opentranscribe-backend:latest` (amd64+arm64 only), requesting `linux/s390x`
    proceeded to download a multi-GB image of a *different* architecture. Scanning that and
    filing it as `-s390x` is worse than not scanning — it manufactures evidence. A registry
    pre-flight (`docker manifest inspect`) refuses an undeclared platform before downloading;
    the post-pull `{{.Os}}/{{.Architecture}}` comparison is the authoritative check. An empty
    platform list is **COULD NOT SCAN (2)**, never "no architectures to scan".
  - Local resolution searches leg tags (`repo:vX.Y.Z-<cap>-<arch>`), not just the bare tag:
    building several legs locally leaves `repo:vX.Y.Z` pointing at whichever built **last**, so
    checking only that tag makes which leg gets scanned depend on iteration order.
  - `40-build.sh` builds every declared leg (one invocation per leg — `--load` cannot export a
    multi-arch manifest) so `50-scan.sh` has something to scan; the baked-version `docker run`
    check applies only to the **host-arch** leg, by its leg tag.
- ⚠️ **Never pipe a producer into a reader that stops before EOF — `grep -q`, `grep -m1`,
  `head -N`, `sed '…q'` — anywhere under `set -o pipefail`.** The reader exits at its first
  match, the producer dies of SIGPIPE (141), and `pipefail` makes 141 the *pipeline's* status.
  Two distinct failures, and both have shipped here:
  - **in a condition** (`if`/`while`/`!`/`&&`), a match reads as a **non-match** — the
    assertion silently inverts. `set -e` does not protect you; a failing condition is not an
    error.
  - **in an assignment** under `set -e` (`x="$(producer | head -1)"`), the captured value is
    correct and the script then **aborts on the assignment**, truncating every phase after it
    with no error trace. That is the #617/#618 family, and it is how
    `mc_assert_no_hardlinks` produced a bare unexplained `exit 141` in precisely the case it
    exists to report (`lib/model-cache.sh`, fixed 2026-09-07).

  ⚠️ **THIS IS NOT SIZE-DEPENDENT. The "output under the 64 KB pipe buffer is unaffected" rule
  this file used to state was MEASURED AND REFUTED on 2026-09-07**, and eight sites had been
  exempted on it. The governing variable is **whether the producer still has a write to make
  after the reader leaves** — a question about ELAPSED TIME between the matching write and the
  producer's final one, not about bytes. Measured on this host:

  | producer | result |
  |---|---|
  | 40 bytes, 2 ms between its two writes | **300 / 300 inverted** |
  | the same 40 bytes written back-to-back | 0 / 3000 |
  | real `docker info` — **1,609 B, ~40x UNDER the buffer** | **12 / 3000 (~1 in 250)** |
  | `find` over a nested 130-file tree into `head -5` | **40 / 40 aborted** (0/40 at 30 files) |

  Same size, opposite outcomes. An external binary that queries a daemon — `docker`, `ss`,
  `buildx` — is **never** safe merely for being terse: the gaps between its writes are RPC
  round-trips. `find` walking directories is the same story with directory reads.

  **The sound admission criterion is: the match can only land on the producer's FINAL write**
  (nothing remains to be written, so nothing can SIGPIPE). A shell builtin emitting a small
  in-memory string also qualifies — but that has a low ceiling, well under the buffer:
  measured, `printf 'FIRST\n<payload>\n' | head -1` under `set -euo pipefail` aborts 0/100 at
  7 KiB, **4/100 at 16 KiB**, 31/100 at 32 KiB and 100/100 at 60 KiB.

  ⚠️ **"We ran it N times and saw nothing" is not evidence below a few thousand iterations.**
  At the measured ~1-in-250 rate for `docker info`, a 400-iteration null result is the expected
  outcome about 45% of the time — an earlier 400-run pass on this exact command is what
  licensed the size rule in the first place. Size the run against the rate, or use a producer
  with a deliberate gap (the 40-byte/2 ms fixture above) which inverts every time.

  Fix: `[ "$(<producer> | grep -c ...)" -gt 0 ]` (`grep -c` reads to EOF), or capture the
  producer into a variable / a `while read` loop and match on that. Two scanners enforce this
  and they have **different reaches** — do not add a third:
  `backend/tests/unit/test_pipefail_grep_q_inversion.py` scans all of `scripts/` for the
  `| grep -q` half, and `test_opentr_docker_probe_sigpipe.py` covers the assignment/abort half
  and the wider reader set over an owned-script list.
- **The scannable component list has exactly one home**: the `SCAN_COMPONENT_*` tables in
  `security-scan.sh`, exposed as `./scripts/security-scan.sh list-components` (and
  `list-repos`, `component<TAB>repo`, which `scripts/release/50-scan.sh` derives its
  component→repo map from). `docker-build-push.sh` validates `BUILT_COMPONENTS` against that
  before pulling anything, and both its pull dispatches have a failing `*)`. Adding a component
  means adding it there; forgetting is loud instead of a green run over an unscanned published
  image. `lite` and `blackwell` are in it as of #680. **The platform table's key set must EQUAL
  that list** — `assert_platform_table_matches_scan_components` checks it on every run, because
  a component in one table and not the other is either unscanned or built with an empty platform
  list. Guarded by `scripts/tests/test-scan-not-a-pass.sh` (prints its own pass count) and
  `scripts/tests/test-publish-platforms.sh` (25 sections, each with an observed red state —
  it prints its own assertion count, don't transcribe one here),
  both offline, both wired as pre-commit hooks.
  ⚠️ `test-scan-not-a-pass.sh` uses **`bogus`** as its "unknown component" placeholder. It used
  to use `lite` — which stopped being unknown the moment #680 made it real, at which point those
  cases started a REAL Trivy scan and hung. If you ever make `bogus` a component, rename it there.
- ⚠️ **The SBOM's licence comes from the image's `LABEL`, and NOTHING else can supply it**
  (issue #886). Three measurements against syft 1.33.0, each a rejected alternative:
  the final `nginx:*-alpine` stages of `frontend/Dockerfile.prod` and `docs-site/Dockerfile`
  contain **zero** `package.json` files (`find / -name package.json` → 0 in both published
  v0.5.0 images), so those manifests' `license` fields never reach a scan; syft catalogues
  **nothing at all** from a `pyproject.toml` — a control with `[project] name/version/license`
  produced an empty artifact list as a `dir:` scan *and* inside an image — so the backend/lite/
  blackwell images cannot be fixed that way even in principle (and `backend/pyproject.toml` is
  not copied into the image anyway); and `syft config` exposes `source.name`/`version`/
  `supplier` and **no licence field**, so syft cannot write CycloneDX's own
  `metadata.component.licenses`. What works is `org.opencontainers.image.licenses` on the
  **final stage** of each production Dockerfile — a label in a builder stage never reaches the
  image — which syft records as a `syft:image:labels:*` property, and which
  `scripts/lib/sbom_license.py` then promotes into `metadata.component.licenses` from inside
  `generate_sbom()`. The label is the single source of truth; the promoter never invents an
  identifier, and an image with no label WARNS rather than failing the scan (turning the SBOM
  step into a licence gate would add a release-blocking surface;
  `backend/tests/unit/test_first_party_license_declarations.py` already fails the unit suite on
  any production Dockerfile that drops the label, and derives the expected SPDX id from the
  repo's own `LICENSE` rather than restating it). **Use the SPDX `-only`/`-or-later` form**:
  four Dockerfiles carried the deprecated bare `AGPL-3.0` against manifests declaring
  `AGPL-3.0-only`, and `docs-site/Dockerfile` carried no label at all.
- ⚠️ **`generate_sbom()`'s stdout IS its return value — keep every message on stderr.** The
  caller runs it as `generate_sbom ... > "${status_dir}/sbom_path.txt"` and feeds that file to
  `scan_grype` as a path. With the banners on stdout that file held six lines of ANSI headers
  with the path last, `[ -f "$sbom_file" ]` was false for the blob, and grype silently took its
  `else` branch and re-catalogued the whole image — so "Grype scan (uses SBOM for speed)"
  described a branch that never ran. A/B-measured before fixing it: the `(vuln id, package,
  version)` match sets from the SBOM and from the image are **identical** on both
  `opentranscribe-docs:v0.5.0` (37 matches) and `opentranscribe-backend-lite:v0.5.0` (744), so
  restoring the fast path moves no CVE count the release gate reads.
- **`BUILD_MODE=local` is the only path that does not push.** Every other path ends in
  `buildx --push`: `:vX.Y.Z` (and `:latest`, unless `PUSH_LATEST=false`) hit Docker Hub the
  instant the build finishes. Capability-bearing components push one **leg** per architecture and
  then assemble the index with `buildx imagetools create`, so the index provably contains exactly
  those legs. In `local` mode there is nothing in a registry to assemble from, so index assembly
  is skipped and the single host-arch build is additionally tagged `repo:vX.Y.Z` — which is what
  `scripts/release/40-build.sh`'s baked-version check runs against. It also runs
  `push-security-reports.sh` when `PUSH_SECURITY_REPORTS=true`, which **git-commits and pushes**
  `security-reports/` to whatever branch is checked out.
