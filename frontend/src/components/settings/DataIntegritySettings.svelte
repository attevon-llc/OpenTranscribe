<script lang="ts">
  import { isRecord } from '$lib/utils/typeGuards';
  import { formatCompactDuration, formatDateTime } from '$lib/utils/formatting';
  import { onMount, onDestroy } from 'svelte';
  import axiosInstance from '$lib/axios';
  import { getErrorStatus } from '$lib/utils/apiError';
  import { toastStore } from '$stores/toast';
  import { t, locale } from '$stores/locale';
  import StatusChip from './StatusChip.svelte';
  import Spinner from '$components/ui/Spinner.svelte';
  import OrphanedDataPanel from './OrphanedDataPanel.svelte';

  // State
  let running = false;
  let loading = true;
  let error = '';
  /** Per-index result keyed by index name, plus `summary` and `error` entries. */
  type IntegrityResults = Record<string, unknown>;

  let lastRun: { timestamp: number; results: IntegrityResults; duration_seconds: number } | null = null;

  // Index overview
  interface IndexEntry {
    name: string;
    label: string;
    exists: boolean;
    total?: number;
    breakdown?: Record<string, number>;
    error?: string;
  }
  interface IndexOverview {
    error?: string;
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
    scope?: string;
    results?: IntegrityResults;
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
    // The storage sweep reports on the same channel; OrphanedDataPanel owns that one.
    if (data.scope === 'storage') return;
    running = false;
    currentIndex = '';
    processedIndices = 0;
    totalIndices = 0;

    if (data.status === 'completed') {
      toastStore.success($t('settings.dataIntegrity.checkComplete'));
      void loadStatus();
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

  function summaryCount(results: IntegrityResults | undefined, field: string): number {
    const summary = results?.summary;
    if (!isRecord(summary)) return 0;
    const value = summary[field];
    return typeof value === 'number' ? value : 0;
  }

  function getTotalOrphans(results: IntegrityResults | undefined): number {
    return summaryCount(results, 'total_orphans_found');
  }

  function getTotalDeleted(results: IntegrityResults | undefined): number {
    return summaryCount(results, 'total_deleted');
  }

  function docCount(stats: Record<string, unknown>, field: string): number {
    const value = stats[field];
    return typeof value === 'number' ? value : 0;
  }

  function getIndexResults(
    results: IntegrityResults | undefined
  ): Array<{ name: string; total: number; orphaned: number; deleted: number }> {
    if (!results) return [];
    return Object.entries(results)
      .filter(([key]) => key !== 'summary' && key !== 'error')
      .flatMap(([name, stats]) =>
        isRecord(stats)
          ? [
              {
                name,
                total: docCount(stats, 'total_docs'),
                orphaned: docCount(stats, 'orphaned_docs'),
                deleted: docCount(stats, 'deleted_docs'),
              },
            ]
          : []
      );
  }

  onMount(() => {
    void loadStatus();
    window.addEventListener('data-integrity-progress', handleProgress as EventListener);
    window.addEventListener('data-integrity-complete', handleComplete as EventListener);
  });

  onDestroy(() => {
    window.removeEventListener('data-integrity-progress', handleProgress as EventListener);
    window.removeEventListener('data-integrity-complete', handleComplete as EventListener);
  });
</script>

<div class="integrity-settings ops-panel">
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
            value={formatDateTime(lastRun.timestamp * 1000, '', $locale)}
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
          {#if indexOverview.error}
            <p class="index-error" data-testid="index-overview-error">
              {$t('settings.dataIntegrity.overviewUnavailable')}
            </p>
          {/if}
          {#if indexOverview.pg_stats}
            <div class="pg-stats">
              <span>{$t('settings.dataIntegrity.pgFiles', { active: indexOverview.pg_stats.active_files, completed: indexOverview.pg_stats.completed_files })}</span>
              <span class="sep">&bull;</span>
              <span>{$t('settings.dataIntegrity.pgSpeakers', { count: indexOverview.pg_stats.speakers })}</span>
            </div>
          {/if}
          <div class="overview-grid">
            {#each indexOverview.indices as idx (idx.name)}
              <div class="index-card" class:missing={!idx.exists && !idx.error}>
                <div class="index-header">
                  <span class="index-label">{idx.label}</span>
                  <span class="index-name">{idx.name}</span>
                </div>
                {#if idx.error}
                  <div class="index-body empty failed" data-testid="index-error-{idx.name}">
                    {$t('settings.dataIntegrity.indexUnreadable', { name: idx.name })}
                  </div>
                {:else if !idx.exists}
                  <div class="index-body empty">
                    {$t('settings.dataIntegrity.indexNotCreated')}
                  </div>
                {:else if idx.breakdown && Object.keys(idx.breakdown).length > 0}
                  <div class="index-body">
                    {#each Object.entries(idx.breakdown) as [key, count], i (key)}
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

      <OrphanedDataPanel />

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
            {$t('settings.dataIntegrity.completedIn', { duration: formatCompactDuration(lastRun.duration_seconds) })}
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
            {#each getIndexResults(lastRun.results) as row (row.name)}
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
  .loading-state,
  .error-state {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: var(--space-12);
    gap: var(--space-4);
    color: var(--text-secondary);
  }

  .error-state {
    color: var(--error-text);
  }

  .section-title {
    font-size: var(--font-size-base);
    font-weight: var(--font-weight-semibold);
    margin: 0;
    color: var(--text-color);
  }

  .section-desc {
    font-size: var(--font-size-sm);
    color: var(--text-muted);
    margin: var(--space-1) 0 var(--space-4) 0;
  }

  .overview-section {
    margin-bottom: var(--space-4);
  }

  .overview-title {
    font-size: var(--font-size-md);
    font-weight: var(--font-weight-semibold);
    margin: 0 0 var(--space-2) 0;
    color: var(--text-color);
  }

  .pg-stats {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    margin-bottom: var(--space-2);
    display: flex;
    gap: var(--space-2);
  }

  .pg-stats .sep {
    opacity: 0.4;
  }

  .overview-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
    gap: var(--space-2);
  }

  .index-card {
    background: var(--background-color);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-md);
    overflow: hidden;
  }

  .index-card.missing {
    opacity: 0.5;
  }

  .index-header {
    padding: var(--space-2) var(--space-2-5);
    border-bottom: 1px solid var(--border-color);
    display: flex;
    flex-direction: column;
    gap: var(--space-0-5);
  }

  .index-label {
    font-size: var(--font-size-xs);
    font-weight: var(--font-weight-semibold);
    color: var(--text-color);
  }

  .index-name {
    font-size: var(--font-size-2xs);
    font-family: monospace;
    color: var(--text-muted);
  }

  .index-body {
    padding: var(--space-1-5) var(--space-2-5);
    font-size: var(--font-size-xs);
  }

  .index-body.empty {
    color: var(--text-muted);
    font-style: italic;
    padding: var(--space-2) var(--space-2-5);
  }

  .index-body.failed,
  .index-error {
    color: var(--error-text);
  }

  .breakdown-row {
    display: flex;
    justify-content: space-between;
    padding: var(--space-0-5) 0;
    color: var(--text-secondary);
  }

  .breakdown-row.muted {
    color: var(--text-muted);
    font-size: var(--font-size-xs);
  }

  .breakdown-val {
    font-weight: var(--font-weight-semibold);
    font-family: monospace;
  }

  .breakdown-total {
    display: flex;
    justify-content: space-between;
    padding: var(--space-1) 0 var(--space-0-5);
    margin-top: var(--space-0-5);
    border-top: 1px solid var(--border-color);
    font-weight: var(--font-weight-semibold);
    color: var(--text-color);
    font-size: var(--font-size-xs);
  }

  .breakdown-total.solo {
    border-top: none;
    margin-top: 0;
    padding-top: 0;
  }

  .action-box {
    display: flex;
    gap: var(--space-4);
    padding: var(--space-4);
    border-radius: var(--radius-lg);
    background: rgba(var(--success-color-rgb), 0.08);
    border: 1px solid rgba(var(--success-color-rgb), 0.3);
    margin-bottom: var(--space-4);
  }

  .action-box.running {
    background: rgba(var(--accent-rgb), 0.08);
    border-color: rgba(var(--accent-rgb), 0.3);
  }

  .action-icon {
    flex-shrink: 0;
    width: 40px;
    height: 40px;
    border-radius: var(--radius-round);
    display: flex;
    align-items: center;
    justify-content: center;
    background: rgba(var(--success-color-rgb), 0.15);
    color: var(--success-text);
  }

  .action-box.running .action-icon {
    background: rgba(var(--accent-rgb), 0.15);
    color: var(--primary-on-surface);
  }

  .action-content h4 {
    margin: 0 0 var(--space-1) 0;
    font-size: var(--font-size-body);
    color: var(--text-color);
  }

  .action-content p {
    margin: 0;
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
  }

  .action-buttons {
    display: flex;
    align-items: flex-start;
  }

  .progress-section {
    margin-top: var(--space-4);
    padding: var(--space-4);
    background: var(--background-color);
    border-radius: var(--radius-md);
    border: 1px solid var(--border-color);
  }

  .progress-text {
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
  }

  .progress-percent {
    font-size: var(--font-size-sm);
    font-weight: var(--font-weight-semibold);
    color: var(--primary-on-surface);
  }

  .results-title {
    margin: 0 0 var(--space-1) 0;
    font-size: var(--font-size-md);
    font-weight: var(--font-weight-semibold);
    color: var(--text-color);
  }

  .results-meta {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    margin: 0 0 var(--space-3) 0;
  }

  .results-table {
    font-size: var(--font-size-xs);
  }

  .results-row {
    display: grid;
    grid-template-columns: 1fr 80px 80px 80px;
    gap: var(--space-2);
    padding: var(--space-1-5) var(--space-2);
    border-bottom: 1px solid var(--border-color);
  }

  .results-row.header {
    font-weight: var(--font-weight-semibold);
    color: var(--text-secondary);
    border-bottom: 2px solid var(--border-color);
  }

  .results-row.has-orphans {
    background: rgba(var(--warning-color-rgb), 0.08);
  }

  .col-index {
    font-family: monospace;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .col-total,
  .col-orphans,
  .col-cleaned {
    text-align: end;
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
      font-size: var(--font-size-xs);
    }

    .col-index {
      word-break: break-all;
    }
  }
</style>
