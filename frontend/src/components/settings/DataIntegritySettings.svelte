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
  let lastRun: { timestamp: number; results: any; duration_seconds: number } | null = null;

  // Index overview
  interface IndexEntry {
    name: string;
    label: string;
    exists: boolean;
    total?: number;
    breakdown?: Record<string, number>;
  }
  interface IndexOverview {
    indices: IndexEntry[];
    pg_stats?: { active_files: number; completed_files: number; speakers: number };
  }
  let indexOverview: IndexOverview | null = null;

  // Map breakdown keys to i18n keys
  const breakdownLabels: Record<string, string> = {
    speakers: 'settings.dataIntegrity.docSpeakers',
    profiles: 'settings.dataIntegrity.docProfiles',
    clusters: 'settings.dataIntegrity.docClusters',
    metadata: 'settings.dataIntegrity.docMetadata',
    chunks: 'settings.dataIntegrity.docChunks',
  };

  // Progress tracking
  let currentIndex = '';
  let processedIndices = 0;
  let totalIndices = 0;

  interface IntegrityProgress {
    current_index: string;
    processed_indices: number;
    total_indices: number;
    progress: number;
    running: boolean;
  }

  interface IntegrityComplete {
    status: string;
    results?: any;
    duration_seconds?: number;
    error?: string;
  }

  function handleProgress(event: CustomEvent<IntegrityProgress>) {
    const data = event.detail;
    running = data.running;
    currentIndex = data.current_index || '';
    processedIndices = data.processed_indices || 0;
    totalIndices = data.total_indices || 0;
  }

  function handleComplete(event: CustomEvent<IntegrityComplete>) {
    const data = event.detail;
    running = false;
    currentIndex = '';
    processedIndices = 0;
    totalIndices = 0;

    if (data.status === 'completed') {
      toastStore.success($t('settings.dataIntegrity.checkComplete'));
      loadStatus();
    } else if (data.status === 'error') {
      toastStore.error($t('settings.dataIntegrity.checkFailed'));
    }
  }

  async function loadStatus() {
    loading = true;
    error = '';

    try {
      const { data } = await axiosInstance.get('/admin/data-integrity/status');
      running = data.running || false;
      lastRun = data.last_run || null;
      indexOverview = data.index_overview || null;
    } catch (err) {
      if (getErrorStatus(err) === 403) {
        error = $t('settings.dataIntegrity.adminRequired');
        return;
      }
      console.error('Failed to load data integrity status:', err);
      error = $t('settings.dataIntegrity.loadFailed');
    } finally {
      loading = false;
    }
  }

  async function startCheck() {
    running = true;
    processedIndices = 0;
    totalIndices = 0;

    try {
      const { data } = await axiosInstance.post('/admin/data-integrity');
      if (data.status === 'already_running') {
        toastStore.info($t('settings.dataIntegrity.alreadyRunning'));
        return;
      }

      toastStore.success($t('settings.dataIntegrity.checkStarted'));
    } catch (err) {
      console.error('Failed to start data integrity check:', err);
      toastStore.error($t('settings.dataIntegrity.checkFailed'));
      running = false;
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

  function getTotalOrphans(results: any): number {
    return results?.summary?.total_orphans_found ?? 0;
  }

  function getTotalDeleted(results: any): number {
    return results?.summary?.total_deleted ?? 0;
  }

  function getIndexResults(results: any): Array<{ name: string; total: number; orphaned: number; deleted: number }> {
    if (!results) return [];
    return Object.entries(results)
      .filter(([key]) => key !== 'summary' && key !== 'error')
      .map(([name, stats]: [string, any]) => ({
        name,
        total: stats.total_docs || 0,
        orphaned: stats.orphaned_docs || 0,
        deleted: stats.deleted_docs || 0,
      }));
  }

  onMount(() => {
    loadStatus();
    window.addEventListener('data-integrity-progress', handleProgress as EventListener);
    window.addEventListener('data-integrity-complete', handleComplete as EventListener);
  });

  onDestroy(() => {
    window.removeEventListener('data-integrity-progress', handleProgress as EventListener);
    window.removeEventListener('data-integrity-complete', handleComplete as EventListener);
  });
</script>

<div class="integrity-settings">
  {#if loading}
    <div class="loading-state">
      <Spinner size="medium" />
      <p>{$t('settings.dataIntegrity.loading')}</p>
    </div>
  {:else if error}
    <div class="error-state">
      <p>{error}</p>
    </div>
  {:else}
    <div class="settings-section">
      <div class="title-row">
        <h3 class="section-title">{$t('settings.dataIntegrity.title')}</h3>
      </div>
      <p class="section-desc">{$t('settings.dataIntegrity.description')}</p>

      <!-- Status Chips -->
      <div class="status-chips-row">
        <StatusChip
          label={$t('settings.dataIntegrity.chipStatus')}
          value={running ? $t('settings.dataIntegrity.chipRunning') : $t('settings.dataIntegrity.chipIdle')}
          status={running ? 'blue' : 'green'}
        />
        {#if lastRun}
          <StatusChip
            label={$t('settings.dataIntegrity.chipLastRun')}
            value={formatTimestamp(lastRun.timestamp)}
            status="neutral"
          />
          <StatusChip
            label={$t('settings.dataIntegrity.chipOrphans')}
            value={String(getTotalOrphans(lastRun.results))}
            status={getTotalOrphans(lastRun.results) > 0 ? 'yellow' : 'green'}
          />
        {/if}
      </div>

      <!-- Index Overview -->
      {#if indexOverview && !loading}
        <div class="overview-section">
          <h4 class="overview-title">{$t('settings.dataIntegrity.indexOverview')}</h4>
          {#if indexOverview.pg_stats}
            <div class="pg-stats">
              <span>{$t('settings.dataIntegrity.pgFiles', { active: indexOverview.pg_stats.active_files, completed: indexOverview.pg_stats.completed_files })}</span>
              <span class="sep">&bull;</span>
              <span>{$t('settings.dataIntegrity.pgSpeakers', { count: indexOverview.pg_stats.speakers })}</span>
            </div>
          {/if}
          <div class="overview-grid">
            {#each indexOverview.indices as idx}
              <div class="index-card" class:missing={!idx.exists}>
                <div class="index-header">
                  <span class="index-label">{idx.label}</span>
                  <span class="index-name">{idx.name}</span>
                </div>
                {#if !idx.exists}
                  <div class="index-body empty">
                    {$t('settings.dataIntegrity.indexNotCreated')}
                  </div>
                {:else if idx.breakdown && Object.keys(idx.breakdown).length > 0}
                  <div class="index-body">
                    {#each Object.entries(idx.breakdown) as [key, count], i}
                      <div class="breakdown-row" class:muted={i > 0}>
                        <span>{$t(breakdownLabels[key] || key)}</span>
                        <span class="breakdown-val">{count}</span>
                      </div>
                    {/each}
                    <div class="breakdown-total">
                      <span>{$t('settings.dataIntegrity.docTotal')}</span>
                      <span class="breakdown-val">{idx.total}</span>
                    </div>
                  </div>
                {:else}
                  <div class="index-body">
                    <div class="breakdown-total solo">
                      <span>{$t('settings.dataIntegrity.docTotal')}</span>
                      <span class="breakdown-val">{idx.total ?? 0}</span>
                    </div>
                  </div>
                {/if}
              </div>
            {/each}
          </div>
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
          <h4>{$t('settings.dataIntegrity.actionTitle')}</h4>
          <p>{$t('settings.dataIntegrity.actionDescription')}</p>
        </div>
        <div class="action-buttons">
          {#if running}
            <button class="btn btn-primary" disabled>
              <Spinner size="small" color="white" />
              {$t('settings.dataIntegrity.running')}
            </button>
          {:else}
            <button class="btn btn-primary" on:click={startCheck}>
              {$t('settings.dataIntegrity.runCheck')}
            </button>
          {/if}
        </div>
      </div>

      <!-- Progress Section -->
      {#if running && totalIndices > 0}
        <div class="progress-section">
          <div class="progress-header">
            <span class="progress-text">
              {$t('settings.dataIntegrity.scanning', { index: currentIndex })}
            </span>
            <span class="progress-percent">
              {processedIndices}/{totalIndices}
              ({Math.round((processedIndices / totalIndices) * 100)}%)
            </span>
          </div>
          <div class="progress-bar-container">
            <div
              class="progress-bar-fill"
              style="width: {(processedIndices / totalIndices) * 100}%"
            ></div>
          </div>
        </div>
      {/if}

      <!-- Last Run Results -->
      {#if lastRun?.results && !running}
        <div class="results-section">
          <h4 class="results-title">{$t('settings.dataIntegrity.lastResults')}</h4>
          <p class="results-meta">
            {$t('settings.dataIntegrity.completedIn', { duration: formatDuration(lastRun.duration_seconds) })}
            &mdash;
            {$t('settings.dataIntegrity.orphansSummary', {
              found: getTotalOrphans(lastRun.results),
              cleaned: getTotalDeleted(lastRun.results)
            })}
          </p>

          <div class="results-table">
            <div class="results-row header">
              <span class="col-index">{$t('settings.dataIntegrity.colIndex')}</span>
              <span class="col-total">{$t('settings.dataIntegrity.colTotal')}</span>
              <span class="col-orphans">{$t('settings.dataIntegrity.colOrphans')}</span>
              <span class="col-cleaned">{$t('settings.dataIntegrity.colCleaned')}</span>
            </div>
            {#each getIndexResults(lastRun.results) as row}
              <div class="results-row" class:has-orphans={row.orphaned > 0}>
                <span class="col-index">{row.name}</span>
                <span class="col-total">{row.total}</span>
                <span class="col-orphans">{row.orphaned}</span>
                <span class="col-cleaned">{row.deleted}</span>
              </div>
            {/each}
          </div>
        </div>
      {/if}
    </div>
  {/if}
</div>

<style>
  .integrity-settings {
    padding: 0.5rem 0;
  }

  .loading-state,
  .error-state {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: 3rem;
    gap: 1rem;
    color: var(--text-secondary);
  }

  .error-state {
    color: var(--error-color);
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

  .overview-section {
    margin-bottom: 1rem;
  }

  .overview-title {
    font-size: 0.85rem;
    font-weight: 600;
    margin: 0 0 0.5rem 0;
    color: var(--text-color);
  }

  .pg-stats {
    font-size: 0.75rem;
    color: var(--text-muted);
    margin-bottom: 0.5rem;
    display: flex;
    gap: 0.5rem;
  }

  .pg-stats .sep {
    opacity: 0.4;
  }

  .overview-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
    gap: 0.5rem;
  }

  .index-card {
    background: var(--background-color);
    border: 1px solid var(--border-color);
    border-radius: 6px;
    overflow: hidden;
  }

  .index-card.missing {
    opacity: 0.5;
  }

  .index-header {
    padding: 0.5rem 0.625rem;
    border-bottom: 1px solid var(--border-color);
    display: flex;
    flex-direction: column;
    gap: 0.125rem;
  }

  .index-label {
    font-size: 0.75rem;
    font-weight: 600;
    color: var(--text-color);
  }

  .index-name {
    font-size: 0.65rem;
    font-family: monospace;
    color: var(--text-muted);
  }

  .index-body {
    padding: 0.375rem 0.625rem;
    font-size: 0.75rem;
  }

  .index-body.empty {
    color: var(--text-muted);
    font-style: italic;
    padding: 0.5rem 0.625rem;
  }

  .breakdown-row {
    display: flex;
    justify-content: space-between;
    padding: 0.125rem 0;
    color: var(--text-secondary);
  }

  .breakdown-row.muted {
    color: var(--text-muted);
    font-size: 0.7rem;
  }

  .breakdown-val {
    font-weight: 600;
    font-family: monospace;
  }

  .breakdown-total {
    display: flex;
    justify-content: space-between;
    padding: 0.25rem 0 0.125rem;
    margin-top: 0.125rem;
    border-top: 1px solid var(--border-color);
    font-weight: 600;
    color: var(--text-color);
    font-size: 0.75rem;
  }

  .breakdown-total.solo {
    border-top: none;
    margin-top: 0;
    padding-top: 0;
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
    background: rgba(59, 130, 246, 0.08);
    border-color: rgba(59, 130, 246, 0.3);
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
    background: rgba(59, 130, 246, 0.15);
    color: var(--primary-on-surface);
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
    color: var(--primary-on-surface);
  }

  .progress-bar-container {
    height: 8px;
    background: var(--border-color);
    border-radius: 4px;
    overflow: hidden;
  }

  .progress-bar-fill {
    height: 100%;
    background: var(--primary-color);
    border-radius: 4px;
    transition: width 0.3s ease;
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
    margin: 0 0 0.75rem 0;
  }

  .results-table {
    font-size: 0.75rem;
  }

  .results-row {
    display: grid;
    grid-template-columns: 1fr 80px 80px 80px;
    gap: 0.5rem;
    padding: 0.375rem 0.5rem;
    border-bottom: 1px solid var(--border-color);
  }

  .results-row.header {
    font-weight: 600;
    color: var(--text-secondary);
    border-bottom: 2px solid var(--border-color);
  }

  .results-row.has-orphans {
    background: rgba(251, 191, 36, 0.08);
  }

  .col-index {
    font-family: monospace;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .col-total,
  .col-orphans,
  .col-cleaned {
    text-align: right;
  }

  @media (max-width: 768px) {
    .action-box {
      flex-direction: column;
    }

    .action-buttons {
      width: 100%;
    }

    .action-buttons .btn {
      width: 100%;
      min-height: 44px;
    }

    .overview-grid {
      grid-template-columns: 1fr;
    }

    .results-row {
      grid-template-columns: 1fr 60px 60px 60px;
      font-size: 0.7rem;
    }

    .col-index {
      word-break: break-all;
    }
  }
</style>
