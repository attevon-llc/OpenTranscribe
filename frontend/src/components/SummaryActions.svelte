<script lang="ts">
  import { onMount } from 'svelte';
  import { createEventDispatcher } from 'svelte';
  import type { SummaryData } from '$lib/types/summary';
  import axiosInstance from '$lib/axios';
  import { t } from '$stores/locale';
  import Spinner from './ui/Spinner.svelte';

  export let summary: SummaryData | null;
  export let generating: boolean = false;
  export let llmAvailable: boolean = false;
  export let canRetry: boolean = false;
  export let summaryStatus: string = 'pending';
  export let canGenerate: boolean = true;
  export let summaryEnabledSystem: boolean = true;

  const dispatch = createEventDispatcher<{
    generateSummary: void;
    retrySummary: void;
    regenerateWithPrompt: { promptUuid: string | null };
  }>();

  let availablePrompts: any[] = [];
  let selectedPromptUuid: string | null = null;
  let loadingPrompts = false;

  async function fetchPrompts() {
    loadingPrompts = true;
    try {
      const response = await axiosInstance.get('/prompts');
      availablePrompts = response.data.prompts || [];
    } catch (err: unknown) {
      console.error('Error fetching prompts:', err);
    } finally {
      loadingPrompts = false;
    }
  }

  function handleRegenerateWithPrompt() {
    dispatch('regenerateWithPrompt', { promptUuid: selectedPromptUuid });
  }

  onMount(() => {
    fetchPrompts();
  });
</script>

