# frontend/src/components/fileStatus

## Purpose

Thin presentational children of `UserFileStatus.svelte` (the coordinator), split out of that
file. They render the task/processing-status dashboard (filters, task grid, detail modal).

## Key files

- `TaskFilterPanel.svelte` — status/type/age/date filters (two-way bound; parent owns refetch).
- `TasksGrid.svelte` — paginated task table; dispatches `viewDetails` / `pageChange`.
- `FileDetailModal.svelte` — per-file processing detail + retry; dispatches `close` / `retry`.

## Conventions / patterns

- Import via `$components/fileStatus/...`; i18n via `$t` from `$stores/locale`.
- Children take props + `createEventDispatcher`; filter state is two-way bound to the parent,
  which holds the source of truth and the refetch reactivity.

## How it connects

- Parent/coordinator: `$components/UserFileStatus.svelte` (rendered on `/file-status`).

## Gotchas

- **Data loading + real-time updates stay in the coordinator**: `UserFileStatus.svelte` owns
  `fetchTasks`, the `apiCache`, and the `websocketStore` subscription (push-based refresh, with
  a polling fallback). Children must not fetch or subscribe — emit events and let the parent reload.
