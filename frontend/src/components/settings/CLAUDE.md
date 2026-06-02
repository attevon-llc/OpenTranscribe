# frontend/src/components/settings

## Purpose

The ~40 settings panels rendered inside `SettingsModal.svelte`. Covers user prefs
(transcription, download, redaction, language…), admin config (ASR, engine, auth, retention,
embedding migration…), and dashboards (UserProfileSettings, SystemStatisticsPanel,
AdminTaskHealthPanel).

## Key files

- `../SettingsModal.svelte` — the **shell/router**: vertical sidebar nav + `{#if activeSection}`
  routing to one panel at a time. Owns `activeSection` (from `settingsModalStore`) and shared
  modals (ConfirmationModal, ProcessingDetailsModal).
- `UserProfileSettings.svelte` — account/profile section.
- `SystemStatisticsPanel.svelte`, `AdminTaskHealthPanel.svelte` — admin dashboards (export
  module-context interfaces consumed by the shell).
- `*Modal.svelte` (ASRConfigModal, LLMConfigModal) — nested editors opened from a panel.

## Conventions / patterns

- Each panel is self-contained: owns its own load/save against `$lib/api/*`; the shell only
  routes and passes `activeSection`. Import panels via `$components/settings/...`.
- Button styling uses the global `.btn`/`.btn-primary`/`.btn-secondary` classes (form-elements.css);
  cancel/reset buttons must hover grey, never blue. i18n via `$t`; light/dark parity required.
- Keep panels under ~300 lines; split large ones into nested modals.

## How it connects

- Parent: `SettingsModal.svelte`, opened via `$stores/settingsModalStore`. Backend admin/user
  settings endpoints through `$lib/api/*`.

## Gotchas

- **E2E-guarded selectors** in the shell: `.settings-modal`, `.settings-sidebar`, `.nav-item`,
  `.section-title`. Renaming them breaks Playwright tests — keep them stable.
- The modal closes itself on route change (`$page.url.pathname`) — don't re-add navigation logic.
