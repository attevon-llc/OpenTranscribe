# docs-site — the Docusaurus documentation site

Repo-wide rules live in the root `CLAUDE.md`. Build check: `cd docs-site && npm ci && npm run
build` (`onBrokenLinks` and `onBrokenAnchors` are `throw`, so a broken link or anchor fails it).
Pages live in `docs/`, the sidebar is the explicit list in `sidebars.ts` (a page not listed there
is unreachable from the navigation).

⚠️ A build that FAILED (e.g. an MDX compile error) can leave the bundler's persistent cache in a
state where the next `npm run build` sits at "Creating an optimized production build..." at 0%
CPU forever (observed twice on 2026-10-11, 25+ min each). `npx docusaurus clear` and rebuild:
the clean build then finished in under 2 minutes.

## Generated content: edit the source, never the output

Two kinds of content here are produced from code and checked for drift. Hand-editing the output
fails CI.

| Output | Source | Regenerate | Drift check |
|---|---|---|---|
| `docs/installation/_generated/*.md` (images, provider speakers/timing, validity matrix), imported by `docs/installation/deployment-variants.md` | `scripts/docker-build-push.sh list-platforms`, `scripts/security-scan.sh list-repos`, `ASR_PROVIDER_CATALOG`, `services/pipeline_plan.resolve_pipeline_plan` | `backend/venv/bin/python scripts/generate-pipeline-docs.py --write` | `--check` in CI's backend job, `backend/tests/unit/test_pipeline_docs_tables.py` |
| `static/architecture/*.html` (the `/architecture` page) | `architecture-diagrams/specs/*.json` | `scripts/generate-architecture-diagrams.sh [name]` (needs Archify; see `architecture-diagrams/README.md`) | `architecture-diagram-drift` pre-commit hook, `backend/tests/unit/test_architecture_diagram_drift.py` |

So a change to the ASR catalog, the pipeline plan or the image platform table is a docs change
too: regenerate the partials in the same commit. Never transcribe those facts into prose
elsewhere; link to `installation/deployment-variants.md#which-cloud-providers-return-speakers`
or `#which-combinations-are-valid` instead.

## Writing pages

- Partials start with `_` (never routed) and are imported as MDX components
  (`import X from './_generated/x.md'`). Comments in MDX are `{/* ... */}`, not `<!-- -->`.
- Custom heading ids (`## Title {#id}`) do NOT parse in this MDX setup and break the build; rely
  on the generated slug.
- Describe UI with its real labels from `frontend/src/lib/i18n/locales/en.json`. A feature that
  only the API exposes (today: `disable_diarization` upload flag) must be described as API-only.
- Claims about processing must match `services/pipeline_plan.py`, not intent: e.g.
  the `ENABLE_DIARIZATION` variable is gone (speaker detection follows each user's setting), lite has no local transcription or
  speaker detection, and there is no forced alignment.
