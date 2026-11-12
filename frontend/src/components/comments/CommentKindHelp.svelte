<script lang="ts">
  import { domId } from '$lib/utils/domId';
  import { t } from '$stores/locale';

  const panelId = domId('comment-kind-help');
  let open = false;
</script>

<button
  type="button"
  class="info-icon"
  aria-expanded={open}
  aria-controls={panelId}
  aria-label={$t('comments.kindHelpTitle')}
  title={$t('comments.kindHelpTitle')}
  on:click={() => (open = !open)}
>
  <svg
    width="16"
    height="16"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-width="2"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
  >
    <circle cx="12" cy="12" r="10" />
    <line x1="12" y1="16" x2="12" y2="12" />
    <line x1="12" y1="8" x2="12.01" y2="8" />
  </svg>
</button>
{#if open}
  <div id={panelId} class="kind-help" role="region" aria-label={$t('comments.kindHelpTitle')}>
    <p>{$t('comments.kindTimedHelp')}</p>
    <p>{$t('comments.kindGeneralHelp')}</p>
    <p>{$t('comments.kindPinHelp')}</p>
  </div>
{/if}

<style>
  .info-icon {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    background: transparent;
    border: none;
    box-shadow: none;
    padding: var(--space-0-5);
    border-radius: var(--radius-round);
    color: var(--text-secondary);
    cursor: pointer;
  }

  .info-icon:hover {
    color: var(--primary-on-surface);
    background: var(--button-hover);
    box-shadow: none;
  }

  .kind-help {
    flex-basis: 100%;
    border: 1px solid var(--border-color);
    border-radius: var(--radius-md);
    background: var(--bg-secondary);
    padding: var(--space-2) var(--space-3);
    font-size: var(--font-size-sm);
    color: var(--text-color);
  }

  .kind-help p {
    margin: 0 0 var(--space-1);
  }

  .kind-help p:last-child {
    margin: 0;
  }
</style>
