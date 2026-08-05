<script lang="ts">
  import type { MediaFileDetail } from '$lib/types/media';
  /**
   * Action buttons in the file-detail video header: view transcript, AI summary
   * (view / generating / generate), and reprocess.
   *
   * Presentational only — the page owns every piece of state and reacts to the
   * dispatched events.
   */
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import Spinner from '$components/ui/Spinner.svelte';

  export let file: MediaFileDetail | null = null;
  export let canEdit = true;
  export let llmAvailable = false;
  export let summaryGenerating = false;
  export let generatingSummary = false;
  export let reprocessing = false;

  const dispatch = createEventDispatcher();
</script>

<div class="header-buttons">
  <!-- View Full Transcript Button - LEFT of AI Summary -->
  {#if file && file.transcript_segments && file.transcript_segments.length > 0 && file.status !== 'processing'}
    <button
      class="view-transcript-btn"
      on:click={() => dispatch('viewTranscript')}
      title={$t('fileDetail.viewTranscript')}
    >
      <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor" class="transcript-icon">
        <path d="M4 2a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2V4a2 2 0 0 0-2-2H4zm0 1h8a1 1 0 0 1 1 1v8a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1z"/>
        <path d="M5 5h6v1H5V5zm0 2h6v1H5V7zm0 2h4v1H5V9z"/>
      </svg>
      {$t('fileDetail.transcript')}
    </button>
  {/if}
  <!-- Debug: Summary button state: hasSummary={!!(file?.has_summary || file?.summary_opensearch_id)}, summaryGenerating={summaryGenerating}, generatingSummary={generatingSummary}, fileStatus={file?.status} -->
  {#if file?.has_summary || file?.summary_opensearch_id}
    <button
      class="view-summary-btn"
      on:click={() => dispatch('showSummary')}
      title={$t('fileDetail.viewSummaryTooltip')}
    >
      <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor" class="ai-icon">
        <path d="M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09zM18.259 8.715L18 9.75l-.259-1.035a3.375 3.375 0 00-2.455-2.456L14.25 6l1.036-.259a3.375 3.375 0 002.455-2.456L18 2.25l.259 1.035a3.375 3.375 0 002.456 2.456L21.75 6l-1.035.259a3.375 3.375 0 00-2.456 2.456zM16.894 20.567L16.5 21.75l-.394-1.183a2.25 2.25 0 00-1.423-1.423L13.5 18.75l1.183-.394a2.25 2.25 0 001.423-1.423L16.5 15.75l.394 1.183a2.25 2.25 0 001.423 1.423L19.5 18.75l-1.183.394a2.25 2.25 0 00-1.423 1.423z"/>
      </svg>
      {$t('fileDetail.summary')}
    </button>
  {:else if summaryGenerating || generatingSummary}
    <!-- Show generating state even when no summary exists yet -->
    <button
      class="generate-summary-btn"
      disabled
      title={$t('fileDetail.aiSummaryGenerating')}
    >
      <Spinner size="small" color="white" />
      <span>{$t('fileDetail.aiSummary')}</span>
    </button>
  {:else if file?.status === 'completed' && canEdit}
    <button
      class="generate-summary-btn"
      on:click={() => dispatch('generateSummary')}
      disabled={generatingSummary || summaryGenerating || !llmAvailable}
      title={!llmAvailable ? $t('fileDetail.aiNotAvailable') :
             (generatingSummary || summaryGenerating) ? $t('fileDetail.aiSummaryGenerating') :
             $t('fileDetail.generateSummaryTooltip')}
    >
      {#if generatingSummary || summaryGenerating}
        <div class="spinner-small"></div>
        <span>{$t('fileDetail.aiSummary')}</span>
      {:else}
        <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor" class="ai-icon">
          <path d="M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09zM18.259 8.715L18 9.75l-.259-1.035a3.375 3.375 0 00-2.455-2.456L14.25 6l1.036-.259a3.375 3.375 0 002.455-2.456L18 2.25l.259 1.035a3.375 3.375 0 002.456 2.456L21.75 6l-1.035.259a3.375 3.375 0 00-2.456 2.456zM16.894 20.567L16.5 21.75l-.394-1.183a2.25 2.25 0 00-1.423-1.423L13.5 18.75l1.183-.394a2.25 2.25 0 001.423-1.423L16.5 15.75l.394 1.183a2.25 2.25 0 001.423 1.423L19.5 18.75l-1.183.394a2.25 2.25 0 00-1.423 1.423z"/>
        </svg>
        {$t('fileDetail.generateSummary')}
      {/if}
    </button>
  {/if}
  <!-- Reprocess Button (opens SelectiveReprocessModal) - editors/owners only -->
  <!-- 'failed' is not a FileStatus value (the backend enum uses 'error'); the
       dead third comparison was dropped when this prop became typed. -->
  {#if canEdit && file && (file.status === 'error' || file.status === 'completed')}
    <button
      class="reprocess-button-header"
      on:click={() => dispatch('openReprocess')}
      disabled={reprocessing}
      title={reprocessing ? $t('fileDetail.reprocessingTooltip') : $t('fileDetail.reprocessTooltip')}
    >
      <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M23 4v6h-6"></path>
        <path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"></path>
      </svg>
      {#if reprocessing}
        <Spinner size="small" />
      {/if}
    </button>
  {/if}
</div>

<style>
  .header-buttons {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex-wrap: wrap;
    justify-content: flex-end;
  }

  .view-transcript-btn {
    background-color: var(--bg-primary);
    color: var(--text-primary);
    border: 1px solid var(--border-color);
    border-radius: 8px;
    padding: 0.6rem 1rem;
    font-size: 0.9rem;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s ease;
    display: flex;
    align-items: center;
    gap: 0.5rem;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.1);
    height: 40px;
    white-space: nowrap;
  }

  .view-transcript-btn:hover {
    background-color: var(--hover-bg);
    border-color: var(--primary-color);
    box-shadow: 0 2px 6px rgba(0, 0, 0, 0.15);
  }

  .view-transcript-btn:active {
    transform: scale(0.98);
  }

  .view-transcript-btn .transcript-icon {
    flex-shrink: 0;
    opacity: 0.8;
  }

  .reprocess-button-header {
    background-color: var(--bg-primary);
    color: var(--text-primary);
    border: 1px solid var(--border-color);
    border-radius: 8px;
    padding: 0.6rem;
    cursor: pointer;
    transition: all 0.2s ease;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 0.25rem;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.1);
    width: 40px;
    height: 40px;
  }

  .reprocess-button-header:hover:not(:disabled) {
    background-color: var(--hover-bg);
    border-color: var(--primary-color);
    box-shadow: 0 2px 6px rgba(0, 0, 0, 0.15);
  }

  .reprocess-button-header:active {
    transform: scale(0.98);
  }

  .reprocess-button-header:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .view-summary-btn {
    background-color: var(--bg-primary);
    color: var(--text-primary);
    border: 1px solid var(--border-color);
    border-radius: 8px;
    padding: 0.6rem 1rem;
    font-size: 0.9rem;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s ease;
    display: flex;
    align-items: center;
    gap: 0.5rem;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.1);
    height: 40px;
    white-space: nowrap;
  }

  .view-summary-btn:hover {
    background-color: var(--hover-bg);
    border-color: var(--primary-color);
    box-shadow: 0 2px 6px rgba(0, 0, 0, 0.15);
  }

  .view-summary-btn:active {
    transform: scale(0.98);
  }

  .view-summary-btn .ai-icon {
    flex-shrink: 0;
    opacity: 0.8;
  }

  .generate-summary-btn {
    background-color: var(--primary-color, #3b82f6);
    color: white;
    border: none;
    border-radius: 6px;
    padding: 0.5rem 1rem;
    font-size: 0.9rem;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s ease;
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }

  .generate-summary-btn:hover:not(:disabled) {
    background-color: var(--primary-color-dark, #2563eb);
    transform: translateY(-1px);
  }

  .generate-summary-btn:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  @media (max-width: 768px) {
    .header-buttons {
      width: 100%;
      justify-content: flex-start;
    }

    .view-transcript-btn,
    .view-summary-btn,
    .generate-summary-btn {
      font-size: 0.8rem;
      padding: 0.4rem 0.6rem;
      height: 36px;
    }
  }

  @media (max-width: 480px) {
    .view-transcript-btn,
    .view-summary-btn,
    .generate-summary-btn {
      font-size: 0.75rem;
      padding: 0.35rem 0.5rem;
      height: 32px;
      gap: 0.25rem;
    }

    .reprocess-button-header {
      width: 32px;
      height: 32px;
      padding: 0.4rem;
    }
  }
</style>
