# SBOM / commercial license review

Generates a per-package license inventory for the backend (Python), frontend (npm), and the
Debian/Alpine OS packages baked into the production Docker images, classified against a
permissive-only commercial license policy. Start with `SBOM-SUMMARY.md` — it has the flagged
items up top.

## Regenerating

Point the scan at the image tags you're about to ship (defaults below are the currently
published `v0.5.1` images — bump the tags before a release, or point at `:latest`).

```bash
# 1. Backend: scan the actual production image (Python packages + Debian OS packages)
trivy image --scanners license --format json --skip-version-check \
  davidamacey/opentranscribe-backend:v0.5.1 > /tmp/trivy-backend.json

# 2. Frontend: scan the actual production image (Alpine OS packages)
trivy image --scanners license --format json --skip-version-check \
  davidamacey/opentranscribe-frontend:v0.5.1 > /tmp/trivy-frontend.json

# 3. Frontend npm packages: install into an isolated temp dir so this never touches
#    frontend/node_modules (which the dev container may be using) or requires root.
mkdir -p /tmp/frontend-sbom-install
cp frontend/package.json frontend/package-lock.json /tmp/frontend-sbom-install/
(cd /tmp/frontend-sbom-install && npm ci --no-audit --no-fund --omit=dev)
(cd /tmp/frontend-sbom-install && npx --yes license-checker --json \
  --out /tmp/license-checker-frontend-prod.json)
(cd /tmp/frontend-sbom-install && npm install --no-audit --no-fund --include=dev --package-lock=false)
(cd /tmp/frontend-sbom-install && npx --yes license-checker --json \
  --out /tmp/license-checker-frontend-all.json)

# 4. (optional, adds author/homepage columns to the backend Python table) pip-licenses
#    against the local backend venv - author metadata only, not the license source of truth.
backend/venv/bin/pip install -q pip-licenses
backend/venv/bin/pip-licenses --format=json --with-authors --with-urls \
  --with-license-file --no-license-path > /tmp/pip-licenses-all.json

# 5. Classify everything and write the reports
backend/venv/bin/python scripts/generate-sbom-report.py
```

## Why trivy image scans, not `pip freeze` / `npm ls`

The license source of truth is **what's actually inside the shipped Docker image** —
`davidamacey/opentranscribe-backend`/`-frontend` on Docker Hub — not the host dev venv (which
also has pytest/mypy/ruff installed, none of which ship) or a local `node_modules` (which may be
stale). Scanning the real image is what a customer's legal/procurement team would do too.

npm is the one exception: the frontend's production Docker image is a built static bundle
(nginx serving compiled JS/CSS) with no `node_modules` inside it at all — the dependency graph
that matters is what got **compiled into** that bundle, which `license-checker` against a clean
`npm ci --omit=dev` install reflects.

## Known gaps / things to re-check periodically

- **OS-layer license strings are long-tail Debian copyright-file text** (e.g.
  `BSD-3-Clause AND CC-BY-4.0 AND CC-BY-SA-3.0 AND cipic AND ...` from font/codec packages)
  that don't map to a single SPDX ID — those land in `UNKNOWN` rather than being guessed at.
  They're virtually all fonts/codecs pulled in transitively by `ffmpeg`'s Debian package, not
  hand-picked dependencies.
- **The classifier is intentionally conservative**: anything it can't confidently place in the
  permissive allowlist is `UNKNOWN`, not `OK`. Re-run after any new dependency is added.
- **This is not legal advice.** It's a systematic first pass so attorneys aren't starting from
  zero — `scripts/generate-sbom-report.py`'s policy tables (`ALLOW`/`CONCERN`/`BLOCKER_EXACT`)
  are the editable source of truth if legal wants a different classification for a specific
  license family.
