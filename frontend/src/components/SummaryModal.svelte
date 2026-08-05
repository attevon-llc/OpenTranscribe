<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import type { SummaryData, SummaryResponse } from '$lib/types/summary';
  import axiosInstance from '$lib/axios';
  import { isLLMAvailable } from '../stores/llmStatus';
  import { copyToClipboard } from '$lib/utils/clipboard';
  import { t } from '$stores/locale';
  import { getErrorMessage, getErrorStatus } from '$lib/utils/apiError';
  import Spinner from './ui/Spinner.svelte';
  import BaseModal from './ui/BaseModal.svelte';

  // Import smaller components
  import SummaryDisplay from './SummaryDisplay.svelte';
  import SummarySearch from './SummarySearch.svelte';
  import SummaryActions from './SummaryActions.svelte';

  export let fileId: number;
  export let fileName: string = '';
  export let isOpen: boolean = false;

  const dispatch = createEventDispatcher<{
    close: void;
    generateSummary: { fileId: number };
    reprocessSummary: { fileId: number };
    regenerateWithPrompt: { fileId: number; promptUuid: string | null };
  }>();

  let summary: SummaryData | null = null;
  let loading = false;
  let error: string | null = null;
  let generating = false;
  let summaryStatus: string = 'pending';
  let canRetry: boolean = false;
  let canGenerate: boolean = true;
  let summaryEnabledSystem: boolean = true;
  let summaryEnabledUser: boolean = true;

  // Get LLM availability from centralized store
  $: llmAvailable = $isLLMAvailable;

  // Search within summary
  let searchQuery = '';
  let currentMatchIndex = 0;
  let totalMatches = 0;

  $: if (isOpen && fileId) {
    loadSummary();
  }

  $: if (searchQuery && summary) {
    totalMatches = countMatches(searchQuery, summary);
    currentMatchIndex = 0;
  } else {
    totalMatches = 0;
    currentMatchIndex = 0;
  }

  async function loadSummary() {
    if (!fileId) return;

    loading = true;
    error = null;

    try {
      // First, check summary status and LLM availability
      try {
        const statusResponse = await axiosInstance.get(`/files/${fileId}/summary-status`);

        if (statusResponse.status === 200) {
          const statusData = statusResponse.data;
          summaryStatus = statusData.summary_status;
          llmAvailable = statusData.llm_available;
          canRetry = statusData.can_retry;
          canGenerate = statusData.can_generate ?? true;
          summaryEnabledSystem = statusData.summary_enabled_system ?? true;
          summaryEnabledUser = statusData.summary_enabled_user ?? true;
        }
      } catch (statusErr) {
        console.warn('Failed to load summary status:', statusErr);
        // Continue with summary loading even if status check fails
      }

      // Then try to load the actual summary if it exists
      try {
        const response = await axiosInstance.get(`/files/${fileId}/summary`);
        const data: SummaryResponse = response.data;
        summary = data.summary_data;
      } catch (summaryErr: unknown) {
        if (getErrorStatus(summaryErr) === 404) {
          // No summary exists yet
          summary = null;
        } else {
          throw new Error(
            $t('summary.loadFailedDetail', {
              error: getErrorMessage(summaryErr, $t('summary.loadFailed')),
            })
          );
        }
      }
    } catch (err) {
      error = err instanceof Error ? err.message : $t('summary.loadFailed');
      console.error('Error loading summary:', err);
    } finally {
      loading = false;
    }
  }

  async function generateSummary() {
    if (!fileId) return;

    generating = true;
    error = null;

    try {
      await axiosInstance.post(`/files/${fileId}/summarize`);

      // Poll for completion (simplified - you might want to use WebSockets)
      setTimeout(() => {
        loadSummary();
      }, 5000);

      dispatch('generateSummary', { fileId });
    } catch (err) {
      error = getErrorMessage(err, $t('summary.startFailed'));
    } finally {
      generating = false;
    }
  }

  async function retryFailedSummary() {
    if (!fileId) return;

    generating = true;
    error = null;

    try {
      await axiosInstance.post(`/files/${fileId}/retry-summary`);

      // Poll for completion
      setTimeout(() => {
        loadSummary();
      }, 5000);

      dispatch('generateSummary', { fileId });
    } catch (err) {
      error = getErrorMessage(err, $t('summary.retryFailed'));
    } finally {
      generating = false;
    }
  }

  function reprocessSummary() {
    if (!fileId) return;

    // Simply dispatch event to parent - parent handles everything
    dispatch('reprocessSummary', { fileId });
  }

  function regenerateWithPrompt(event: CustomEvent<{ promptUuid: string | null }>) {
    if (!fileId) return;
    dispatch('regenerateWithPrompt', { fileId, promptUuid: event.detail.promptUuid });
  }

  function countMatches(query: string, summaryData: SummaryData): number {
    if (!query.trim() || !summaryData) return 0;

    const searchTerm = query.toLowerCase();
    return countMatchesRecursive(summaryData, searchTerm);
  }

  function countMatchesRecursive(obj: any, searchTerm: string): number {
    if (!obj) return 0;

    let count = 0;

    if (typeof obj === 'string') {
      count += countInText(obj, searchTerm);
    } else if (Array.isArray(obj)) {
      obj.forEach(item => {
        count += countMatchesRecursive(item, searchTerm);
      });
    } else if (typeof obj === 'object') {
      // Skip metadata field
      Object.entries(obj).forEach(([key, value]) => {
        if (key !== 'metadata') {
          count += countMatchesRecursive(value, searchTerm);
        }
      });
    }

    return count;
  }

  function countInText(text: string, searchTerm: string): number {
    if (!text || !searchTerm) return 0;
    const escapedTerm = searchTerm.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const matches = text.toLowerCase().match(new RegExp(escapedTerm, 'g'));
    return matches ? matches.length : 0;
  }

  function cycleToNextMatch() {
    if (totalMatches > 0) {
      currentMatchIndex = (currentMatchIndex + 1) % totalMatches;
      scrollToCurrentMatch();
    }
  }

  function cycleToPreviousMatch() {
    if (totalMatches > 0) {
      currentMatchIndex = currentMatchIndex > 0 ? currentMatchIndex - 1 : totalMatches - 1;
      scrollToCurrentMatch();
    }
  }

  function scrollToCurrentMatch() {
    // Wait for the DOM to update with new highlighting
    setTimeout(() => {
      const currentMatch = document.querySelector(`[data-match-index="${currentMatchIndex}"].current-match`);
      if (currentMatch) {
        currentMatch.scrollIntoView({
          behavior: 'smooth',
          block: 'center',
          inline: 'nearest'
        });
      } else {
        // Fallback: find any current-match element
        const fallbackMatch = document.querySelector('.current-match');
        if (fallbackMatch) {
          fallbackMatch.scrollIntoView({
            behavior: 'smooth',
            block: 'center',
            inline: 'nearest'
          });
        }
      }
    }, 50);
  }

  function handleClose() {
    dispatch('close');
  }

  function handleSearchKeydown(event: CustomEvent<KeyboardEvent>) {
    // Handle other non-Enter keys if needed
    if (event.detail.key === 'Escape') {
      searchQuery = '';
    }
  }

  function handleCopy() {
    if (!summary) return;

    const markdown = formatSummaryAsMarkdown(summary);

    copyToClipboard(
      markdown,
      () => {
        copyButtonText = $t('summary.copied');
        setTimeout(() => {
          copyButtonText = $t('summary.copy');
        }, 2000);
      },
      (error) => {
        copyButtonText = $t('summary.copyFailed');
        setTimeout(() => {
          copyButtonText = $t('summary.copy');
        }, 2000);
      }
    );
  }

  function removeEmojis(text: string | null | undefined): string {
    // Remove all emoji characters using Unicode ranges
    if (!text) return '';
    return text.replace(/[\u{1F600}-\u{1F64F}]|[\u{1F300}-\u{1F5FF}]|[\u{1F680}-\u{1F6FF}]|[\u{1F1E0}-\u{1F1FF}]|[\u{2600}-\u{26FF}]|[\u{2700}-\u{27BF}]/gu, '').trim();
  }

  function formatSummaryAsMarkdown(data: SummaryData): string {
    let markdown = `# ${$t('summary.modalTitle', { fileName })}\n\n`;

    // Check if this is standard BLUF format or custom format
    const isStandardBLUF = !!(data.bluf && data.brief_summary);

    if (isStandardBLUF) {
      // Standard BLUF format
      if (data.bluf) {
        markdown += `## ${$t('summary.executiveSummary')}\n${removeEmojis(data.bluf)}\n\n`;
      }

      // Brief Summary
      if (data.brief_summary) {
        markdown += `## ${$t('summary.briefSummary')}\n${removeEmojis(data.brief_summary)}\n\n`;
      }

      // Major Topics
      if (data.major_topics && data.major_topics.length > 0) {
        markdown += `## ${$t('summary.majorTopics')}\n`;
        data.major_topics.forEach((topic: any) => {
          // Use text indicators instead of emojis
          const importanceText = topic.importance === 'high' ? `[${$t('summary.importance.high')}] ` : topic.importance === 'medium' ? `[${$t('summary.importance.medium')}] ` : `[${$t('summary.importance.low')}] `;
          markdown += `### ${importanceText}${removeEmojis(topic.topic || '')}\n`;
          if (topic.participants && topic.participants.length > 0) {
            markdown += `*${$t('summary.keyParticipants', { participants: topic.participants.join(', ') })}*\n\n`;
          }
          if (topic.key_points && topic.key_points.length > 0) {
            topic.key_points.forEach((point: string) => {
              markdown += `- ${removeEmojis(point)}\n`;
            });
          }
          markdown += '\n';
        });
      }

      // Key Decisions
      if (data.key_decisions && data.key_decisions.length > 0) {
        markdown += `## ${$t('summary.keyDecisions')}\n`;
        data.key_decisions.forEach((decision: any) => {
          const text = typeof decision === 'string' ? decision : (decision.decision || JSON.stringify(decision));
          markdown += `- ${removeEmojis(text)}\n`;
        });
        markdown += '\n';
      }

      // Follow-up Items
      if (data.follow_up_items && data.follow_up_items.length > 0) {
        markdown += `## ${$t('summary.followUpItems')}\n`;
        data.follow_up_items.forEach((item: any) => {
          const text = typeof item === 'string' ? item : (item.item || JSON.stringify(item));
          markdown += `- ${removeEmojis(text)}\n`;
        });
        markdown += '\n';
      }
    } else {
      // Custom format - recursively convert any structure to markdown
      markdown += formatCustomSummaryMarkdown(data, 2);
    }

    // AI Disclaimer
    if (data.metadata) {
      markdown += `---\n\n*${$t('summary.aiDisclaimer')} `;
      markdown += $t('summary.generatedBy', { provider: data.metadata.provider, model: data.metadata.model });
      if (data.metadata.processing_time_ms) {
        markdown += ` ${$t('summary.processingTime', { time: (data.metadata.processing_time_ms / 1000).toFixed(1) })}`;
      }
      markdown += `.*\n`;
    }

    return markdown;
  }

  function formatCustomSummaryMarkdown(obj: any, headingLevel: number = 2): string {
    let markdown = '';
    const headingPrefix = '#'.repeat(headingLevel);

    for (const [key, value] of Object.entries(obj)) {
      // Skip metadata field
      if (key === 'metadata') continue;

      // Format key as heading
      const formattedKey = key.replace(/_/g, ' ').replace(/\b\w/g, l => l.toUpperCase());
      markdown += `${headingPrefix} ${formattedKey}\n`;

      // Format value based on type
      if (value === null || value === undefined) {
        markdown += `*No data*\n\n`;
      } else if (typeof value === 'string') {
        markdown += `${removeEmojis(value)}\n\n`;
      } else if (Array.isArray(value)) {
        value.forEach(item => {
          if (typeof item === 'string') {
            markdown += `- ${removeEmojis(item)}\n`;
          } else if (typeof item === 'object' && item !== null) {
            // Extract text from object
            const text = item.text || item.decision || item.item || item.description || JSON.stringify(item);
            markdown += `- ${removeEmojis(text)}\n`;
          } else {
            markdown += `- ${String(item)}\n`;
          }
        });
        markdown += '\n';
      } else if (typeof value === 'object' && value !== null) {
        // Nested object - recurse with increased heading level
        markdown += formatCustomSummaryMarkdown(value, headingLevel + 1);
      } else {
        markdown += `${String(value)}\n\n`;
      }
    }

    return markdown;
  }

  let copyButtonText = $t('summary.copy');
