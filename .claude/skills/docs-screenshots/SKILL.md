---
name: docs-screenshots
description: Capture or refresh OpenTranscribe docs-site screenshots and feature GIFs from the live dev stack. Use when the user says "update the screenshots", "the docs images are stale", "capture a screenshot of X", "make a GIF of the upload/view flow", "/docs-screenshots", or when a feature has shipped with no screenshots under docs-site/static/img/screenshots/. Also trigger after UI changes that would make existing screenshots visibly wrong (new tabs, renamed buttons, redesigned panels).
---

# Docs screenshots & GIFs

Drives the real app in a headless (or XRDP-visible) browser via the system-wide `browse.js`
tool and writes PNGs straight into `docs-site/static/img/screenshots/<category>/`, matching the
conventions already established by `docs/research-paper/capture_screenshots.sh` (the pattern this
skill generalizes).

## Prerequisites

- Dev stack running: `./opentr.sh start dev` (screenshots must reflect real data/UI, not a cold
  empty instance — seed a couple of completed files first if the category needs them, e.g. gallery,
  transcript, search).
- `node ~/bin/browser-tools/browse.js` installed (see `~/bin/browser-tools/README.md`).
- `ffmpeg` for GIF assembly (`which ffmpeg` — already present on this host).
- Login creds: `admin@example.com` / `password` (dev-stack only, never used against prod).
- On XRDP, pass `--display=:11` to watch it run; omit for headless (faster, works over SSH).

## Category directory conventions

Existing categories under `docs-site/static/img/screenshots/`: `auth`, `gallery`, `upload`,
`processing`, `transcript`, `speakers`, `search`, `settings`, `ai-features`, `info`, `workflow`.
Filenames are `NN-kebab-case-description.png` (zero-padded, sequential per category) — check
`ls docs-site/static/img/screenshots/<category>/` for the next number before adding to an existing
category. New categories get their own subdirectory (e.g. `chat`, `watch-sources`, `redaction`).

## Step 1: capture screenshots

Use `scripts/capture.sh <category> <name> <browse.js actions...>` (relative to this skill dir).
It wraps the login+theme boilerplate, runs the action sequence, then copies the result into
`docs-site/static/img/screenshots/<category>/`. Example — capturing the chat panel:

```bash
.claude/skills/docs-screenshots/scripts/capture.sh chat 01-chat-panel-with-citations \
  'click:a[href="/search"]' \
  'wait:1000'
# add whatever click/fill/wait steps are needed to reach the target UI state,
# the script appends the final 'screenshot:<name>' step itself
```

For light/dark parity (project convention — every frontend feature needs both), capture twice:
once as-is (dark, the default `capture.sh` theme) and once with `'click:.theme-toggle'` prepended
for light mode, following the `-dark`/`-light` or `-light-mode`/`-dark-mode` suffix convention
already used under `gallery/`.

Inspect selectors first if the flow isn't already known — `browse.js`'s `eval:` and `text` actions
are the fastest way to find them:
```bash
node ~/bin/browser-tools/browse.js http://localhost:5173 \
  'fill:#email:admin@example.com' 'fill:#password:password' 'click:button[type=submit]' 'wait:3000' \
  "eval:Array.from(document.querySelectorAll('button, a')).map(e => e.textContent.trim()).filter(Boolean)"
```

## Step 2: optimize

Large/uncompressed PNGs bloat the docs-site build. After capturing, run:
```bash
python3 scripts/organize-all-screenshots.py   # only if using its SOURCE_DIR/mapping workflow
```
For one-off captures from `capture.sh` (already written straight to the target path), just check
size — anything over ~200KB, re-save via `sips`/`pillow` at `PNG optimize=True` or reduce viewport
width. `capture.sh` defaults to a 1440x900 viewport, matching the existing screenshot set.

## Step 3: wire into docs

Add the `<Img src="/img/screenshots/<category>/<file>.png" .../>` block to the relevant page under
`docs-site/docs/` (and to `docs-site/docs/getting-started/screenshots.mdx` if it belongs in the
full visual walkthrough — that page's `Img` helper is a **lowercase** custom component, not the
Docusaurus `<Img>`; using the capitalized form recurses infinitely, see the comment at the top of
that file). Then verify the build:
```bash
cd docs-site && npm run build
```

## GIFs

`scripts/capture-gif.sh <output-name> <scene1-name> <scene2-name> ...` takes an ordered list of
**already-captured** screenshot names (from Step 1, PNGs living under
`~/bin/browser-tools/screenshots/`) and assembles them into a paletted, high-quality GIF at
`docs-site/static/img/<output-name>.gif` — each frame held ~1.8s, matching the pacing of the
existing `opentranscribe-workflow.gif`. This is a **discrete-scene** GIF (distinct UI states held
for a beat), not a smooth screen recording — that's what the current workflow GIF actually is, and
it keeps file size sane. Capture each scene as a normal screenshot first, then stitch:

```bash
# capture each scene (Step 1) with unique names, then:
.claude/skills/docs-screenshots/scripts/capture-gif.sh opentranscribe-upload-view-workflow \
  login-empty upload-modal upload-progress gallery-processing transcript-view
```

## When re-running for a release

Compare `git log --oneline -1 -- docs-site/static/img/screenshots/<category>/` against the feature's
last real UI change before assuming a screenshot is current — don't recapture everything blindly,
only what's actually stale or missing (check `docs-site/docs/*/*.md` for `<Img src=.../>` references
to categories that don't exist yet, or a category whose newest file predates a feature's shipped
date in `CHANGELOG.md`).
