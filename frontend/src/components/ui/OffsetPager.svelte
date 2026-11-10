<!--
  OffsetPager.svelte — previous/next pager for offset-paginated admin tables.
  The parent owns the data: it reloads on `onpage(newOffset)`.
-->
<script lang="ts">
  import { t } from '$stores/locale';

  export let offset: number;
  export let total: number;
  export let pageSize: number;
  export let loading = false;
  export let onpage: (offset: number) => void;
</script>

<div class="pagination">
  <button disabled={offset === 0 || loading} on:click={() => onpage(Math.max(0, offset - pageSize))}>
    {$t('common.previous')}
  </button>
  <span>{offset + 1}-{Math.min(offset + pageSize, total)} / {total}</span>
  <button disabled={offset + pageSize >= total || loading} on:click={() => onpage(offset + pageSize)}>
    {$t('common.next')}
  </button>
</div>

<style>
  .pagination {
    display: flex;
    align-items: center;
    gap: var(--space-3);
    margin-top: var(--space-3);
    font-size: var(--font-size-sm);
  }

  .pagination button {
    padding: var(--space-1) var(--space-3);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-md);
    background: var(--bg-color);
    color: var(--text-color);
    cursor: pointer;
  }

  .pagination button:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
</style>
