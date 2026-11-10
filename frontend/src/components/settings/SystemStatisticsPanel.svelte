<script context="module" lang="ts">
  import { formatDateTime, formatElapsed } from '$lib/utils/formatting';
  export interface SearchIndexStatus {
    indexed_files: number;
    total_files: number;
    pending_files: number;
    in_progress: boolean;
    current_model: string;
  }

  export type SearchHealthStatus = Record<string, { status: string; doc_count: number }>;
</script>

<script lang="ts">
  import type { SystemStats } from '$lib/settings/adminLoaders';
  import MenuIcon from '$components/ui/MenuIcon.svelte';
  import { taskStatusIcon } from '$lib/ui/menuIcons';
  import { createEventDispatcher } from 'svelte';
  import { t, locale } from '$stores/locale';
  import Spinner from '$components/ui/Spinner.svelte';
  import InfoTip from '$components/settings/transcription/InfoTip.svelte';
  import Badge from '$components/ui/Badge.svelte';
  import { badgeVariantForStatus } from '$lib/fileStatus/statusVariant';
  import EmptyState from '$components/ui/EmptyState.svelte';

  export let stats: SystemStats;
  export let statsLoading = false;
  export let statsRefreshing = false;
  export let searchIndexStatus: SearchIndexStatus | null = null;
  export let searchHealthStatus: SearchHealthStatus | null = null;

  const dispatch = createEventDispatcher<{ refresh: void; openDetails: string }>();

  // Backend sends usage as strings like "42.5%"; unparsable or missing reads as 0.
  function meterValue(raw: unknown): number {
    const value = parseFloat(String(raw));
    return Number.isFinite(value) ? Math.min(100, Math.max(0, value)) : 0;
  }

  let currentGpuIndex = 0;
  $: activeGpu = stats.system?.gpus?.[currentGpuIndex] ?? stats.system?.gpus?.[0];
  $: gpuCount = stats.system?.gpus?.length ?? 1;

  // Clamp index in case GPU count decreased (e.g. on a live stats update)
  $: if (currentGpuIndex >= gpuCount) currentGpuIndex = 0;

  // Read-only: the cap is a deployment setting latched once per process (issue #1270).
  $: rerankerThreads = stats.system?.chat_reranker_threads as
    | { threads: number | null; source: string }
    | undefined;
  $: rerankerThreadsLabel = !rerankerThreads
    ? ''
    : rerankerThreads.threads === null
      ? $t('settings.statistics.rerankerThreadsTorchEnv')
      : $t(
          rerankerThreads.source === 'configured'
            ? 'settings.statistics.rerankerThreadsSet'
            : 'settings.statistics.rerankerThreadsAuto',
          { count: rerankerThreads.threads }
        );

  function refreshStats() {
    dispatch('refresh');
  }

  function openProcessingDetails(section: string) {
    dispatch('openDetails', section);
  }

  /** A click on an info icon inside a clickable card explains; it must not also open the details. */
  // Enter/Space on the card itself only: a key pressed on a focused tooltip inside it is not ours.
  function openOnKey(event: KeyboardEvent, section: string) {
    if (event.target !== event.currentTarget) return;
    if (event.key !== 'Enter' && event.key !== ' ') return;
    event.preventDefault();
    openProcessingDetails(section);
  }

  function openDetailsUnlessTip(event: MouseEvent, section: string) {
    if ((event.target as Element | null)?.closest('.tip')) return;
    openProcessingDetails(section);
  }

  // Narrow screens show a status glyph (named for screen readers) instead of the word.
  const isMobileView = typeof window !== 'undefined' && window.innerWidth < 768;

  function formatStatus(status: string): string {
    const statusMap: Record<string, string> = {
      'completed': $t('common.completed'),
      'processing': $t('common.processing'),
      'pending': $t('common.pending'),
      'error': $t('common.error'),
      'failed': $t('fileStatus.failed'),
      'in_progress': $t('fileStatus.inProgress'),
      'success': $t('common.success'),
    };
    return statusMap[status.toLowerCase()] || status.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
  }
</script>