</script>

<div class="summary-modal-wrapper">
<BaseModal {isOpen} onClose={handleClose} maxWidth="1200px">
      <svelte:fragment slot="header">
        <h2 class="modal-title">{$t('summary.modalTitle', { fileName })}</h2>
        {#if summary}
          <div class="header-actions">
              <button
                class="copy-button-header"
                class:copied={copyButtonText === $t('summary.copied')}
                on:click={handleCopy}
                aria-label={$t('summary.copySummaryLabel')}
                title={copyButtonText === $t('summary.copied') ? $t('summary.copiedToClipboard') : $t('summary.copySummaryMarkdown')}
              >
                {#if copyButtonText === $t('summary.copied')}
                  <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor">
                    <path d="M10.97 4.97a.75.75 0 0 1 1.07 1.05l-3.99 4.99a.75.75 0 0 1-1.08.02L4.324 8.384a.75.75 0 1 1 1.06-1.06l2.094 2.093 3.473-4.425a.267.267 0 0 1 .02-.022z"/>
                  </svg>
                  {$t('summary.copied')}
                {:else}
                  <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor">
                    <path d="M4 1.5H3a2 2 0 0 0-2 2V14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V3.5a2 2 0 0 0-2-2h-1v1h1a1 1 0 0 1 1 1V14a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1V3.5a1 1 0 0 1 1-1h1v-1z"/>
                    <path d="M9.5 1a.5.5 0 0 1 .5.5v1a.5.5 0 0 1-.5.5h-3a.5.5 0 0 1-.5-.5v-1a.5.5 0 0 1 .5-.5h3zm-3-1A1.5 1.5 0 0 0 5 1.5v1A1.5 1.5 0 0 0 6.5 4h3A1.5 1.5 0 0 0 11 2.5v-1A1.5 1.5 0 0 0 9.5 0h-3z"/>
                  </svg>
                  {$t('summary.copy')}
                {/if}
              </button>

              <!-- Reprocess button when summary exists and LLM is available -->
              {#if llmAvailable}
                <button
                  class="reprocess-button-header"
                  on:click={reprocessSummary}
                  disabled={generating}
                  aria-label={$t('summary.reprocessLabel')}
                  title={$t('summary.regenerateSummaryTooltip')}
                >
                  {#if generating}
                    <Spinner size="small" />
                  {:else}
                    <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor">
                      <path d="M11.534 7h3.932a.25.25 0 0 1 .192.41l-1.966 2.36a.25.25 0 0 1-.384 0l-1.966-2.36a.25.25 0 0 1 .192-.41zm-11 2h3.932a.25.25 0 0 0 .192-.41L2.692 6.23a.25.25 0 0 0-.384 0L.342 8.59A.25.25 0 0 0 .534 9z"/>
                      <path fill-rule="evenodd" d="M8 3c-1.552 0-2.94.707-3.857 1.818a.5.5 0 1 1-.771-.636A6.002 6.002 0 0 1 13.917 7H12.9A5.002 5.002 0 0 0 8 3zM3.1 9a5.002 5.002 0 0 0 8.757 2.182.5.5 0 1 1 .771.636A6.002 6.002 0 0 1 2.083 9H3.1z"/>
                    </svg>
                  {/if}
                  {$t('summary.reprocess')}
                </button>
              {/if}
          </div>
        {/if}
      </svelte:fragment>

      {#if loading}
        <div class="loading-container">
          <Spinner size="large" />
          <p>{$t('summary.loading')}</p>
        </div>
      {:else if error}
        <div class="error-container">
          <div class="error-icon">
            <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/>
              <path d="M12 9v4"/>
              <path d="m12 17 .01 0"/>
            </svg>
          </div>
          <div class="error-message">
            <h3>{$t('summary.errorLoading')}</h3>
            <p>{error}</p>
          </div>
        </div>
      {:else}
        {#if summary}
          <SummarySearch
            bind:searchQuery
            {totalMatches}
            {currentMatchIndex}
            disabled={!summary}
            on:search={({ detail }) => searchQuery = detail.query}
            on:clearSearch={() => searchQuery = ''}
            on:keydown={handleSearchKeydown}
            on:nextMatch={cycleToNextMatch}
            on:previousMatch={cycleToPreviousMatch}
          />
          <div class="summary-scroll-area">
            <SummaryDisplay
              {summary}
              {searchQuery}
              {currentMatchIndex}
            />
          </div>
          <div class="summary-actions-fixed">
            <SummaryActions
              {summary}
              {generating}
              {llmAvailable}
              {canRetry}
              {summaryStatus}
              {canGenerate}
              {summaryEnabledSystem}
              on:generateSummary={generateSummary}
              on:retrySummary={retryFailedSummary}
              on:regenerateWithPrompt={regenerateWithPrompt}
            />
          </div>
        {:else}
          <SummaryActions
            {summary}
            {generating}
            {llmAvailable}
            {canRetry}
            {summaryStatus}
            on:generateSummary={generateSummary}
            on:retrySummary={retryFailedSummary}
            on:regenerateWithPrompt={regenerateWithPrompt}
          />
        {/if}
      {/if}
</BaseModal>
</div>

<style>
  /* Zero out BaseModal body padding for full-bleed layout matching TranscriptModal */
  .summary-modal-wrapper :global(.modal-body) {
    padding: 0 !important;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }

  .summary-scroll-area {
    flex: 1;
    overflow-y: auto;
    padding: 1.5rem;
    min-height: 0;
    -webkit-overflow-scrolling: touch;
  }

  .summary-actions-fixed {
    flex-shrink: 0;
    border-top: 1px solid var(--border-color, #e0e0e0);
    padding: 0.75rem 1.5rem;
    padding-bottom: calc(0.75rem + env(safe-area-inset-bottom, 0px));
    background: var(--bg-primary, #ffffff);
  }

  .modal-title {
    margin: 0;
    font-size: 1.25rem;
    font-weight: 600;
    color: var(--text-primary);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .header-actions {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    flex-shrink: 0;
  }

  .copy-button-header {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    background: var(--bg-primary);
    border: 1px solid var(--border-color);
    color: var(--text-secondary);
    padding: 0.5rem 0.75rem;
    border-radius: 6px;
    cursor: pointer;
    transition: all 0.2s ease;
    font-size: 0.85rem;
  }

  .copy-button-header:hover {
    background-color: var(--hover-bg);
    color: var(--primary-color);
    border-color: var(--primary-color);
  }

  .copy-button-header.copied {
    background-color: var(--success-bg);
    border-color: var(--success-color);
    color: var(--success-color);
  }

  .copy-button-header.copied:hover {
    background-color: var(--success-bg);
    border-color: var(--success-color);
    color: var(--success-color);
  }

  .reprocess-button-header {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    padding: 0.5rem 0.75rem;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    background-color: var(--bg-primary);
    color: var(--text-primary);
    font-size: 0.8rem;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s ease;
  }

  .reprocess-button-header:hover:not(:disabled) {
    background-color: var(--hover-bg);
    border-color: var(--primary-color);
    color: var(--primary-color);
  }

  .reprocess-button-header:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .loading-container, .error-container {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: 3rem;
    gap: 1rem;
  }

  .error-container {
    flex-direction: row;
    text-align: left;
  }

  .error-icon {
    font-size: 2rem;
    flex-shrink: 0;
  }

  .error-message h3 {
    margin: 0 0 0.5rem 0;
    color: var(--error-color);
  }

  .error-message p {
    margin: 0;
    color: var(--text-secondary);
  }

  @media (max-width: 768px) {
    .modal-title {
      font-size: 1.1rem;
    }

    .header-actions {
      flex-wrap: wrap;
      gap: 0.5rem;
    }

    .copy-button-header,
    .reprocess-button-header {
      min-height: 44px;
      padding: 0.5rem 0.625rem;
      font-size: 0.8rem;
    }

    .summary-scroll-area {
      padding: 0.75rem;
    }

    .summary-actions-fixed {
      padding: 0.5rem 0.75rem;
      padding-bottom: calc(0.5rem + env(safe-area-inset-bottom, 0px));
    }
  }

</style>
