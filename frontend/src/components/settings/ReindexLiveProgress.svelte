<script lang="ts">
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

  function formatEta(seconds: number | null | undefined): string {
    if (seconds == null || seconds <= 0) return '';
    if (seconds < 60) return `${Math.round(seconds)}s`;
    const m = Math.floor(seconds / 60);
    const s = Math.round(seconds % 60);
    if (m < 60) return s > 0 ? `${m}m ${s}s` : `${m}m`;
    const h = Math.floor(m / 60);
    return `${h}h ${m % 60}m`;
  }
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
          {Math.min(Math.round(reindexProgress.progress * 100), 100)}%
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
    margin-bottom: 0.75rem;
    padding: 0.75rem;
    background: var(--background-color);
    border-radius: 6px;
    border: 1px solid var(--border-color);
  }

  .reindex-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 0.5rem;
  }

  .reindex-label {
    display: flex;
    align-items: center;
    gap: 0.5rem;
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
    background: var(--primary-light, rgba(var(--primary-color-rgb), 0.2));
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
    margin-top: 0.25rem;
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
    margin-top: 0.5rem;
  }

  .btn-danger-outline {
    background: transparent;
    color: var(--danger-color, #dc2626);
    border: 1px solid var(--danger-color, #dc2626);
  }

  .btn-danger-outline:hover:not(:disabled) {
    background: var(--danger-bg, rgba(220, 38, 38, 0.08));
  }

  .btn-danger-outline:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .btn-sm {
    padding: 0.25rem 0.75rem;
    font-size: 0.75rem;
  }

  .progress-container {
    display: flex;
    align-items: center;
    gap: 0.625rem;
    margin-bottom: 0.5rem;
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
    transition: width 0.3s;
  }

  .progress-text {
    font-size: 0.75rem;
    color: var(--text-secondary);
    white-space: nowrap;
  }

  .indeterminate-row {
    display: flex;
    align-items: center;
    gap: 0.625rem;
    margin-bottom: 0.5rem;
  }

  .indeterminate-row .progress-bar-wrapper {
    flex: 1;
  }
</style>