<div class="summary-actions">
  {#if !summary && summaryStatus === 'disabled'}
    <div class="disabled-badge">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <circle cx="12" cy="12" r="10"/>
        <line x1="4.93" y1="4.93" x2="19.07" y2="19.07"/>
      </svg>
      {$t('summary.statusDisabled')}
    </div>
    {#if canGenerate}
      <button
        class="action-button primary"
        on:click={() => dispatch('generateSummary')}
        disabled={generating}
      >
        {#if generating}
          <Spinner size="small" color="currentColor" />
          {$t('summary.generatingSummary')}
        {:else}
          {$t('summary.generateAnyway')}
        {/if}
      </button>
    {:else if !summaryEnabledSystem}
      <span class="admin-hint">{$t('summary.adminEnableHint')}</span>
    {/if}
  {:else if !summary && llmAvailable && summaryStatus === 'pending'}
    <button
      class="action-button primary"
      on:click={() => dispatch('generateSummary')}
      disabled={generating}
    >
      {#if generating}
        <Spinner size="small" color="currentColor" />
        {$t('summary.generatingSummary')}
      {:else}
        <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor">
          <path d="M8 4a.5.5 0 0 1 .5.5v3h3a.5.5 0 0 1 0 1h-3v3a.5.5 0 0 1-1 0v-3h-3a.5.5 0 0 1 0-1h3v-3A.5.5 0 0 1 8 4z"/>
        </svg>
        {$t('summary.generateSummary')}
      {/if}
    </button>
  {:else if !summary && !llmAvailable && summaryStatus === 'pending'}
    <span class="admin-hint">{$t('llm.featuresUnavailable')}</span>
  {:else if !summary && canRetry && (summaryStatus === 'failed' || summaryStatus === 'error')}
    <button
      class="action-button warning"
      on:click={() => dispatch('retrySummary')}
      disabled={generating}
    >
      {#if generating}
        <Spinner size="small" color="currentColor" />
        {$t('summary.retrying')}
      {:else}
        <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor">
          <path d="M11.534 7h3.932a.25.25 0 0 1 .192.41l-1.966 2.36a.25.25 0 0 1-.384 0l-1.966-2.36a.25.25 0 0 1 .192-.41zm-11 2h3.932a.25.25 0 0 0 .192-.41L2.692 6.23a.25.25 0 0 0-.384 0L.342 8.59A.25.25 0 0 0 .534 9z"/>
          <path fill-rule="evenodd" d="M8 3c-1.552 0-2.94.707-3.857 1.818a.5.5 0 1 1-.771-.636A6.002 6.002 0 0 1 13.917 7H12.9A5.002 5.002 0 0 0 8 3zM3.1 9a5.002 5.002 0 0 0 8.757 2.182.5.5 0 1 1 .771.636A6.002 6.002 0 0 1 2.083 9H3.1z"/>
        </svg>
        {$t('summary.retrySummaryGeneration')}
      {/if}
    </button>
  {:else if !summary && !canRetry && (summaryStatus === 'failed' || summaryStatus === 'error')}
    <span class="admin-hint">{$t('summary.retryUnavailableHint')}</span>
  {/if}

  {#if summary && llmAvailable}
    <div class="prompt-picker">
      <select
        class="prompt-select"
        bind:value={selectedPromptUuid}
        disabled={generating || loadingPrompts}
        title={$t('summary.selectPromptTooltip')}
      >
        <option value={null}>{$t('summary.useActivePrompt')}</option>
        {#each availablePrompts as prompt}
          <option value={prompt.uuid}>
            {prompt.name}{prompt.is_system_default ? ` (${$t('summary.systemPrompt')})` : ''}
          </option>
        {/each}
      </select>
      <button
        class="action-button regenerate"
        on:click={handleRegenerateWithPrompt}
        disabled={generating}
        title={$t('summary.regenerateWithPromptTooltip')}
      >
        {#if generating}
          <Spinner size="small" color="currentColor" />
          {$t('summary.regenerating')}
        {:else}
          <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor">
            <path d="M11.534 7h3.932a.25.25 0 0 1 .192.41l-1.966 2.36a.25.25 0 0 1-.384 0l-1.966-2.36a.25.25 0 0 1 .192-.41zm-11 2h3.932a.25.25 0 0 0 .192-.41L2.692 6.23a.25.25 0 0 0-.384 0L.342 8.59A.25.25 0 0 0 .534 9z"/>
            <path fill-rule="evenodd" d="M8 3c-1.552 0-2.94.707-3.857 1.818a.5.5 0 1 1-.771-.636A6.002 6.002 0 0 1 13.917 7H12.9A5.002 5.002 0 0 0 8 3zM3.1 9a5.002 5.002 0 0 0 8.757 2.182.5.5 0 1 1 .771.636A6.002 6.002 0 0 1 2.083 9H3.1z"/>
          </svg>
          {$t('summary.regenerateWithPrompt')}
        {/if}
      </button>
    </div>
  {/if}
</div>

<style>
  .summary-actions {
    display: flex;
    gap: 1rem;
    padding: 1rem;
    background-color: var(--bg-secondary);
    border-top: 1px solid var(--border-color);
    flex-wrap: wrap;
    align-items: center;
  }

  .action-button {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.75rem 1.25rem;
    border: none;
    border-radius: 6px;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s ease;
  }

  .action-button:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .action-button.primary {
    background-color: var(--primary-color);
    color: white;
  }

  .action-button.primary:hover:not(:disabled) {
    background-color: var(--primary-dark);
  }

  .action-button.warning {
    background-color: var(--warning-color);
    color: white;
  }

  .action-button.warning:hover:not(:disabled) {
    background-color: var(--warning-dark);
  }

  .action-button.regenerate {
    background-color: var(--primary-color);
    color: white;
    padding: 0.5rem 1rem;
    font-size: 0.85rem;
  }

  .action-button.regenerate:hover:not(:disabled) {
    background-color: var(--primary-dark);
  }

  .prompt-picker {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex: 1;
    min-width: 0;
  }

  .prompt-select {
    flex: 1;
    min-width: 0;
    padding: 0.5rem 0.75rem;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    background-color: var(--input-background);
    color: var(--text-color);
    font-size: 0.85rem;
    cursor: pointer;
    transition: border-color 0.2s ease;
    appearance: none;
    -webkit-appearance: none;
    padding-right: 2.5rem;
  }

  .prompt-select:focus {
    outline: none;
    border-color: var(--primary-color);
    box-shadow: 0 0 0 2px rgba(59, 130, 246, 0.1);
  }

  .prompt-select:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .disabled-badge {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.5rem 1rem;
    border-radius: 6px;
    font-size: 0.85rem;
    font-weight: 500;
    color: var(--text-muted);
    background: var(--surface-color);
    border: 1px solid var(--border-color);
  }

  .admin-hint {
    font-size: 0.8rem;
    color: var(--text-muted);
    font-style: italic;
  }

  @media (max-width: 768px) {
    .summary-actions {
      flex-direction: column;
    }

    .prompt-picker {
      width: 100%;
    }
  }
</style>
