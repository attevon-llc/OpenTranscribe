<script lang="ts">
  import { formatEta } from '$lib/utils/formatting';
  import Spinner from '$components/ui/Spinner.svelte';
  import ProgressBar from '$components/ui/ProgressBar.svelte';
  import { t } from '$stores/locale';

  interface ReindexProgress {
    progress: number;
    indexed_files: number;
    total_files: number;
    eta_seconds?: number | null;
    message?: string;
  }

  export let reindexProgress: ReindexProgress | null;
  export let isStopping: boolean;
  export let onstop: () => void;

</script>

  <div class="reindex-live-progress">
    <div class="reindex-header">
      <span class="reindex-label">
        <Spinner size="small" />
        {isStopping
          ? ($t('search.stoppingReindex'))
          : ($t('search.reindexingInProgress'))}
      </span>
      {#if reindexProgress && reindexProgress.total_files > 0}
        <span class="reindex-count">
          {reindexProgress.indexed_files} / {reindexProgress.total_files}
        </span>
      {/if}
    </div>
    {#if reindexProgress && reindexProgress.total_files > 0}
      <div class="progress-container">
        <div class="progress-bar reindexing">
          <div class="progress-fill" style="width: {Math.min(Math.round(reindexProgress.progress * 100), 100)}%"></div>
        </div>
        <span class="progress-text">
          <bdi dir="ltr">{Math.min(Math.round(reindexProgress.progress * 100), 100)}%</bdi>
        </span>
      </div>
      <div class="reindex-details">
        {#if reindexProgress.message}
          <span class="reindex-message">{reindexProgress.message}</span>
        {/if}
        {#if formatEta(reindexProgress.eta_seconds)}
          <span class="reindex-eta">{formatEta(reindexProgress.eta_seconds)} {$t('upload.remaining')}</span>
        {/if}
      </div>
    {:else}
      <div class="progress-container indeterminate-row">
        <div class="progress-bar-wrapper">
          <ProgressBar percent={null} />
        </div>
        <span class="progress-text">{$t('search.reindexStarting')}</span>
      </div>
    {/if}
    <div class="reindex-actions">
      <button
        class="btn btn-danger-outline btn-sm"
        on:click={onstop}
        disabled={isStopping}
      >
        {isStopping ? ($t('search.stoppingReindex')) : ($t('search.stopReindex'))}
      </button>
    </div>
  </div>

<style>
  /* Live reindex progress styles */
  .reindex-live-progress {
    margin-bottom: var(--space-3);
    padding: var(--space-3);
    background: var(--background-color);
    border-radius: var(--radius-md);
    border: 1px solid var(--border-color);
  }

  .reindex-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: var(--space-2);
  }

  .reindex-label {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    font-size: 0.8125rem;
    font-weight: 500;
    color: var(--primary-on-surface);
  }

  .reindex-count {
    font-size: 0.75rem;
    color: var(--text-secondary);
    font-weight: 500;
  }

  .progress-bar.reindexing {
    background: var(--primary-light);
  }

  .progress-bar.reindexing .progress-fill {
    background: var(--primary-color);
    animation: pulse 1.5s ease-in-out infinite;
  }

  @keyframes pulse {
    0%, 100% {
      opacity: 1;
    }
    50% {
      opacity: 0.7;
    }
  }

  .reindex-details {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-top: var(--space-1);
    font-size: 0.75rem;
    color: var(--text-secondary);
  }

  .reindex-message {
    flex: 1;
  }

  .reindex-eta {
    white-space: nowrap;
    font-weight: 500;
    color: var(--primary-on-surface);
  }

  .reindex-actions {
    display: flex;
    justify-content: flex-end;
    margin-top: var(--space-2);
  }

  .btn-danger-outline {
    background: transparent;
    color: var(--danger-color);
    border: 1px solid var(--danger-color);
  }

  .btn-danger-outline:hover:not(:disabled) {
    background: var(--danger-bg);
  }

  .btn-danger-outline:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .btn-sm {
    padding: var(--space-1) var(--space-3);
    font-size: 0.75rem;
  }

  .progress-container {
    display: flex;
    align-items: center;
    gap: var(--space-2-5);
    margin-bottom: var(--space-2);
  }

  .progress-bar {
    flex: 1;
    height: 6px;
    background: var(--border-color);
    border-radius: 3px;
    overflow: hidden;
  }

  .progress-fill {
    height: 100%;
    background: var(--primary-color);
    border-radius: 3px;
    transition: width var(--duration-slow);
  }

  .progress-text {
    font-size: 0.75rem;
    color: var(--text-secondary);
    white-space: nowrap;
  }

  .indeterminate-row {
    display: flex;
    align-items: center;
    gap: var(--space-2-5);
    margin-bottom: var(--space-2);
  }

  .indeterminate-row .progress-bar-wrapper {
    flex: 1;
  }
</style>
