<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import type { IconName, Tone } from '$lib/notifications/display';
  import NotificationIcon from './NotificationIcon.svelte';

  /** Item chrome shared by plain rows and processing cards: indicator, icon, dismiss. */
  export let icon: IconName;
  export let tone: Tone = 'default';
  export let read = false;
  export let processing = false;
  export let dismissible = true;
  export let extraClass = '';

  const dispatch = createEventDispatcher<{ dismiss: void }>();
  $: dismissLabel = dismissible ? $t('notifications.dismiss') : $t('notifications.processingCannotDismiss');
</script>

<li
  class="notification-item {read ? 'read' : 'unread'} status-{tone} {extraClass}"
  class:processing
>
  <div class="notification-indicator" aria-hidden="true"></div>

  <div class="notification-icon">
    <NotificationIcon name={icon} />
  </div>

  <div class="notification-content">
    <slot />
  </div>

  <button
    type="button"
    class="notification-dismiss"
    class:non-dismissible={!dismissible}
    on:click={() => dispatch('dismiss')}
    title={dismissLabel}
    aria-label={dismissLabel}
    disabled={!dismissible}
  >
    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      <line x1="18" y1="6" x2="6" y2="18"></line>
      <line x1="6" y1="6" x2="18" y2="18"></line>
    </svg>
  </button>
</li>

<style>
  .notification-item {
    position: relative;
    display: flex;
    align-items: flex-start;
    gap: 12px;
    padding: 16px 20px;
    border-bottom: 1px solid var(--border-color);
    transition: background-color 0.2s ease;
    background: var(--background-color);
    list-style: none;
  }

  .notification-item:last-child {
    border-bottom: none;
  }

  .notification-item:hover {
    background: var(--hover-color, rgba(0, 0, 0, 0.025));
  }

  .notification-item.unread {
    background: rgba(var(--primary-color-rgb), 0.04);
  }

  :global([data-theme='dark']) .notification-item.unread {
    background: rgba(var(--primary-color-rgb), 0.08);
  }

  .notification-indicator {
    position: absolute;
    inset-inline-start: 0;
    top: 0;
    bottom: 0;
    width: 3px;
    background: transparent;
  }

  .status-success .notification-indicator {
    background: var(--success-color);
  }

  .status-error .notification-indicator {
    background: var(--error-color);
  }

  .status-info .notification-indicator,
  .unread .notification-indicator {
    background: var(--primary-color);
  }

  .status-warning .notification-indicator {
    background: var(--warning-color, #f59e0b);
  }

  /* An error keeps its colour even while unread: it is the thing to notice. */
  .unread.status-error .notification-indicator {
    background: var(--error-color);
  }

  .processing .notification-indicator {
    background: var(--primary-color);
    animation: pulse 1.5s infinite;
  }

  @keyframes pulse {
    0%,
    100% {
      opacity: 1;
    }
    50% {
      opacity: 0.5;
    }
  }

  .notification-icon {
    flex-shrink: 0;
    width: 32px;
    height: 32px;
    border-radius: 8px;
    display: flex;
    align-items: center;
    justify-content: center;
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    color: var(--text-secondary);
    margin-top: 2px;
  }

  .status-success .notification-icon {
    background: rgba(16, 185, 129, 0.1);
    border-color: rgba(16, 185, 129, 0.2);
    color: var(--success-color);
  }

  .status-error .notification-icon {
    background: rgba(239, 68, 68, 0.1);
    border-color: rgba(239, 68, 68, 0.2);
    color: var(--error-color);
  }

  .status-info .notification-icon {
    background: rgba(var(--primary-color-rgb), 0.1);
    border-color: rgba(var(--primary-color-rgb), 0.2);
    color: var(--primary-on-surface);
  }

  .status-warning .notification-icon {
    background: rgba(245, 158, 11, 0.1);
    border-color: rgba(245, 158, 11, 0.2);
    color: var(--warning-color, #f59e0b);
  }

  .notification-content {
    flex: 1;
    min-width: 0;
  }

  .notification-dismiss {
    flex-shrink: 0;
    width: 24px;
    height: 24px;
    /* Resets the global `button { padding: 0.6rem 1.2rem }` (38.4px), which is wider than
       this 24px box and would clamp the content box to zero, hiding the icon (#746). */
    padding: 0;
    background: none;
    border: none;
    border-radius: 4px;
    /* Flat icon control: the global button rule's shadow drew a raised box around the x. */
    box-shadow: none;
    cursor: pointer;
    color: var(--text-secondary);
    display: flex;
    align-items: center;
    justify-content: center;
    transition:
      background-color 0.2s ease,
      color 0.2s ease;
    opacity: 0.6;
  }

  .notification-dismiss:hover {
    background: var(--error-background, rgba(239, 68, 68, 0.1));
    color: var(--error-color);
    opacity: 1;
    box-shadow: none;
    transform: none;
  }

  .notification-dismiss.non-dismissible,
  .notification-dismiss.non-dismissible:hover {
    background: none;
    color: var(--text-secondary);
    opacity: 0.3;
    cursor: not-allowed;
  }

  @media (max-width: 480px) {
    .notification-item {
      padding: 14px 16px;
      gap: 10px;
    }

    .notification-icon {
      width: 28px;
      height: 28px;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .notification-item,
    .notification-dismiss {
      transition: none;
    }

    .processing .notification-indicator {
      animation: none;
    }
  }

  @media (prefers-contrast: high) {
    .notification-item {
      border-bottom-width: 2px;
    }

    .notification-indicator {
      width: 4px;
    }
  }
</style>