<div class="content-section">
  <div class="section-header-row">
    <div>
      <h3 class="section-title">{$t('settings.statistics.title')}</h3>
      <p class="section-description">{$t('settings.statistics.description')}</p>
    </div>
    <button
      type="button"
      class="btn btn-secondary btn-refresh"
      on:click={refreshStats}
      disabled={statsLoading || statsRefreshing}
    >
      <svg class="refresh-icon" class:spinning={statsRefreshing} xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M21.5 2v6h-6M2.5 22v-6h6M2 11.5a10 10 0 0 1 18.8-4.3M22 12.5a10 10 0 0 1-18.8 4.2"/>
      </svg>
      {$t('settings.statistics.refresh')}
    </button>
  </div>

  {#if statsLoading}
    <div class="loading-state">
      <Spinner size="large" />
      <p>{$t('settings.statistics.loadingMessage')}</p>
    </div>
  {:else}
    {#if stats.system?.device_mode === 'cpu'}
      <!-- CPU-only mode advisory: shown when the backend reports
           device_mode=cpu (either FORCE_CPU_MODE=true or no GPU
           detected on host). Distinguishes the two reasons so
           the user can act on it. -->
      <div class="cpu-mode-banner" role="alert">
        <div class="cpu-mode-banner-header">
          <span class="cpu-mode-icon" aria-hidden="true"><MenuIcon name="cpu" size="lg" /></span>
          <strong>{$t('settings.statistics.cpuMode.title')}</strong>
        </div>
        <div class="cpu-mode-banner-reason">
          {stats.system?.force_cpu_mode
            ? $t('settings.statistics.cpuMode.reasonForced')
            : $t('settings.statistics.cpuMode.reasonAuto')}
        </div>
        <ul class="cpu-mode-banner-list">
          <li>
            <strong>{$t('settings.statistics.cpuMode.modelLabel')}:</strong>
            <code>{stats.system?.whisper_model || '—'}</code>
            {#if ['tiny', 'tiny.en', 'base', 'base.en', 'small', 'small.en'].includes(stats.system?.whisper_model ?? '')}
              — {$t('settings.statistics.cpuMode.modelHintGood')}
            {:else}
              — {$t('settings.statistics.cpuMode.modelHintHeavy')}
            {/if}
          </li>
        </ul>
        <div class="cpu-mode-banner-footer">
          {$t('settings.statistics.cpuMode.howToFix')}
        </div>
      </div>
    {/if}
    <div class="stats-grid" class:stats-refreshing={statsRefreshing}>
      <!-- User Stats -->
      <div class="stat-card">
        <h4>{$t('settings.statistics.users')}</h4>
        <div class="stat-value">{stats.users?.total || 0}</div>
        <div class="stat-detail">{$t('settings.statistics.newUsers')}: {stats.users?.new || 0}</div>
      </div>

      <!-- Media Stats -->
      <div class="stat-card">
        <h4>{$t('settings.statistics.mediaFiles')}</h4>
        <div class="stat-value">{stats.files?.total || 0}</div>
        <div class="stat-detail">{$t('settings.statistics.new')}: {stats.files?.new || 0}</div>
        <div class="stat-detail">{$t('settings.statistics.segments')}: {stats.transcripts?.total_segments_is_estimate ? '≈' : ''}{stats.files?.segments || 0}</div>
      </div>

      <!-- Task Stats -->
      <div class="stat-card">
        <h4>{$t('settings.statistics.tasks')}</h4>
        <div class="stat-detail">{$t('settings.statistics.pending')}: {stats.tasks?.pending || 0}</div>
        <div class="stat-detail">{$t('settings.statistics.running')}: {stats.tasks?.running || 0}</div>
        <div class="stat-detail">{$t('settings.statistics.completed')}: {stats.tasks?.completed || 0}</div>
        <div class="stat-detail">{$t('settings.statistics.failed')}: {stats.tasks?.failed || 0}</div>
        <div class="stat-detail">{$t('settings.statistics.successRate')}: <bdi dir="ltr">{stats.tasks?.success_rate || 0}%</bdi></div>
      </div>

      <!-- Performance Stats -->
      <div class="stat-card stat-card-clickable" role="button" tabindex="0" on:click={() => openProcessingDetails('performance')} on:keydown={(event) => openOnKey(event, 'performance')}>
        <h4>{$t('settings.statistics.performance')}</h4>
        <div class="stat-detail">{$t('settings.statistics.avgProcessTime')}: {formatElapsed(stats.tasks?.avg_processing_time || 0)}</div>
        <div class="stat-detail">{$t('settings.statistics.fileTimingAvg')}: {formatElapsed(stats.file_timing?.avg_secs || 0)}</div>
        <div class="stat-detail">{$t('settings.statistics.fileTimingMin')}: {formatElapsed(stats.file_timing?.min_secs || 0)}</div>
        <div class="stat-detail">{$t('settings.statistics.fileTimingMax')}: {formatElapsed(stats.file_timing?.max_secs || 0)}</div>
        <div class="stat-detail">{$t('settings.statistics.speakers')}: {stats.speakers?.total_is_estimate ? '≈' : ''}{stats.speakers?.total || 0}</div>
        <div class="stat-detail stat-detail-hint">{$t('settings.statistics.viewDetails')}</div>
      </div>

      <!-- Throughput & ETA -->
      <div class="stat-card stat-card-clickable" role="button" tabindex="0" on:click={() => openProcessingDetails('throughput')} on:keydown={(event) => openOnKey(event, 'throughput')}>
        <h4>{$t('settings.statistics.throughput')}</h4>
        <div class="stat-value">{stats.throughput?.rate_1h || 0} <span class="stat-unit">{$t('settings.statistics.filesPerHour')}</span></div>
        <div class="stat-detail">{$t('settings.statistics.avgRate3h')}: {stats.throughput?.rate_3h || 0} {$t('settings.statistics.filesPerHour')}</div>
        {#if stats.eta?.remaining > 0}
          <div class="stat-detail">{$t('settings.statistics.remaining')}: {stats.eta.remaining} {$t('settings.statistics.filesUnit')}</div>
          {#if stats.eta.hours_remaining !== null}
            <div class="stat-detail">{$t('settings.statistics.hoursRemaining')}: {stats.eta.hours_remaining}{$t('settings.statistics.hoursUnit')}</div>
          {/if}
        {:else}
          <div class="stat-detail">{$t('settings.statistics.noActiveProcessing')}</div>
        {/if}
        <div class="stat-detail stat-detail-hint">{$t('settings.statistics.viewDetails')}</div>
      </div>

      <!-- Queue Depths -->
      <div class="stat-card stat-card-clickable" role="button" tabindex="0" on:click={() => openProcessingDetails('queues')} on:keydown={(event) => openOnKey(event, 'queues')}>
        <h4>{$t('settings.statistics.queueDepths')}</h4>
        <div class="stat-value">{stats.queues_available === false ? '—' : stats.queues?.total || 0} <span class="stat-unit">{$t('settings.statistics.queueTotal')}</span></div>
        {#if (stats.queues?.total ?? 0) > 0}
          <div class="queue-bars">
            {#each [
              { key: 'gpu', label: $t('settings.statistics.queueGpu') },
              { key: 'download', label: $t('settings.statistics.queueDownload') },
              { key: 'nlp', label: $t('settings.statistics.queueNlp') },
              { key: 'embedding', label: $t('settings.statistics.queueEmbedding') },
              { key: 'cpu', label: $t('settings.statistics.queueCpu') }
            ] as queue (queue.key)}
              {#if (stats.queues?.[queue.key] ?? 0) > 0}
                <div class="stat-detail">{queue.label}: {stats.queues?.[queue.key]}</div>
              {/if}
            {/each}
          </div>
        {/if}
        <div class="stat-detail stat-detail-hint">{$t('settings.statistics.viewDetails')}</div>
      </div>

      <!-- AI Models -->
      <div class="stat-card model-card stat-card-clickable" role="button" tabindex="0" on:click={(event) => openDetailsUnlessTip(event, 'models')} on:keydown={(event) => openOnKey(event, 'models')}>
        <h4>{$t('settings.statistics.aiModels')}</h4>
        {#if stats.models}
          <div class="model-info">
            <div class="model-item">
              <span class="model-label">{$t('settings.statistics.whisperModel')}:</span>
              <span class="model-value">{stats.models.whisper?.name || $t('common.notAvailable')}</span>
            </div>
            <div class="model-item">
              <span class="model-label">{$t('settings.statistics.diarization')}:</span>
              <span class="model-value">
                {stats.models.diarization?.description || stats.models.diarization?.name || $t('common.notAvailable')}
              </span>
              {#if stats.models.diarization?.using_fallback}
                <span class="model-fallback-badge" title={$t('settings.statistics.diarizationFallbackWarning', {
                  configured: stats.models.diarization?.configured_description || stats.models.diarization?.configured_backend,
                  effective: stats.models.diarization?.description || stats.models.diarization?.effective_backend
                })}>
                  <MenuIcon name="warning" size="xs" />
                  {$t('settings.statistics.diarizationFallbackBadge')}
                </span>
              {/if}
            </div>
            {#if stats.models.search_embedding}
              <div class="model-item">
                <span class="model-label">{$t('settings.statistics.searchModel')}:</span>
                <span class="model-value">{stats.models.search_embedding.name}</span>
              </div>
            {/if}
            {#if stats.models.llm}
              <div class="model-item">
                <span class="model-label">{$t('settings.statistics.llmModel')}:</span>
                <span class="model-value">{stats.models.llm.name}</span>
              </div>
            {/if}
            {#if rerankerThreads}
              <div class="model-item" data-testid="reranker-threads">
                <span class="model-label">
                  {$t('settings.statistics.rerankerThreads')}:
                  <InfoTip inline label={$t('settings.statistics.rerankerThreads')}>
                    {$t('settings.statistics.rerankerThreadsHelp')}
                  </InfoTip>
                </span>
                <span class="model-value">{rerankerThreadsLabel}</span>
              </div>
            {/if}
          </div>
        {:else}
          <div class="stat-detail">{$t('settings.statistics.modelNotAvailable')}</div>
        {/if}
        <div class="stat-detail stat-detail-hint">{$t('settings.statistics.viewDetails')}</div>
      </div>

      <!-- Search Index Status -->
      {#if searchIndexStatus}
        <div class="stat-card">
          <h4>{$t('settings.statistics.searchIndex')}</h4>
          <div class="stat-value">
            {searchIndexStatus.indexed_files}/{searchIndexStatus.total_files}
            <span class="stat-unit">{$t('settings.statistics.indexed')}</span>
          </div>
          <div class="stat-detail">{$t('settings.statistics.model')}: {searchIndexStatus.current_model}</div>
          {#if searchIndexStatus.pending_files > 0}
            <div class="stat-detail stat-detail-warning">{searchIndexStatus.pending_files} {$t('settings.statistics.pendingReindex')}</div>
          {/if}
          {#if searchIndexStatus.in_progress}
            <div class="stat-detail stat-detail-active">{$t('settings.statistics.reindexingActive')}</div>
          {/if}
          {#if searchHealthStatus}
            <div class="search-health-row">
              {#each Object.entries(searchHealthStatus) as [name, info] (name)}
                <span class="search-health-dot" class:healthy={info.status === 'green'} class:error={info.status === 'red'} title="{name}: {info.status === 'green' ? $t('settings.search.indexGreen') : $t('settings.search.indexRed')}"></span>
              {/each}
              <span class="search-health-label">
                {#if Object.values(searchHealthStatus).every(i => i.status === 'green')}
                  {$t('settings.statistics.allHealthy')}
                {:else}
                  {$t('settings.statistics.needsRepair')}
                {/if}
              </span>
            </div>
          {/if}
        </div>
      {/if}

      <!-- System Resources: CPU & Memory -->
      <div class="stat-card stat-card-stacked">
        <div class="stat-section">
          <h4>{$t('settings.statistics.cpuUsage')}</h4>
          <div class="stat-value">{stats.system?.cpu?.total_percent || '0%'}</div>
          <div
            class="meter"
            role="meter"
            aria-label={$t('settings.statistics.cpuUsage')}
            aria-valuemin="0"
            aria-valuemax="100"
            aria-valuenow={meterValue(stats.system?.cpu?.total_percent)}
          >
            <div class="meter-fill" style="width: {meterValue(stats.system?.cpu?.total_percent)}%"></div>
          </div>
        </div>

        <div class="stat-section">
          <h4>{$t('settings.statistics.memoryUsage')}</h4>
          <div class="stat-value">{stats.system?.memory?.percent || '0%'}</div>
          <div class="stat-detail-compact">
            <bdi dir="ltr">{stats.system?.memory?.used || $t('common.unknown')} / {stats.system?.memory?.total || $t('common.unknown')}</bdi>
          </div>
          <div
            class="meter"
            role="meter"
            aria-label={$t('settings.statistics.memoryUsage')}
            aria-valuemin="0"
            aria-valuemax="100"
            aria-valuenow={meterValue(stats.system?.memory?.percent)}
          >
            <div class="meter-fill" style="width: {meterValue(stats.system?.memory?.percent)}%"></div>
          </div>
        </div>
      </div>

      <div class="stat-card stat-card-with-bar">
        <div class="stat-card-content">
          <h4>{$t('settings.statistics.diskUsage')}</h4>
          <div class="stat-value">{stats.system?.disk?.percent || '0%'}</div>
          <div class="stat-detail">
            <span>{$t('settings.statistics.total')}: <bdi dir="ltr">{stats.system?.disk?.total || $t('common.unknown')}</bdi></span>
            <span>{$t('settings.statistics.used')}: <bdi dir="ltr">{stats.system?.disk?.used || $t('common.unknown')}</bdi></span>
            <span>{$t('settings.statistics.free')}: <bdi dir="ltr">{stats.system?.disk?.free || $t('common.unknown')}</bdi></span>
          </div>
        </div>
        <div
          class="meter"
          role="meter"
          aria-label={$t('settings.statistics.diskUsage')}
          aria-valuemin="0"
          aria-valuemax="100"
          aria-valuenow={meterValue(stats.system?.disk?.percent)}
        >
          <div class="meter-fill" style="width: {meterValue(stats.system?.disk?.percent)}%"></div>
        </div>
      </div>

      <!-- GPU VRAM -->
      <div class="stat-card stat-card-with-bar">
        {#if activeGpu?.available}
          <div class="stat-card-content">
            <h4 class="gpu-card-header">
              <span>{$t('settings.statistics.gpuVram')}</span>
              {#if gpuCount > 1}
                <div class="gpu-stepper">
                  <button class="gpu-step-btn" on:click={() => currentGpuIndex = (currentGpuIndex - 1 + gpuCount) % gpuCount} aria-label={$t('settings.statistics.previousGpu')}><MenuIcon name="chevron-left" size="xs" /></button>
                  <span class="gpu-step-label">GPU {currentGpuIndex + 1}/{gpuCount}</span>
                  <button class="gpu-step-btn" on:click={() => currentGpuIndex = (currentGpuIndex + 1) % gpuCount} aria-label={$t('settings.statistics.nextGpu')}><MenuIcon name="chevron-right" size="xs" /></button>
                </div>
              {/if}
            </h4>
            <div class="stat-value">{activeGpu.memory_percent || '0%'}</div>
            <div class="stat-detail">
              <span>{$t('settings.statistics.gpu')}: <bdi dir="ltr">{activeGpu.name || $t('common.unknown')}</bdi></span>
              <span>{$t('settings.statistics.total')}: <bdi dir="ltr">{activeGpu.memory_total || $t('common.unknown')}</bdi></span>
              <span>{$t('settings.statistics.used')}: <bdi dir="ltr">{activeGpu.memory_used || $t('common.unknown')}</bdi></span>
              <span>{$t('settings.statistics.free')}: <bdi dir="ltr">{activeGpu.memory_free || $t('common.unknown')}</bdi></span>
              {#if activeGpu.utilization_percent && activeGpu.utilization_percent !== 'N/A'}
                <span>{$t('settings.statistics.gpuUtilization')}: <bdi dir="ltr">{activeGpu.utilization_percent}</bdi></span>
              {/if}
              {#if activeGpu.temperature_celsius !== null && activeGpu.temperature_celsius !== undefined}
                <span>{$t('settings.statistics.gpuTemperature')}: <bdi dir="ltr">{activeGpu.temperature_celsius}°C</bdi></span>
              {/if}
            </div>
          </div>
          <div
            class="meter"
            role="meter"
            aria-label={$t('settings.statistics.gpuVram')}
            aria-valuemin="0"
            aria-valuemax="100"
            aria-valuenow={meterValue(activeGpu.memory_percent)}
          >
            <div class="meter-fill" style="width: {meterValue(activeGpu.memory_percent)}%"></div>
          </div>
        {:else if activeGpu?.loading}
          <div class="stat-card-content">
            <h4>{$t('settings.statistics.gpuVram')}</h4>
            <div class="stat-value loading-text">{$t('common.loading')}</div>
            <div class="stat-detail">{$t('settings.statistics.gpuStatsLoading')}</div>
          </div>
        {:else}
          <div class="stat-card-content">
            <h4>{$t('settings.statistics.gpuVram')}</h4>
            <div class="stat-value">{$t('common.notAvailable')}</div>
            <div class="stat-detail">{activeGpu?.name || $t('settings.statistics.noGpu')}</div>
          </div>
        {/if}
      </div>
    </div>

    <!-- Recent Tasks Table -->
    {#if stats.tasks?.recent && stats.tasks.recent.length > 0}
      <div class="recent-tasks" class:stats-refreshing={statsRefreshing}>
        <h4>{$t('settings.statistics.recentTasks')}</h4>
        <div class="table-container">
          <table class="data-table">
            <thead>
              <tr>
                <th>{$t('settings.statistics.taskId')}</th>
                <th>{$t('settings.statistics.type')}</th>
                <th>{$t('settings.statistics.status')}</th>
                <th>{$t('settings.statistics.created')}</th>
                <th>{$t('settings.statistics.elapsed')}</th>
              </tr>
            </thead>
            <tbody>
              {#each stats.tasks.recent as task (task.id)}
                <tr>
                  <td>{task.id.substring(0, 8)}...</td>
                  <td>{task.type}</td>
                  <td>
                    <Badge variant={badgeVariantForStatus(task.status)} size="md" class="status-badge">{#if isMobileView && taskStatusIcon(task.status)}<span class="status-glyph" role="img" aria-label={formatStatus(task.status)} title={formatStatus(task.status)}><MenuIcon name={taskStatusIcon(task.status) ?? 'status-queued'} size="xs" /></span>{:else}{formatStatus(task.status)}{/if}</Badge>
                  </td>
                  <td>{formatDateTime(task.created_at, '', $locale)}</td>
                  <td>{formatElapsed(task.elapsed)}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      </div>
    {:else}
      <div class="recent-tasks" class:stats-refreshing={statsRefreshing}>
        <h4>{$t('settings.statistics.recentTasks')}</h4>
        <EmptyState padding="var(--space-4)" description={$t('settings.statistics.noRecentTasks')} />
      </div>
    {/if}
  {/if}
</div>

<style>
  .cpu-mode-banner {
    background: rgba(var(--warning-color-rgb), 0.08);
    border: 1px solid var(--warning-color);
    border-left: 4px solid var(--warning-color);
    border-radius: var(--radius-md);
    padding: var(--space-3-5) 18px;
    margin-bottom: var(--space-5);
    color: var(--text-color);
  }

  .cpu-mode-banner-header {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    font-size: var(--font-size-base);
    margin-bottom: var(--space-1);
  }

  .cpu-mode-icon {
    display: inline-flex;
    align-items: center;
  }

  .cpu-mode-banner-reason {
    font-size: 0.85rem;
    color: var(--text-secondary);
    margin-bottom: var(--space-2-5);
  }

  .cpu-mode-banner-list {
    margin: 0 0 var(--space-2-5) 0;
    padding-left: 22px;
    font-size: 0.9rem;
    line-height: 1.55;
  }

  .cpu-mode-banner-list code {
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: 3px;
    padding: 1px 5px;
    font-size: 0.85em;
  }

  .cpu-mode-banner-footer {
    font-size: 0.82rem;
    color: var(--text-secondary);
    border-top: 1px solid var(--border-color);
    padding-top: var(--space-2);
    margin-top: var(--space-1);
  }

  .section-title {
    font-size: 1.125rem;
    font-weight: var(--font-weight-semibold);
    margin: 0 0 var(--space-1) 0;
    color: var(--text-color);
  }

  .section-description {
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
    margin: 0 0 var(--space-5) 0;
  }

  .btn-refresh {
    display: inline-flex;
    align-items: center;
    gap: var(--space-1-5);
  }

  .refresh-icon {
    flex-shrink: 0;
    transition: transform var(--duration-slow) ease;
  }

  .refresh-icon.spinning {
    animation: spin 1s linear infinite;
  }

  .stats-refreshing {
    opacity: 0.45;
    pointer-events: none;
    transition: opacity var(--duration-slow) ease;
  }

  .section-header-row {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: var(--space-4);
    margin-bottom: var(--space-4);
    padding-right: var(--space-8);
  }

  .section-header-row .section-description {
    margin-bottom: 0;
  }

  .loading-state {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: var(--space-8);
    color: var(--text-secondary);
  }

  .loading-state p {
    margin: 0;
    font-size: var(--font-size-sm);
  }

  .stats-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
    gap: var(--space-4);
    margin-bottom: var(--space-6);
  }

  .stat-card {
    background-color: var(--background-color);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-lg);
    padding: var(--space-4);
  }

  .stat-card-clickable {
    cursor: pointer;
    transition: border-color var(--duration-base), box-shadow var(--duration-base);
  }

  .stat-card-clickable:hover {
    border-color: var(--primary-color);
    box-shadow: 0 2px 8px rgba(var(--primary-color-rgb), 0.1);
  }

  .stat-card-with-bar {
    display: flex;
    flex-direction: column;
  }

  .stat-card-stacked {
    display: flex;
    flex-direction: column;
    gap: var(--space-4);
  }

  .stat-section {
    display: flex;
    flex-direction: column;
  }

  .stat-section h4 {
    font-size: var(--font-size-xs);
    font-weight: var(--font-weight-medium);
    color: var(--text-secondary);
    margin: 0 0 var(--space-2) 0;
  }

  .stat-value {
    font-size: var(--font-size-2xl);
    font-weight: var(--font-weight-semibold);
    color: var(--text-color);
    margin-bottom: var(--space-1-5);
  }

  .stat-section .stat-value {
    font-size: var(--font-size-2xl);
    font-weight: var(--font-weight-semibold);
    color: var(--text-color);
    margin-bottom: var(--space-1-5);
  }

  .stat-detail-compact {
    font-size: var(--font-size-2xs);
    color: var(--text-secondary);
    margin-bottom: var(--space-1-5);
  }

  .stat-card-content {
    flex: 1;
    display: flex;
    flex-direction: column;
    margin-bottom: var(--space-3);
  }

  .stat-card h4 {
    font-size: var(--font-size-xs);
    font-weight: var(--font-weight-medium);
    color: var(--text-secondary);
    margin: 0 0 var(--space-2) 0;
  }

  .loading-text {
    opacity: 0.6;
    animation: pulse 1.5s ease-in-out infinite;
  }

  @keyframes pulse {
    0%, 100% { opacity: 0.6; }
    50% { opacity: 1; }
  }

  .stat-detail {
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
    margin-bottom: var(--space-0-5);
  }

  .stat-detail span {
    display: block;
    margin-bottom: var(--space-0-5);
  }

  .stat-detail-hint {
    margin-top: var(--space-2);
    font-size: var(--font-size-2xs);
    opacity: 0.6;
    font-style: italic;
  }

  .stat-unit {
    font-size: var(--font-size-xs);
    font-weight: var(--font-weight-regular);
    color: var(--text-secondary);
  }

  .stat-detail-warning {
    color: var(--warning-text);
  }

  :global([data-theme='dark']) .stat-detail-warning {
    color: #fbbf24;
  }

  .stat-detail-active {
    color: var(--primary-on-surface);
    font-weight: var(--font-weight-medium);
  }

  .search-health-row {
    display: flex;
    align-items: center;
    gap: var(--space-1-5);
    margin-top: var(--space-1-5);
  }

  .search-health-dot {
    width: 6px;
    height: 6px;
    border-radius: var(--radius-round);
    background: var(--text-secondary);
  }

  .search-health-dot.healthy {
    background: #22c55e;
  }

  .search-health-dot.error {
    background: #ef4444;
  }

  .search-health-label {
    font-size: var(--font-size-2xs);
    color: var(--text-secondary);
  }

  .gpu-card-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
  }

  .gpu-stepper {
    display: flex;
    align-items: center;
    gap: 0.2rem;
    margin-left: auto;
  }

  .gpu-step-btn {
    display: inline-flex;
    align-items: center;
    background: none;
    border: 1px solid var(--border-color);
    border-radius: 3px;
    color: var(--text-secondary);
    cursor: pointer;
    font-size: var(--font-size-md);
    line-height: 1;
    padding: 0 0.3rem;
    transition: background-color var(--duration-fast), color var(--duration-fast);
  }

  .gpu-step-btn:hover {
    background-color: var(--primary-light);
    color: var(--primary-on-surface);
    border-color: var(--primary-color);
  }

  .gpu-step-label {
    font-size: 0.6rem;
    font-weight: var(--font-weight-semibold);
    color: var(--text-secondary);
    letter-spacing: 0.03em;
    white-space: nowrap;
  }

  .meter {
    width: 100%;
    height: 6px;
    background-color: var(--color-bg-tertiary);
    border-radius: var(--radius-pill);
    overflow: hidden;
  }

  .meter-fill {
    height: 100%;
    background-color: var(--primary-color);
    border-radius: var(--radius-pill);
    transition: width var(--duration-slow) ease;
  }

  .recent-tasks {
    margin-top: var(--space-6);
  }

  .recent-tasks h4 {
    font-size: var(--font-size-body);
    font-weight: var(--font-weight-semibold);
    color: var(--text-color);
    margin: 0 0 var(--space-3) 0;
  }

  .table-container {
    overflow-x: auto;
    border: 1px solid var(--border-color);
    border-radius: var(--radius-lg);
  }

  .data-table {
    width: 100%;
    border-collapse: collapse;
    font-size: var(--font-size-sm);
  }

  .data-table thead {
    background-color: var(--background-color);
  }

  .data-table th {
    padding: var(--space-2) var(--space-3);
    text-align: left;
    font-size: var(--font-size-2xs);
    font-weight: var(--font-weight-semibold);
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--text-secondary);
    border-bottom: 1px solid var(--border-color);
  }

  .data-table td {
    padding: var(--space-2-5) var(--space-3);
    border-bottom: 1px solid var(--border-color);
    color: var(--text-color);
  }

  .data-table tbody tr:last-child td {
    border-bottom: none;
  }

  .data-table tbody tr:hover {
    background-color: var(--background-color);
  }

  /* AI Models Card Styles */
  .model-card {
    grid-column: span 1;
  }

  .model-info {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
  }

  .model-item {
    display: flex;
    flex-direction: column;
    gap: var(--space-0-5);
  }

  .model-label {
    font-size: var(--font-size-xs);
    font-weight: var(--font-weight-medium);
    color: var(--text-secondary);
  }

  .model-value {
    font-size: var(--font-size-sm);
    font-weight: var(--font-weight-medium);
    color: var(--text-color);
    font-family: 'Courier New', Courier, monospace;
  }

  .model-fallback-badge {
    display: inline-flex;
    align-items: center;
    gap: var(--space-1);
    align-self: flex-start;
    margin-top: var(--space-0-5);
    font-size: var(--font-size-2xs);
    font-weight: var(--font-weight-semibold);
    color: var(--warning-text);
    background: rgba(var(--warning-color-rgb), 0.12);
    border: 1px solid var(--warning-color);
    border-radius: var(--radius-xl);
    padding: var(--space-0-5) var(--space-2);
    cursor: help;
  }

  :global([data-theme='dark']) .model-fallback-badge {
    color: #fbbf24;
    background: rgba(251, 191, 36, 0.12);
    border-color: #fbbf24;
  }

  /* Responsive Design */
  @media (max-width: 768px) {
    .stats-grid {
      grid-template-columns: 1fr;
    }

    .section-header-row {
      flex-direction: column;
      align-items: flex-start;
      padding-right: 0;
      gap: var(--space-2);
    }

    .section-header-row .btn-refresh {
      width: 100%;
      justify-content: center;
      min-height: 44px;
    }

    .stat-card {
      padding: var(--space-3);
    }

    .stat-value {
      font-size: var(--font-size-xl);
    }

    .model-value {
      word-break: break-all;
    }

    .data-table th,
    .data-table td {
      padding: 0.4rem var(--space-2);
      font-size: var(--font-size-xs);
    }
  }
</style>
