<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';

  export let page: number = 1;
  export let totalPages: number = 1;

  const dispatch = createEventDispatcher<{ pageChange: number }>();

  function goToPage(p: number) {
    if (p >= 1 && p <= totalPages && p !== page) {
      dispatch('pageChange', p);
    }
  }

  $: pages = getVisiblePages(page, totalPages);

  function getVisiblePages(current: number, total: number): (number | '...')[] {
    const INITIAL_PAGES = 5;  // Show pages 1-5 initially (Google style)
    const AROUND_CURRENT = 2; // Show current ± 2 pages

    // If total pages fit comfortably, show all
    if (total <= INITIAL_PAGES + 4) {
      return Array.from({ length: total }, (_, i) => i + 1);
    }

    const result: (number | '...')[] = [];

    // Add initial pages (1-5)
    for (let i = 1; i <= INITIAL_PAGES; i++) {
      result.push(i);
    }

    // If current is beyond initial pages, add window around current
    if (current > INITIAL_PAGES) {
      // Add ellipsis if there's a gap
      if (current > INITIAL_PAGES + 1) {
        result.push('...');
      }

      // Add current ± 2 pages
      const windowStart = Math.max(INITIAL_PAGES + 1, current - AROUND_CURRENT);
      const windowEnd = Math.min(total - 1, current + AROUND_CURRENT);

      for (let i = windowStart; i <= windowEnd; i++) {
        result.push(i);
      }

      // Add ellipsis before last page if there's a gap
      if (windowEnd < total - 1) {
        result.push('...');
      }
    } else {
      // Current within initial pages, add ellipsis before last if needed
      if (INITIAL_PAGES < total - 1) {
        result.push('...');
      }
    }

    // Add last page if not already included
    if (total > INITIAL_PAGES && result[result.length - 1] !== total) {
      result.push(total);
    }

    return result;
  }
</script>

<nav class="pagination" aria-label={$t('pagination.nav')}>
  <button
    class="page-btn prev"
    disabled={page <= 1}
    on:click={() => goToPage(page - 1)}
    aria-label={$t('pagination.previousPage')}
  >
    <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <polyline points="15 18 9 12 15 6"></polyline>
    </svg>
  </button>

  {#each pages as p}
    {#if p === '...'}
      <span class="ellipsis">...</span>
    {:else}
      <button
        class="page-btn"
        class:active={p === page}
        on:click={() => goToPage(p)}
        aria-label={$t('pagination.page', { page: p })}
        aria-current={p === page ? 'page' : undefined}
      >
        {p}
      </button>
    {/if}
  {/each}

  <button
    class="page-btn next"
    disabled={page >= totalPages}
    on:click={() => goToPage(page + 1)}
    aria-label={$t('pagination.nextPage')}
  >
    <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
      <polyline points="9 18 15 12 9 6"></polyline>
    </svg>
  </button>
</nav>

<style>
  .pagination {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 0.25rem;
    margin-top: 1.5rem;
    padding: 1rem 0;
  }

  .page-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    min-width: 36px;
    height: 36px;
    padding: 0 0.5rem;
    border: 1px solid var(--border-color, #e5e7eb);
    border-radius: 6px;
    background: var(--surface-color, #fff);
    color: var(--text-color, #374151);
    font-size: 0.875rem;
    cursor: pointer;
    transition: all 0.15s;
  }

  .page-btn:hover:not(:disabled):not(.active) {
    background: var(--hover-color, #f3f4f6);
    border-color: var(--primary-color, #4f46e5);
  }

  .page-btn.active {
    background: var(--primary-color, #4f46e5);
    color: white;
    border-color: var(--primary-color, #4f46e5);
  }

  .page-btn:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }

  .ellipsis {
    padding: 0 0.25rem;
    color: var(--text-secondary, #6b7280);
  }
</style>
