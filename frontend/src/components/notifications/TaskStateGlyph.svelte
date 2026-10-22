<script lang="ts">
  import type { TaskState } from '$lib/notifications/processingJob';

  /** Decorative state mark; the state is always also given as text. */
  export let state: TaskState;
</script>

<svg
  class="glyph state-{state}"
  width="12"
  height="12"
  viewBox="0 0 24 24"
  fill="none"
  stroke="currentColor"
  stroke-width="3"
  stroke-linecap="round"
  stroke-linejoin="round"
  aria-hidden="true"
>
  {#if state === 'done'}
    <polyline points="20 6 9 17 4 12"></polyline>
  {:else if state === 'failed'}
    <line x1="18" y1="6" x2="6" y2="18"></line>
    <line x1="6" y1="6" x2="18" y2="18"></line>
  {:else if state === 'running'}
    <path d="M21 12a9 9 0 1 1-6.22-8.56"></path>
  {:else if state === 'skipped'}
    <line x1="5" y1="12" x2="19" y2="12"></line>
  {:else}
    <circle cx="12" cy="12" r="8" stroke-width="2.5"></circle>
  {/if}
</svg>

<style>
  .glyph {
    flex-shrink: 0;
  }

  .state-done {
    color: var(--success-color);
  }

  .state-failed {
    color: var(--error-color);
  }

  .state-running {
    color: var(--primary-on-surface);
    animation: spin 1s linear infinite;
  }

  .state-pending,
  .state-skipped {
    color: var(--text-secondary);
  }

  @keyframes spin {
    to {
      transform: rotate(360deg);
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .state-running {
      animation: none;
    }
  }
</style>
