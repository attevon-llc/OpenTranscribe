<script lang="ts">
  import { page } from '$app/stores';
  import { t } from '$stores/locale';

  // SvelteKit renders this on load/navigation errors and for unmatched routes.
  $: status = $page.status;
  $: isNotFound = status === 404;
  $: errorMessage = $page.error?.message ?? '';

  $: heading = isNotFound ? $t('errorPage.notFoundTitle') : $t('errorPage.title');
  // Prefer the framework-provided message; fall back to a friendly translated one.
  $: body = errorMessage || (isNotFound ? $t('errorPage.notFoundMessage') : $t('errorPage.genericMessage'));

  function reload() {
    if (typeof window !== 'undefined') window.location.reload();
  }
</script>

<svelte:head>
  <title>{heading}</title>
</svelte:head>

<div class="error-page">
  <div class="error-card">
    <p class="error-status">{$t('errorPage.statusLabel')} {status}</p>
    <h1 class="error-heading">{heading}</h1>
    <p class="error-body">{body}</p>
    <div class="error-actions">
      <a class="error-home" href="/">{$t('errorPage.goHome')}</a>
      <button type="button" class="error-reload" on:click={reload}>
        {$t('errorPage.reload')}
      </button>
    </div>
  </div>
</div>

<style>
  .error-page {
    display: flex;
    align-items: center;
    justify-content: center;
    min-height: 60vh;
    padding: 2rem 1rem;
  }

  .error-card {
    width: 100%;
    max-width: 480px;
    text-align: center;
    background: var(--surface-color, #ffffff);
    border: 1px solid var(--border-color, #e0e0e0);
    border-radius: 12px;
    box-shadow: 0 10px 30px rgba(0, 0, 0, 0.08);
    padding: 2.5rem 2rem;
    color: var(--text-color, #1a1a1a);
  }

  .error-status {
    margin: 0 0 0.75rem;
    font-size: 0.85rem;
    font-weight: 600;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    color: var(--text-secondary, #64748b);
  }

  .error-heading {
    margin: 0 0 0.75rem;
    font-size: 1.6rem;
    line-height: 1.2;
    color: var(--text-color, #1a1a1a);
  }

  .error-body {
    margin: 0 0 1.75rem;
    font-size: 1rem;
    line-height: 1.5;
    color: var(--text-secondary, #64748b);
    word-break: break-word;
  }

  .error-actions {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: center;
    gap: 0.75rem;
  }

  .error-home,
  .error-reload {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-height: 44px;
    padding: 0.6rem 1.5rem;
    border-radius: 8px;
    font-weight: 600;
    font-size: 1rem;
    cursor: pointer;
    transition: background-color 0.2s ease, transform 0.2s ease;
  }

  /* Primary action — "Go home". */
  .error-home {
    background: var(--primary-color, #3b82f6);
    color: #ffffff;
    text-decoration: none;
    border: none;
  }

  .error-home:hover {
    background: var(--primary-hover, #2563eb);
    transform: translateY(-1px);
  }

  /* Secondary action — "Reload" (grey, never blue). */
  .error-reload {
    background: var(--surface-color, #ffffff);
    color: var(--text-color, #1a1a1a);
    border: 1px solid var(--border-color, #e0e0e0);
  }

  .error-reload:hover {
    background: var(--button-hover, #f1f5f9);
    transform: translateY(-1px);
  }

  .error-home:focus-visible,
  .error-reload:focus-visible {
    outline: 2px solid var(--primary-color, #3b82f6);
    outline-offset: 2px;
  }

  @media (prefers-reduced-motion: reduce) {
    .error-home,
    .error-reload {
      transition: none;
    }
    .error-home:hover,
    .error-reload:hover {
      transform: none;
    }
  }
</style>
