<script lang="ts">
  /**
   * Single status affordance shared by `VirtualList` and `VirtualGrid` (issue #749).
   *
   * Before this component the two views ran different systems: the list rendered the
   * status word unconditionally, the grid collapsed it into a `max-width: 0 -> 120px`
   * hover reveal with `cursor: help` and no `title` at all — a tooltip cursor promising
   * a tooltip that did not exist. This renders one dot+glyph icon plus a native
   * `title`/`aria-label` tooltip everywhere, and an optional inline word for callers
   * (list view) that still want one.
   *
   * Covers all ten `MediaFileStatus` members, not just the two the grid/list styled
   * before (`queued`/`downloading` had zero `.status-*` CSS in either view).
   */
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import type { MediaFileStatus } from '$lib/types/media';

  export let status: MediaFileStatus;
  /** Backend-formatted status text (`file.display_status`) — preferred over the local i18n fallback. */
  export let displayStatus: string | undefined = undefined;
  /** True only for `error` with a `user_message` to show — the one status with a click target. */
  export let clickable = false;
  /** List view renders the word inline; grid view relies on the icon + native tooltip only. */
  export let showLabel = false;

  const dispatch = createEventDispatcher<{ click: void }>();

  const STATUS_KEYS: Record<MediaFileStatus, string> = {
    pending: 'common.pending',
    queued: 'common.queued',
    downloading: 'common.downloading',
    processing: 'common.processing',
    completed: 'common.completed',
    error: 'common.error',
    cancelling: 'common.cancelling',
    cancelled: 'common.cancelled',
    orphaned: 'common.orphaned',
    quarantined: 'common.quarantined',
  };

  $: label = displayStatus || $t(STATUS_KEYS[status]);

  function handleClick(e: MouseEvent) {
    if (!clickable) return;
    e.preventDefault();
    e.stopPropagation();
    dispatch('click');
  }
</script>

<!-- svelte-ignore a11y-click-events-have-key-events -->
<!-- svelte-ignore a11y-no-static-element-interactions -->
<span
  class="status-indicator status-{status}"
  class:status-clickable={clickable}
  title={label}
  aria-label={label}
  on:click={handleClick}
>
  <svg class="status-icon" viewBox="0 0 16 16" width="10" height="10" aria-hidden="true" focusable="false">
    <circle cx="8" cy="8" r="7" fill="currentColor" />
    {#if status === 'completed'}
      <path
        d="M4.7 8.3l2.1 2.1 4.5-4.6"
        fill="none"
        stroke="var(--surface-color)"
        stroke-width="1.7"
        stroke-linecap="round"
        stroke-linejoin="round"
      />
    {:else if status === 'error'}
      <path
        d="M5.3 5.3l5.4 5.4M10.7 5.3l-5.4 5.4"
        fill="none"
        stroke="var(--surface-color)"
        stroke-width="1.5"
        stroke-linecap="round"
      />
    {/if}
  </svg>
  {#if showLabel}<span class="status-text">{label}</span>{/if}
</span>

<style>
  .status-indicator {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    cursor: help;
    width: fit-content;
    white-space: nowrap;
  }

  .status-indicator.status-clickable {
    cursor: pointer;
  }

  .status-indicator.status-clickable .status-text {
    text-decoration: underline;
    text-decoration-style: dotted;
  }

  .status-indicator.status-clickable:hover .status-text {
    text-decoration-style: solid;
  }

  .status-icon {
    flex-shrink: 0;
  }

  /* Statuses actively in flight (issue #749/§13.3 — queued/downloading previously had
     no styling at all in either view). */
  .status-pending,
  .status-queued,
  .status-downloading,
  .status-processing,
  .status-cancelling {
    color: #f59e0b;
  }

  .status-completed {
    color: #10b981;
  }

  .status-error {
    color: #ef4444;
  }

  .status-cancelled {
    color: #6b7280;
  }

  .status-orphaned {
    color: #dc2626;
  }

  /* Abuse / DMCA takedown hold — amber, distinct from the red error states. */
  .status-quarantined {
    color: #d97706;
  }

  @keyframes status-pulse {
    0% { opacity: 0.6; }
    50% { opacity: 1; }
    100% { opacity: 0.6; }
  }

  .status-processing .status-icon,
  .status-downloading .status-icon {
    animation: status-pulse 2s ease-in-out infinite;
  }

  @media (prefers-reduced-motion: reduce) {
    .status-processing .status-icon,
    .status-downloading .status-icon {
      animation: none;
    }
  }
</style>
