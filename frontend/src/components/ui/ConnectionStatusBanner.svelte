<script lang="ts">
  /**
   * Slim, non-intrusive banner that surfaces real-time connection loss.
   *
   * The WebSocket store reconnects on its own (exponential backoff); this component only
   * *observes* {@link connectionStatus} and tells the user when live updates are paused so a
   * silent disconnect doesn't look like a frozen app. Hidden entirely while connected.
   */
  import { connectionStatus, websocketStore } from '$stores/websocket';
  import { t } from '$stores/locale';

  $: status = $connectionStatus;
  $: visible = status !== 'connected';
  $: message =
    status === 'reconnecting'
      ? $t('connection.reconnecting')
      : $t('connection.disconnected');

  function retryNow() {
    websocketStore.connect();
  }
</script>

{#if visible}
  <div
    class="connection-banner"
    class:reconnecting={status === 'reconnecting'}
    role="status"
    aria-live="polite"
  >
    <span class="connection-dot" aria-hidden="true"></span>
    <span class="connection-message">{message}</span>
    {#if status === 'disconnected'}
      <button type="button" class="connection-retry" on:click={retryNow}>
        {$t('connection.retry')}
      </button>
    {/if}
  </div>
{/if}

<style>
  .connection-banner {
    position: fixed;
    bottom: 1rem;
    left: 50%;
    transform: translateX(-50%);
    z-index: 1200;
    display: flex;
    align-items: center;
    gap: 0.6rem;
    max-width: calc(100vw - 2rem);
    padding: 0.55rem 1rem;
    border-radius: 999px;
    background: var(--surface-color, #ffffff);
    border: 1px solid var(--warning-color, #f59e0b);
    box-shadow: 0 6px 20px rgba(0, 0, 0, 0.18);
    color: var(--text-color, #1a1a1a);
    font-size: 0.875rem;
    line-height: 1.3;
  }

  .connection-dot {
    flex: 0 0 auto;
    width: 9px;
    height: 9px;
    border-radius: 50%;
    background: var(--warning-color, #f59e0b);
  }

  /* Pulse only while actively reconnecting; steady when fully disconnected. */
  .connection-banner.reconnecting .connection-dot {
    animation: connection-pulse 1.4s ease-in-out infinite;
  }

  .connection-message {
    min-width: 0;
  }

  .connection-retry {
    flex: 0 0 auto;
    padding: 0.3rem 0.75rem;
    border-radius: 999px;
    border: 1px solid var(--border-color, #e0e0e0);
    background: var(--button-hover, #f1f5f9);
    color: var(--text-color, #1a1a1a);
    font-size: 0.8rem;
    font-weight: 600;
    cursor: pointer;
    transition: background-color 0.2s ease;
  }

  .connection-retry:hover {
    background: var(--border-color, #e0e0e0);
  }

  .connection-retry:focus-visible {
    outline: 2px solid var(--primary-color, #3b82f6);
    outline-offset: 2px;
  }

  @keyframes connection-pulse {
    0%,
    100% {
      opacity: 1;
    }
    50% {
      opacity: 0.4;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .connection-banner.reconnecting .connection-dot {
      animation: none;
    }
  }
</style>
