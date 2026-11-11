<script lang="ts">
  import { t } from '$stores/locale';

  export let shareCount: number = 0;
  export let isShared: boolean = false;
</script>

{#if isShared || shareCount > 0}
  <span class="share-badge" title={$t('sharing.sharedWithCount', { count: shareCount })}>
    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <path d="M4 12v8a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8"/>
      <polyline points="16 6 12 2 8 6"/>
      <line x1="12" y1="2" x2="12" y2="15"/>
    </svg>
    {#if shareCount > 0}
      <span class="count">{shareCount}</span>
    {:else}
      <span class="label">{$t('sharing.shared')}</span>
    {/if}
  </span>
{/if}

<style>
  .share-badge {
    display: inline-flex;
    align-items: center;
    gap: var(--space-1);
    padding: var(--space-0-5) var(--space-2);
    border-radius: var(--radius-2xl);
    font-size: var(--font-size-2xs);
    font-weight: var(--font-weight-medium);
    background: rgba(var(--accent-rgb), 0.1);
    color: var(--primary-on-surface);
    white-space: nowrap;
  }

  .count, .label {
    line-height: 1;
  }
</style>
