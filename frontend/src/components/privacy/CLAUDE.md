# frontend/src/components/privacy

## Purpose

Shared pieces of the privacy controls (issue #1280): voiceprint matching and inferred gender under one
restrict-only policy (platform, deployment, organization and user; any layer off = off). The panels that
use them live in `components/settings/` (`OrgPrivacyPolicyPanel`, `InstancePrivacyCard`, `UserPrivacyCard`).

## Key files

- `PrivacySwitchCard.svelte` - one feature: a CONTROLLED switch (it shows `checked` and asks the parent to
  change it, so a refused save leaves it truthful), the "who locked this" sentence, and its purge button.
- `PurgeConfirmDialog.svelte` - the one confirmation for an irreversible purge: ticked box AND the typed
  word `PURGE` before the destructive button enables; resets on every open; follows the purge task and
  reports counts only. The caller supplies `start`, the one API call for its scope.
- `SpeakerPrivacyNotice.svelte` - the explanation on speaker surfaces (see `speakers/CLAUDE.md`).

## Gotchas

- Switching a feature off HIDES data; only the purge deletes it, and the API refuses a purge while the
  switch is on. Never enable a purge button from a draft value, only from the server's.
- Layer names and error codes come from `backend/app/schemas/privacy.py`; copy is chosen by
  `$lib/settings/privacyPolicy` through literal-key maps, never built from a variable.
- `stores/privacyPolicy` caches `/privacy/effective` for 15 s and is cleared by `purgeTenantDataCaches`.
  Keep it out of the root layout and `keyMaps.ts` (bundle budget).
