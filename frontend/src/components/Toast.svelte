<script lang="ts">
  import { fly } from 'svelte/transition';
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';

  export let message: string = '';
  export let type: 'success' | 'error' | 'info' | 'warning' = 'success';
  export let duration: number = 3000;

  const dispatch = createEventDispatcher();

  const icons = {
    success: '✓',
    error: '✗',
    warning: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/><path d="M12 9v4"/><path d="m12 17 .01 0"/></svg>`,
    info: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="m9 12 2 2 4-4"/></svg>`
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
  role="alert"
>
  <span class="toast-icon">{@html icons[type]}</span>
  <span class="toast-message">{message}</span>
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
    font-size: 12px;
    font-weight: bold;
    flex-shrink: 0;
  }

  .toast-message {
    flex: 1;
    font-size: 14px;
    color: var(--text-primary);
    font-weight: 500;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    min-width: 0;
  }

  :global([data-theme='dark']) .toast-message {
    color: var(--text-primary);
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
