<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import axiosInstance from '$lib/axios';
  import { getErrorStatus } from '$lib/utils/apiError';
  import { toastStore } from '$stores/toast';
  import { t } from '$stores/locale';
  import StatusChip from './StatusChip.svelte';
  import Spinner from '../ui/Spinner.svelte';

  // State
  let running = false;
  let loading = true;
  let error = '';
  let lastRun: { timestamp: number; status: string; repaired?: number; unrepairable?: number; failed_files?: string[]; total_files?: number; duration_seconds: number; v3_missing?: number; v4_missing?: number; total_pg_speakers?: number } | null = null;
  let counts: { total_pg_speakers: number; v3_indexed: number; v3_missing: number; unrepairable?: number; no_segments?: number; orphans?: number; v4_exists: boolean; v4_indexed?: number; v4_missing: number } | null = null;
  let loadingCounts = false;

  // Progress tracking
  let processedFiles = 0;
  let totalFiles = 0;
  let repaired = 0;
  let failedFiles: string[] = [];
  let etaSeconds: number | null = null;

  interface ConsistencyProgress {
    total_files: number;
    processed_files: number;
    v3_missing: number;
    v4_missing: number;
    v4_exists: boolean;
    failed_files: string[];
    repaired: number;
    running: boolean;
  }

  interface ConsistencyComplete {
    status: string;
    repaired?: number;
    failed_files?: string[];
    total_files?: number;
    duration_seconds?: number;
    error?: string;
  }

  function handleProgress(event: CustomEvent<ConsistencyProgress>) {
    const data = event.detail;
    running = data.running;
    processedFiles = data.processed_files || 0;
    totalFiles = data.total_files || 0;
    repaired = data.repaired || 0;
    failedFiles = data.failed_files || [];
    etaSeconds = (data as unknown as Record<string, unknown>).eta_seconds as number | null;
  }

  function handleBundledStatus(event: CustomEvent<{ running: boolean; progress: ConsistencyProgress | null; last_run: typeof lastRun }>) {
    const data = event.detail;
    running = data.running || false;
    lastRun = data.last_run || null;

    if (data.progress && data.running) {
      processedFiles = data.progress.processed_files || 0;
      totalFiles = data.progress.total_files || 0;
      repaired = data.progress.repaired || 0;
      failedFiles = data.progress.failed_files || [];
    }

    loading = false;
  }

  function handleComplete(event: CustomEvent<ConsistencyComplete>) {
    const data = event.detail;
    running = false;
    processedFiles = 0;
    totalFiles = 0;
    etaSeconds = null;

    if (data.status === 'completed') {
      toastStore.success($t('settings.embeddingConsistency.repairComplete'));
      Promise.allSettled([loadStatus(), loadCounts()]);
    } else if (data.status === 'stopped') {
      toastStore.info($t('settings.embeddingConsistency.repairStopped'));
      Promise.allSettled([loadStatus(), loadCounts()]);
    } else if (data.status === 'error') {
      toastStore.error($t('settings.embeddingConsistency.repairFailed'));
    }
  }

  async function loadStatus() {
    loading = true;
    error = '';

    try {
      const { data } = await axiosInstance.get('/admin/embedding-consistency/status');
      running = data.running || false;
      lastRun = data.last_run || null;

      if (data.progress && data.running) {
        processedFiles = data.progress.processed_files || 0;
        totalFiles = data.progress.total_files || 0;
        repaired = data.progress.repaired || 0;
        failedFiles = data.progress.failed_files || [];
      }
    } catch (err) {
      if (getErrorStatus(err) === 403) {
        error = $t('settings.embeddingConsistency.adminRequired');
        return;
      }
      console.error('Failed to load embedding consistency status:', err);
      error = $t('settings.embeddingConsistency.loadFailed');
    } finally {
      loading = false;
    }
  }

  async function loadCounts() {
    loadingCounts = true;
    try {
      counts = (await axiosInstance.get('/admin/embedding-consistency/counts')).data;
    } catch (err) {
      console.error('Failed to load embedding consistency counts:', err);
      toastStore.error($t('settings.embeddingConsistency.countsFailed'));
    } finally {
      loadingCounts = false;
    }
  }

  async function startRepair() {
    running = true;
    processedFiles = 0;
    totalFiles = 0;
    repaired = 0;
    failedFiles = [];
    etaSeconds = null;

    try {
      const { data } = await axiosInstance.post('/admin/embedding-consistency/repair');
      if (data.status === 'already_running') {
        toastStore.info($t('settings.embeddingConsistency.alreadyRunning'));
        return;
      }

      toastStore.success($t('settings.embeddingConsistency.repairStarted'));
    } catch (err) {
      console.error('Failed to start embedding consistency repair:', err);
      toastStore.error($t('settings.embeddingConsistency.repairFailed'));
      running = false;
    }
  }

  async function stopRepair() {
    try {
      await axiosInstance.post('/admin/embedding-consistency/stop');
      running = false;
      toastStore.info($t('settings.embeddingConsistency.repairStopped'));
      loadStatus();
    } catch (err) {
      console.error('Failed to stop repair:', err);
      toastStore.error($t('settings.embeddingConsistency.stopFailed'));
    }
  }

  function formatTimestamp(ts: number): string {
    return new Date(ts * 1000).toLocaleString();
  }

  function formatDuration(seconds: number): string {
    if (seconds < 60) return `${Math.round(seconds)}s`;
    const m = Math.floor(seconds / 60);
    const s = Math.round(seconds % 60);
    return s > 0 ? `${m}m ${s}s` : `${m}m`;
  }

  onMount(() => {
    window.addEventListener('embedding-consistency-progress', handleProgress as EventListener);
    window.addEventListener('embedding-consistency-complete', handleComplete as EventListener);
    window.addEventListener('consistency-status-loaded', handleBundledStatus as EventListener);
    loadStatus();
  });

  onDestroy(() => {
    window.removeEventListener('embedding-consistency-progress', handleProgress as EventListener);
    window.removeEventListener('embedding-consistency-complete', handleComplete as EventListener);
    window.removeEventListener('consistency-status-loaded', handleBundledStatus as EventListener);
  });
