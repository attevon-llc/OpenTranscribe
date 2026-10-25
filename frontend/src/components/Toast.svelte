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

  const colors = {
    success: '#10b981',
    error: '#ef4444',
    warning: '#f59e0b',
    info: '#3b82f6'
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
  style="--toast-color: {colors[type]}"
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
  .toast {
    display: flex;
    align-items: center;
    gap: 12px;
    background: var(--background-color);
    padding: 12px 16px;
    border-radius: 8px;
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.1);
    margin-bottom: 8px;
    min-width: 300px;
    max-width: 500px;
    border-left: 4px solid var(--toast-color);
    border: 1px solid var(--border-color);
  }

  :global([data-theme='dark']) .toast {
    background: var(--background-color);
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.3);
    border-color: var(--border-color);
  }

  .toast-icon {
    width: 20px;
    height: 20px;
    display: flex;
    align-items: center;
    justify-content: center;
    background: var(--toast-color);
    color: white;
    border-radius: 50%;
    flex-shrink: 0;
  }

  .toast-message {
    flex: 1;
    font-size: 14px;
    color: var(--text-primary);
    font-weight: 500;
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

  :global([data-theme='dark']) .toast-message {
    color: var(--text-primary);
  }

  .toast-retry-hint {
    display: block;
    font-size: 12px;
    font-weight: 400;
    color: var(--text-secondary);
    white-space: normal;
  }

  .toast-action {
    background: none;
    border: 1px solid var(--toast-color);
    color: var(--toast-color);
    border-radius: 4px;
    padding: 4px 10px;
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;
    flex-shrink: 0;
    transition: all 0.2s;
  }

  .toast-action:hover:not(:disabled) {
    background: var(--toast-color);
    color: white;
  }

  :global([data-theme='dark']) .toast-action:hover:not(:disabled) {
    color: var(--background-color);
  }

  .toast-action:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .toast-close {
    background: none;
    border: none;
    cursor: pointer;
    padding: 4px;
    display: flex;
    align-items: center;
    justify-content: center;
    border-radius: 4px;
    color: var(--text-secondary);
    transition: all 0.2s;
  }

  .toast-close:hover {
    background: rgba(0, 0, 0, 0.05);
    color: var(--text-primary);
  }

  :global([data-theme='dark']) .toast-close:hover {
    background: rgba(255, 255, 255, 0.1);
  }
</style>
