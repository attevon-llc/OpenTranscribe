# Original exploration report

`index.html` is the working document from the initial codebase-visualization session: the
full comparison of static-analysis tools tried (madge, dependency-cruiser, pyreverse) against
Archify, the root-cause writeups for why the raw tool output was unusable, and the
route/reach-trace findings. It is kept here for reference only.

**This is not part of the docs site build** (it lives outside `static/` and `src/`, so
Docusaurus never touches it) and it is not linked from anywhere on the live site. The
production-quality output of this exploration is `/architecture` on the docs site
(`docs-site/src/pages/architecture.tsx`), which shows only the 10 curated, validated Archify
diagrams — the specific use cases, not the tool-comparison narrative.

To view this file, open it directly in a browser. It references the diagram HTML files by
relative filename (`archify-architecture.html`, `celery-queues.html`, etc.) — those are the
pre-viewBox-fix, pre-scale-normalization originals and do **not** match the current
`docs-site/static/architecture/*.html` builds. Treat this purely as a historical record of how
the diagrams were built and iterated, not as a current reference.