</script>

<div class="consistency-settings">
  <div class="settings-section">
    <div class="title-row">
      <h3 class="section-title">{$t('settings.embeddingConsistency.title')}</h3>
    </div>
    <p class="section-desc">{$t('settings.embeddingConsistency.description')}</p>

    {#if error}
      <div class="error-state">
        <p>{error}</p>
      </div>
    {:else}
      <!-- Status Chips -->
      <div class="status-chips-row">
        {#if loading}
          <div class="skeleton-chip"></div>
          <div class="skeleton-chip wide"></div>
        {:else}
          <StatusChip
            label={$t('settings.embeddingConsistency.chipStatus')}
            value={running ? $t('settings.embeddingConsistency.chipRunning') : $t('settings.embeddingConsistency.chipIdle')}
            status={running ? 'blue' : 'green'}
          />
          {#if lastRun}
            <StatusChip
              label={$t('settings.embeddingConsistency.chipLastRun')}
              value={formatTimestamp(lastRun.timestamp)}
              status="neutral"
            />
            <StatusChip
              label={$t('settings.embeddingConsistency.chipResult')}
              value={lastRun.status === 'healthy' ? $t('settings.embeddingConsistency.healthy') : $t('settings.embeddingConsistency.repairedCount', { count: lastRun.repaired || 0 })}
              status={lastRun.status === 'healthy' ? 'green' : 'yellow'}
            />
          {/if}
        {/if}
      </div>

      <!-- Counts Section -->
      {#if counts && !running}
        <div class="counts-section">
          <div class="counts-row">
            <span class="count-label">{$t('settings.embeddingConsistency.totalSpeakers')}</span>
            <span class="count-value">{counts.total_pg_speakers}</span>
          </div>
          <div class="counts-row">
            <span class="count-label">{$t('settings.embeddingConsistency.v3Indexed')}</span>
            <span class="count-value">{counts.v3_indexed}</span>
          </div>
          <div class="counts-row" class:has-missing={counts.v3_missing > 0}>
            <span class="count-label">{$t('settings.embeddingConsistency.v3Missing')}</span>
            <span class="count-value">{counts.v3_missing}</span>
          </div>
          {#if counts.unrepairable && counts.unrepairable > 0}
            <div class="counts-row unrepairable">
              <span class="count-label">{$t('settings.embeddingConsistency.unrepairable')}</span>
              <span class="count-value">{counts.unrepairable}</span>
            </div>
          {/if}
          {#if counts.no_segments && counts.no_segments > 0}
            <div class="counts-row info-row">
              <span class="count-label">{$t('settings.embeddingConsistency.noSegments')}</span>
              <span class="count-value">{counts.no_segments}</span>
            </div>
          {/if}
          {#if counts.orphans && counts.orphans > 0}
            <div class="counts-row has-missing">
              <span class="count-label">{$t('settings.embeddingConsistency.orphans')}</span>
              <span class="count-value">{counts.orphans}</span>
            </div>
          {/if}
          {#if counts.v4_exists}
            <div class="counts-row">
              <span class="count-label">{$t('settings.embeddingConsistency.v4Indexed')}</span>
              <span class="count-value">{counts.v4_indexed ?? 0}</span>
            </div>
            <div class="counts-row" class:has-missing={counts.v4_missing > 0}>
              <span class="count-label">{$t('settings.embeddingConsistency.v4Missing')}</span>
              <span class="count-value">{counts.v4_missing}</span>
            </div>
          {/if}
        </div>
      {/if}

      <!-- Action Box -->
      <div class="action-box" class:running>
        <div class="action-icon">
          <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>
            <path d="m9 12 2 2 4-4"/>
          </svg>
        </div>
        <div class="action-content">
          <h4>{$t('settings.embeddingConsistency.actionTitle')}</h4>
          <p>{$t('settings.embeddingConsistency.actionDescription')}</p>
        </div>
        <div class="action-buttons">
          {#if running}
            <button class="btn btn-danger" on:click={stopRepair}>
              {$t('settings.embeddingConsistency.stop')}
            </button>
          {:else}
            <button class="btn btn-secondary" on:click={loadCounts} disabled={loadingCounts || loading}>
              {#if loadingCounts}
                <Spinner size="small" color="var(--text-color)" />
              {/if}
              {$t('settings.embeddingConsistency.check')}
            </button>
            <button class="btn btn-primary" on:click={startRepair} disabled={loading}>
              {$t('settings.embeddingConsistency.repair')}
            </button>
          {/if}
        </div>
      </div>

      <!-- Progress Section -->
      {#if running && totalFiles > 0}
        <div class="progress-section">
          <div class="progress-header">
            <span class="progress-text">
              {$t('settings.embeddingConsistency.repairing', { processed: processedFiles, total: totalFiles })}
            </span>
            <span class="progress-percent">
              {processedFiles}/{totalFiles}
              ({Math.round((processedFiles / totalFiles) * 100)}%)
              {#if etaSeconds != null && etaSeconds > 0}
                {$t('settings.embeddingConsistency.eta', { eta: formatDuration(etaSeconds) })}
              {/if}
            </span>
          </div>
          <div class="progress-bar-container">
            <div
              class="progress-bar-fill"
              style="width: {(processedFiles / totalFiles) * 100}%"
            ></div>
          </div>
          {#if repaired > 0}
            <div class="progress-stats">
              <span>{$t('settings.embeddingConsistency.repairedCount', { count: repaired })}</span>
              {#if failedFiles.length > 0}
                <span class="failed-count">{$t('settings.embeddingConsistency.failedCount', { count: failedFiles.length })}</span>
              {/if}
            </div>
          {/if}
        </div>
      {/if}

      <!-- Last Run Results -->
      {#if lastRun && !running}
        <div class="results-section">
          <h4 class="results-title">{$t('settings.embeddingConsistency.lastResults')}</h4>
          <p class="results-meta">
            {$t('settings.embeddingConsistency.completedIn', { duration: formatDuration(lastRun.duration_seconds) })}
            {#if lastRun.status === 'healthy'}
              &mdash; {$t('settings.embeddingConsistency.allSpeakersIndexed')}
            {:else if lastRun.status === 'repaired'}
              &mdash; {$t('settings.embeddingConsistency.repairSummary', {
                repaired: lastRun.repaired || 0,
                failed: (lastRun.failed_files || []).length
              })}
            {/if}
            {#if lastRun.unrepairable && lastRun.unrepairable > 0}
              <br />{$t('settings.embeddingConsistency.unrepairableCount', { count: lastRun.unrepairable })}
            {/if}
          </p>
        </div>
      {/if}
    {/if}
  </div>
</div>

<style>
  .consistency-settings {
    padding: 0.5rem 0;
  }

  .error-state {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: 1.5rem;
    gap: 0.5rem;
    color: var(--error-color);
  }

  .skeleton-chip {
    height: 28px;
    width: 90px;
    border-radius: 14px;
    background: var(--border-color, #e5e7eb);
    animation: skeleton-pulse 1.5s ease-in-out infinite;
  }

  .skeleton-chip.wide {
    width: 130px;
  }

  .settings-section {
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: 8px;
    padding: 1.25rem;
  }

  .title-row {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }

  .section-title {
    font-size: 0.95rem;
    font-weight: 600;
    margin: 0;
    color: var(--text-color);
  }

  .section-desc {
    font-size: 0.8rem;
    color: var(--text-muted);
    margin: 0.25rem 0 1rem 0;
  }

  .status-chips-row {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
    margin-bottom: 1rem;
  }

  .counts-section {
    margin-bottom: 1rem;
    padding: 0.75rem;
    background: var(--background-color);
    border-radius: 6px;
    border: 1px solid var(--border-color);
    font-size: 0.8rem;
  }

  .counts-row {
    display: flex;
    justify-content: space-between;
    padding: 0.25rem 0.5rem;
  }

  .counts-row.has-missing {
    color: var(--warning-color, #f59e0b);
    font-weight: 600;
  }

  .counts-row.unrepairable,
  .counts-row.info-row {
    color: var(--text-muted);
    font-style: italic;
  }

  .count-label {
    color: var(--text-secondary);
  }

  .counts-row.has-missing .count-label {
    color: inherit;
  }

  .count-value {
    font-weight: 600;
    font-family: monospace;
  }

  .action-box {
    display: flex;
    gap: 1rem;
    padding: 1rem;
    border-radius: 8px;
    background: rgba(34, 197, 94, 0.08);
    border: 1px solid rgba(34, 197, 94, 0.3);
    margin-bottom: 1rem;
  }

  .action-box.running {
    background: rgba(var(--primary-color-rgb), 0.08);
    border-color: rgba(var(--primary-color-rgb), 0.3);
  }

  .action-icon {
    flex-shrink: 0;
    width: 40px;
    height: 40px;
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    background: rgba(34, 197, 94, 0.15);
    color: #22c55e;
  }

  .action-box.running .action-icon {
    background: rgba(var(--primary-color-rgb), 0.15);
    color: var(--primary-color);
  }

  .action-content {
    flex: 1;
  }

  .action-content h4 {
    margin: 0 0 0.25rem 0;
    font-size: 0.9rem;
    color: var(--text-color);
  }

  .action-content p {
    margin: 0;
    font-size: 0.8rem;
    color: var(--text-secondary);
  }

  .action-buttons {
    display: flex;
    align-items: flex-start;
    gap: 0.5rem;
  }

  .progress-section {
    margin-top: 1rem;
    padding: 1rem;
    background: var(--background-color);
    border-radius: 6px;
    border: 1px solid var(--border-color);
  }

  .progress-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 0.5rem;
  }

  .progress-text {
    font-size: 0.8rem;
    color: var(--text-secondary);
  }

  .progress-percent {
    font-size: 0.8rem;
    font-weight: 600;
    color: var(--primary-color);
  }

  .progress-bar-container {
    height: 8px;
    background: var(--border-color);
    border-radius: 4px;
    overflow: hidden;
  }

  .progress-bar-fill {
    height: 100%;
    background: #3b82f6;
    border-radius: 4px;
    transition: width 0.3s ease;
  }

  .progress-stats {
    display: flex;
    gap: 1rem;
    margin-top: 0.5rem;
    font-size: 0.75rem;
    color: var(--text-secondary);
  }

  .failed-count {
    color: var(--error-color);
  }

  .results-section {
    margin-top: 1rem;
    padding: 1rem;
    background: var(--background-color);
    border-radius: 6px;
    border: 1px solid var(--border-color);
  }

  .results-title {
    margin: 0 0 0.25rem 0;
    font-size: 0.85rem;
    font-weight: 600;
    color: var(--text-color);
  }

  .results-meta {
    font-size: 0.75rem;
    color: var(--text-muted);
    margin: 0;
  }

  @media (max-width: 768px) {
    .action-box {
      flex-direction: column;
    }

    .action-buttons {
      width: 100%;
      flex-wrap: wrap;
    }

    .action-buttons .btn {
      flex: 1;
      min-height: 44px;
    }
  }
</style>
