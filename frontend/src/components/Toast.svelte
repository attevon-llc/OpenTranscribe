<script lang="ts">
  import { fly } from 'svelte/transition';
  import { createEventDispatcher, onDestroy, onMount } from 'svelte';
  import { t } from '$stores/locale';
  import { retryWaitLabel } from '$lib/utils/formatting';
  import type { ToastAction } from '$stores/toast';
  import MenuIcon from '$components/ui/MenuIcon.svelte';
  import type { MenuIconName } from '$lib/ui/menuIcons';

  export let message: string = '';
  export let type: 'success' | 'error' | 'info' | 'warning' = 'success';
  export let duration: number = 3000;
  /** Issue #788: seconds until a rate-limited action can be retried. */
  export let retryAfterSeconds: number | undefined = undefined;
  /** Issue #788: an optional clickable action, e.g. a "Retry" button. */
  export let action: ToastAction | undefined = undefined;

  const dispatch = createEventDispatcher();

  // Non-error toasts are advisory, not alarming -- `role="status"` announces
  // politely instead of interrupting like `role="alert"` (issue #788 / #785).
  $: toastRole = type === 'error' || type === 'warning' ? 'alert' : 'status';
  $: retryLabel = retryAfterSeconds != null ? retryWaitLabel(retryAfterSeconds) : null;

  // Issue #788 (last AC): the `action` slot is disabled until `retryAfterSeconds`
  // elapses, then silently re-enables. Deliberately drives the BUTTON's `disabled`
  // attribute on a timer rather than re-rendering the toast's text -- the toast is
  // `role="alert"`/`role="status"`, so a ticking or changing text node would
  // re-announce to a screen reader every tick (J7 in the #788 plan; see
  // `retryWaitLabel`'s doc comment for the same reasoning on the hint text). A
  // `disabled` attribute flip on an element with unchanged text content is not an
  // accessibility-tree text mutation and does not trigger a live-region
  // announcement, so the wait produces exactly one state change and zero
  // re-announcements.
  //
  // No `retryAfterSeconds` (header absent/unparseable) means there is nothing to
  // count down -- the action starts enabled immediately rather than staying
  // permanently disabled or showing a NaN-derived wait.
  let actionDisabled = false;
  let reenableTimer: ReturnType<typeof setTimeout> | undefined;

  onMount(() => {
    if (action && typeof retryAfterSeconds === 'number' && retryAfterSeconds > 0) {
      actionDisabled = true;
      reenableTimer = setTimeout(() => {
        actionDisabled = false;
      }, retryAfterSeconds * 1000);
    }
  });

  onDestroy(() => {
    if (reenableTimer) clearTimeout(reenableTimer);
  });

  // Drawn white on a disc of the toast colour, so these are bare marks, not circled icons.
  const icons: Record<typeof type, MenuIconName> = {
    success: 'check',
    error: 'x',
    warning: 'exclamation',
    info: 'info-mark',
  };

  // Auto-dismiss after duration
  if (duration > 0) {
    setTimeout(() => {
      dispatch('dismiss');
    }, duration);
  }

  function dismiss() {
    dispatch('dismiss');
  }
</script>

<div
  class="toast toast-{type}"
  transition:fly={{ y: 50, duration: 300 }}
  role={toastRole}
>
  <span class="toast-icon" data-type={type}><MenuIcon name={icons[type]} size="xs" /></span>
  <span class="toast-message">
    {message}
    {#if retryLabel}
      <span class="toast-retry-hint">{$t(retryLabel.key, { count: retryLabel.count })}</span>
    {/if}
  </span>
  {#if action}
    <button
      class="toast-action"
      on:click={() => {
        if (!actionDisabled) action.onClick();
      }}
      disabled={actionDisabled}
      aria-disabled={actionDisabled}
    >
      {action.label}
    </button>
  {/if}
  <button class="toast-close" on:click={dismiss} aria-label={$t('toast.dismiss')}>
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
      <line x1="18" y1="6" x2="6" y2="18"></line>
      <line x1="6" y1="6" x2="18" y2="18"></line>
    </svg>
  </button>
</div>

<style>
  .toast-success {
    --toast-solid: var(--success-solid);
    --toast-text: var(--success-text);
    --toast-on: var(--text-on-solid);
  }
  .toast-error {
    --toast-solid: var(--error-solid);
    --toast-text: var(--error-text);
    --toast-on: var(--text-on-solid);
  }
  .toast-warning {
    --toast-solid: var(--warning-solid);
    --toast-text: var(--warning-text);
    --toast-on: var(--text-on-warning);
  }
  .toast-info {
    --toast-solid: var(--info-solid);
    --toast-text: var(--info-text);
    --toast-on: var(--text-on-solid);
  }

  .toast {
    display: flex;
    align-items: center;
    gap: var(--space-3);
    background: var(--background-color);
    padding: var(--space-3) var(--space-4);
    border-radius: var(--radius-lg);
    box-shadow: var(--shadow-lg);
    margin-bottom: var(--space-2);
    min-width: 300px;
    max-width: 500px;
    border: 1px solid var(--border-color);
    border-inline-start: 4px solid var(--toast-solid);
  }

  .toast-icon {
    width: 20px;
    height: 20px;
    display: flex;
    align-items: center;
    justify-content: center;
    background: var(--toast-solid);
    color: var(--toast-on);
    border-radius: var(--radius-round);
    flex-shrink: 0;
  }

  .toast-message {
    flex: 1;
    font-size: var(--font-size-md);
    color: var(--text-primary);
    font-weight: var(--font-weight-medium);
    min-width: 0;
    /* A sentence wraps to three lines before it is cut: a one-line clip hid the reason in
       messages like "speaker detection is back to Automatic". Short messages are unchanged. */
    overflow: hidden;
    display: -webkit-box;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 3;
    line-clamp: 3;
    overflow-wrap: anywhere;
  }

  .toast-retry-hint {
    display: block;
    font-size: var(--font-size-xs);
    font-weight: var(--font-weight-regular);
    color: var(--text-secondary);
    white-space: normal;
  }

  .toast-action {
    background: none;
    border: 1px solid var(--toast-solid);
    color: var(--toast-text);
    border-radius: var(--radius-sm);
    padding: var(--space-1) var(--space-2-5);
    font-size: var(--font-size-sm);
    font-weight: var(--font-weight-semibold);
    cursor: pointer;
    flex-shrink: 0;
    transition: all var(--duration-base);
  }

  .toast-action:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .toast-action:hover:not(:disabled) {
    background: var(--toast-solid);
    color: var(--toast-on);
  }

  .toast-close {
    background: none;
    border: none;
    cursor: pointer;
    padding: var(--space-1);
    display: flex;
    align-items: center;
    justify-content: center;
    border-radius: var(--radius-sm);
    color: var(--text-secondary);
    transition: all var(--duration-base);
  }

  .toast-close:hover {
    background: var(--surface-hover);
    color: var(--text-primary);
  }
</style>
