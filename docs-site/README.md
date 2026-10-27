# OpenTranscribe Documentation Site

Source for the OpenTranscribe documentation website, built with [Docusaurus](https://docusaurus.io/)
3 and published at <https://docs.opentranscribe.app>. The same site is also built into the
`opentranscribe-docs` image that the app serves at `/docs/` for offline use.

Maintainer notes (generated content, MDX gotchas, what to check when writing pages) are in
[`CLAUDE.md`](CLAUDE.md). Read it before editing.

## Quick Start

Requires Node 24 or newer (`package.json` `engines`); `.nvmrc` pins 26, which CI and the Docker
build use.

```bash
cd docs-site
npm ci            # or npm install
npm start         # dev server with live reload, http://localhost:3000
npm run build     # static site into build/
npm run serve     # serve the built site locally
```

`npm run build` fails on any broken link or broken heading anchor (`onBrokenLinks` and
`onBrokenAnchors` are `throw`), so it doubles as the link checker. If a failed build leaves the
next one hanging with no progress, run `npx docusaurus clear` and build again.

Other scripts in `package.json`: `typecheck` (`tsc`), `clear`, `swizzle`, `write-heading-ids`,
`write-translations`, `deploy`.

To build the way the in-app copy is built (pages served under `/docs/`):

```bash
DOCS_BASE_URL=/docs/ npm run build
```

## Project Structure

```
docs-site/
  docs/                    Documentation pages
    getting-started/       Introduction, quick start, first transcription, screenshots
    installation/          Docker Compose, deployment variants, GPU, offline, troubleshooting
                           (_generated/ holds partials produced from code; do not hand-edit)
    authentication/        Auth methods: OIDC, LDAP, Keycloak, SAML, PKI, groups, email
    configuration/         Environment variables, nginx, multi-GPU, search, password policy
    features/              Feature documentation
    user-guide/            End-user guides
    operations/            Backup, monitoring, hardening, runbooks, fresh deployments
    developer-guide/       Architecture, testing, releasing, RAG, contributing
    legal/                 Legal documents
    faq.md
  blog/                    Release announcements; authors.yml and tags.yml live here
  src/
    pages/                 Custom pages: index.tsx, architecture.tsx, roadmap.tsx
    components/            React components
    data/roadmap.json      Data for the roadmap page (generated, see below)
    css/, theme/           Styling and MDX component overrides
  static/                  Images (img/, img/screenshots/), architecture/*.html, CNAME
  architecture-diagrams/   Diagram specs that the architecture page is rendered from
  docusaurus.config.ts     Site configuration
  sidebars.ts              Sidebar navigation (an explicit list)
  Dockerfile, nginx.conf   Image that serves the built site
```

## Writing Documentation

### Creating a New Page

1. Create a Markdown file in the matching directory under `docs/`.
2. Add frontmatter:

   ```markdown
   ---
   title: Page Title
   ---

   # Page Heading
   ```

3. **Add the page to `sidebars.ts`.** The sidebar is an explicit list, not generated from the
   file tree, so a page that is not listed there is not reachable from the navigation.
4. Run `npm run build` and fix any broken link or anchor it reports.

Describe the UI with its real labels from `frontend/src/lib/i18n/locales/en.json`, and make
claims about processing match the code, not intent.

### Markdown Features

Docusaurus admonitions, tabs and syntax-highlighted code blocks all work, as do Mermaid diagrams
(`markdown.mermaid` is enabled).

```markdown
:::tip Pro Tip
This is a helpful tip!
:::

:::warning
Be careful with this!
:::
```

**Comments, blog truncation and explicit heading IDs: MDX form only.** Every `.md` file here is
compiled as MDX, which parses `{ ... }` as a JavaScript expression and rejects HTML comments.
Use the MDX forms:

```markdown
{/* an ordinary comment */}

{/* truncate */}

## A heading with a stable anchor {/* #explicit-id */}
```

The CommonMark spellings (`<!-- comment -->`, `<!-- truncate -->`, `## Heading {#id}`) fail the
build. Give a heading an explicit ID when other pages link to it: the auto-generated slug changes
whenever the heading is reworded, and `onBrokenAnchors: 'throw'` only catches that for links
inside this site. `npm run write-heading-ids` can add IDs in bulk.

Files whose name starts with `_` are partials, never routed; import them as MDX components.

### Adding Images

Put images under `static/img/` (screenshots under `static/img/screenshots/<area>/`) and
reference them with an absolute path:

```markdown
![Alt text](/img/screenshots/gallery/example.png)
```

### Blog Posts

Posts live in `blog/` as `YYYY-MM-DD-slug.md`:

```markdown
---
slug: v0.6.0-release
title: "Post title"
authors: [opentranscribe]
tags: [release, features]
---

Summary shown on the blog index.

{/* truncate */}

The rest of the post.
```

- `slug` must be unique across the blog.
- `authors` must be keys from `blog/authors.yml` (currently `opentranscribe` and `davidamacey`).
- Take `tags` from `blog/tags.yml`; add a tag there before using a new one.
- `draft: true` keeps a post out of the production build (it still shows in `npm start`). Remove
  it, and rename the file to the publication date, when the release goes out.

## Generated Content

Three things here are produced from code or from the issue tracker. Edit the source, never the
output.

| Output | Source | Regenerate | Drift check |
| --- | --- | --- | --- |
| `docs/installation/_generated/*.md` | build/scan scripts, ASR provider catalog, pipeline plan | `backend/venv/bin/python scripts/generate-pipeline-docs.py --write` (repo root) | `scripts/generate-pipeline-docs.py --check` in CI, `backend/tests/unit/test_pipeline_docs_tables.py` |
| `static/architecture/*.html` (the `/architecture` page) | `architecture-diagrams/specs/*.json` | `scripts/generate-architecture-diagrams.sh [name]`; needs the Archify skill, see [`architecture-diagrams/README.md`](architecture-diagrams/README.md) | `architecture-diagram-drift` pre-commit hook, `backend/tests/unit/test_architecture_diagram_drift.py` |
| `src/data/roadmap.json` (the `/roadmap` page) | GitHub milestones and `epic:*` issue labels | `python3 scripts/generate-roadmap.py` (repo root, needs an authenticated `gh`) | none; the deploy workflow regenerates it on every build |

The committed `roadmap.json` is a snapshot used by the offline Docker image; the live site
regenerates it from the tracker at deploy time.

## Deployment

The public site deploys to GitHub Pages through `.github/workflows/deploy-docs.yml`:

- A push to `master` that touches `docs-site/**` or `VERSION` builds and deploys.
- A daily scheduled run (06:17 UTC) redeploys so the roadmap stays current.
- `workflow_dispatch` runs it on demand.
- A pull request touching the same paths runs the build job only and never deploys.

The custom domain is set by `static/CNAME` (`docs.opentranscribe.app`) and `url` in
`docusaurus.config.ts`. The workflow checks out full git history because the homepage version
badge compares `VERSION` with the newest release tag.

The in-app copy is built by `docs-site/Dockerfile` (Node build stage, then nginx) with
`DOCS_BASE_URL=/docs/`; `./opentr.sh start prod --build` builds it along with the other images.

Before pushing, the pre-push pre-commit hook `docs-build` runs the same `npm run build`.

## Versioning and Translations

The site is not versioned (`docs` always tracks the default branch, which the announcement bar
says) and has a single locale, `en`. Docusaurus supports both
(`docusaurus docs:version`, `write-translations`) but neither is set up.

## Contributing

See [CONTRIBUTING.md](../docs/CONTRIBUTING.md) and the
[contributing guide](docs/developer-guide/contributing.md).

## Resources

- [Docusaurus Documentation](https://docusaurus.io/docs)
- [GitHub Issues](https://github.com/attevon-llc/OpenTranscribe/issues) for documentation problems
- [GitHub Discussions](https://github.com/attevon-llc/OpenTranscribe/discussions) for questions
