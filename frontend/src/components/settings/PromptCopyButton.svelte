<script lang="ts">
  import { onDestroy } from 'svelte';
  import { copyToClipboard } from '$lib/utils/clipboard';
  import { t } from '$stores/locale';

  /** The prompt text to put on the clipboard. */
  export let text: string;

  // How long the button stays in its "Copied" state.
  const COPIED_MS = 2000;

  let isCopied = false;
  let resetTimer: ReturnType<typeof setTimeout> | undefined;

  onDestroy(() => clearTimeout(resetTimer));

  function copy() {
    if (!text) return;

    void copyToClipboard(
      text,
      () => {
        isCopied = true;
        clearTimeout(resetTimer);
        resetTimer = setTimeout(() => {
          isCopied = false;
        }, COPIED_MS);
      },
      () => {
        // Failed - just keep the default state
      }
    );
  }
</script>

<button
  type="button"
  class="copy-button-header"
  class:copied={isCopied}
  on:click={copy}
  aria-label={$t('prompts.copy')}
  title={isCopied ? $t('prompts.copiedToClipboard') : $t('prompts.copy')}
>
  {#if isCopied}
    <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor">
      <path d="M10.97 4.97a.75.75 0 0 1 1.07 1.05l-3.99 4.99a.75.75 0 0 1-1.08.02L4.324 8.384a.75.75 0 1 1 1.06-1.06l2.094 2.093 3.473-4.425a.267.267 0 0 1 .02-.022z"/>
    </svg>
    {$t('prompts.copied')}
  {:else}
    <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor">
      <path d="M4 1.5H3a2 2 0 0 0-2 2V14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V3.5a2 2 0 0 0-2-2h-1v1h1a1 1 0 0 1 1 1V14a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1V3.5a1 1 0 0 1 1-1h1v-1z"/>
      <path d="M9.5 1a.5.5 0 0 1 .5.5v1a.5.5 0 0 1-.5.5h-3a.5.5 0 0 1-.5-.5v-1a.5.5 0 0 1 .5-.5h3zm-3-1A1.5 1.5 0 0 0 5 1.5v1A1.5 1.5 0 0 0 6.5 4h3A1.5 1.5 0 0 0 11 2.5v-1A1.5 1.5 0 0 0 9.5 0h-3z"/>
    </svg>
    {$t('prompts.copy')}
  {/if}
</button>

<style>
  /* Matches the SummaryModal copy button. */
  .copy-button-header {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    background: var(--bg-primary);
    border: 1px solid var(--border-color);
    color: var(--text-secondary);
    padding: var(--space-2) var(--space-3);
    border-radius: var(--radius-md);
    cursor: pointer;
    transition: all var(--duration-base) ease;
    font-size: var(--font-size-sm);
  }

  .copy-button-header:hover {
    background-color: var(--hover-bg);
    color: var(--primary-on-surface);
    border-color: var(--primary-color);
  }

  .copy-button-header.copied,
  .copy-button-header.copied:hover {
    background-color: var(--success-bg);
    border-color: var(--success-color);
    color: var(--success-color);
  }
</style>
