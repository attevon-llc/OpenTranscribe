# `$lib/cloud` — managed-edition seam (community stub)

This directory is the frontend's single extension point for the commercial
managed edition, mirroring the backend's provider-registry/hooks seams. The
community build ships these inert stubs; the managed edition's image build
replaces the whole directory with its real implementation.

Rules for core code:

- Import **only** `$lib/cloud` (the `index.ts` surface) or a component path
  under `$lib/cloud/components/`. Never deep-import anything else from here.
- Gate every call site with `isCloudEdition` from `$lib/edition` (compile-time
  constant) so all of it tree-shakes out of community bundles.
- `index.ts` defines the seam contract. Signature changes here must be
  mirrored by the overlay — change the stub first, then the overlay.
- No vendor names in this tree or in core call sites: CI's seam-guard greps
  `frontend/src` (and `backend/app`) and fails the build on a match.
- `locales/` may be added by the overlay (per-locale flat JSON string packs);
  `$lib/i18n` merges any packs it finds there at init. The community stub
  intentionally has none.
