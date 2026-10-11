<script lang="ts">
  import { t } from '$stores/locale';
  import { clearedNotice } from '$stores/notificationRetention';
</script>

<div class="cleared-notice" role="status" aria-live="polite">
  {#if $clearedNotice > 0}
    <span>{$t('notifications.clearedNotice', { count: $clearedNotice })}</span>
    <button type="button" on:click={clearedNotice.dismiss} aria-label={$t('notifications.clearedNoticeDismiss')}>
      <svg
        width="14"
        height="14"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="2"
        stroke-linecap="round"
        stroke-linejoin="round"
        aria-hidden="true"
      >
        <line x1="18" y1="6" x2="6" y2="18"></line>
        <line x1="6" y1="6" x2="18" y2="18"></line>
      </svg>
    </button>
  {/if}
</div>

<style>
  .cleared-notice:empty {
    display: none;
  }

  .cleared-notice {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    padding: 8px 20px;
    border-bottom: 1px solid var(--border-color);
    background: var(--background-color);
    color: var(--text-secondary);
    font-size: 0.8125rem;
  }

  button {
    display: inline-flex;
    flex: none;
    align-items: center;
    justify-content: center;
    width: 24px;
    height: 24px;
    padding: 0;
    border: none;
    border-radius: 4px;
    background: transparent;
    color: var(--text-secondary);
    cursor: pointer;
  }

  button:hover {
    color: var(--text-color);
  }

  @media (max-width: 640px) {
    .cleared-notice {
      padding: 8px 16px;
    }
  }
</style>
