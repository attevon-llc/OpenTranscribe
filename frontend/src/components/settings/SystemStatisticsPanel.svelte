<script context="module" lang="ts">
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
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import Spinner from '$components/ui/Spinner.svelte';

  export let stats: any;
  export let statsLoading = false;
  export let statsRefreshing = false;
  export let searchIndexStatus: SearchIndexStatus | null = null;
  export let searchHealthStatus: SearchHealthStatus | null = null;

  const dispatch = createEventDispatcher<{ refresh: void; openDetails: string }>();

  let currentGpuIndex = 0;
  $: activeGpu = stats.system?.gpus?.[currentGpuIndex] ?? stats.system?.gpus?.[0];
  $: gpuCount = stats.system?.gpus?.length ?? 1;

  // Clamp index in case GPU count decreased (e.g. on a live stats update)
  $: if (currentGpuIndex >= gpuCount) currentGpuIndex = 0;

  function refreshStats() {
    dispatch('refresh');
  }

  function openProcessingDetails(section: string) {
    dispatch('openDetails', section);
  }

  // Helper function for formatting time
  function formatTime(seconds: number): string {
    if (!seconds) return '0s';
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const secs = Math.floor(seconds % 60);
    let result = '';
    if (hours > 0) result += `${hours}h `;
    if (minutes > 0 || hours > 0) result += `${minutes}m `;
    result += `${secs}s`;
    return result;
  }

  // Helper function for formatting status text
  // Uses compact symbols on small screens
  const isMobileView = typeof window !== 'undefined' && window.innerWidth < 768;

  function formatStatus(status: string): string {
    if (isMobileView) {
      const compactMap: Record<string, string> = {
        'completed': '✓',
        'success': '✓',
        'processing': '...',
        'in_progress': '...',
        'pending': '--',
        'error': '✗',
        'failed': '✗',
      };
      return compactMap[status.toLowerCase()] || status.slice(0, 4);
    }
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
          <span class="cpu-mode-icon" aria-hidden="true">🧮</span>
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
            {#if ['tiny', 'tiny.en', 'base', 'base.en', 'small', 'small.en'].includes(stats.system?.whisper_model)}
              — {$t('settings.statistics.cpuMode.modelHintGood')}
            {:else}
              — {$t('settings.statistics.cpuMode.modelHintHeavy')}
            {/if}
          </li>
          <li>
            <strong>{$t('settings.statistics.cpuMode.diarizationLabel')}:</strong>
            {stats.system?.diarization_enabled
              ? $t('settings.statistics.cpuMode.diarizationOn')
              : $t('settings.statistics.cpuMode.diarizationOff')}
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
        <div class="stat-detail">{$t('settings.statistics.segments')}: {stats.files?.segments || 0}</div>
      </div>

      <!-- Task Stats -->
      <div class="stat-card">
        <h4>{$t('settings.statistics.tasks')}</h4>
        <div class="stat-detail">{$t('settings.statistics.pending')}: {stats.tasks?.pending || 0}</div>
        <div class="stat-detail">{$t('settings.statistics.running')}: {stats.tasks?.running || 0}</div>
        <div class="stat-detail">{$t('settings.statistics.completed')}: {stats.tasks?.completed || 0}</div>
        <div class="stat-detail">{$t('settings.statistics.failed')}: {stats.tasks?.failed || 0}</div>
        <div class="stat-detail">{$t('settings.statistics.successRate')}: {stats.tasks?.success_rate || 0}%</div>
      </div>

      <!-- Performance Stats -->
      <!-- svelte-ignore a11y-click-events-have-key-events -->
      <!-- svelte-ignore a11y-no-static-element-interactions -->
      <div class="stat-card stat-card-clickable" on:click={() => openProcessingDetails('performance')}>
        <h4>{$t('settings.statistics.performance')}</h4>
        <div class="stat-detail">{$t('settings.statistics.avgProcessTime')}: {formatTime(stats.tasks?.avg_processing_time || 0)}</div>
        <div class="stat-detail">{$t('settings.statistics.fileTimingAvg')}: {formatTime(stats.file_timing?.avg_secs || 0)}</div>
        <div class="stat-detail">{$t('settings.statistics.fileTimingMin')}: {formatTime(stats.file_timing?.min_secs || 0)}</div>
        <div class="stat-detail">{$t('settings.statistics.fileTimingMax')}: {formatTime(stats.file_timing?.max_secs || 0)}</div>
        <div class="stat-detail">{$t('settings.statistics.speakers')}: {stats.speakers?.total || 0}</div>
        <div class="stat-detail stat-detail-hint">{$t('settings.statistics.viewDetails')}</div>
      </div>

      <!-- Throughput & ETA -->
      <!-- svelte-ignore a11y-click-events-have-key-events -->
      <!-- svelte-ignore a11y-no-static-element-interactions -->
      <div class="stat-card stat-card-clickable" on:click={() => openProcessingDetails('throughput')}>
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
      <!-- svelte-ignore a11y-click-events-have-key-events -->
      <!-- svelte-ignore a11y-no-static-element-interactions -->
      <div class="stat-card stat-card-clickable" on:click={() => openProcessingDetails('queues')}>
        <h4>{$t('settings.statistics.queueDepths')}</h4>
        <div class="stat-value">{stats.queues?.total || 0} <span class="stat-unit">{$t('settings.statistics.queueTotal')}</span></div>
        {#if stats.queues?.total > 0}
          <div class="queue-bars">
            {#each [
              { key: 'gpu', label: $t('settings.statistics.queueGpu') },
              { key: 'download', label: $t('settings.statistics.queueDownload') },
              { key: 'nlp', label: $t('settings.statistics.queueNlp') },
              { key: 'embedding', label: $t('settings.statistics.queueEmbedding') },
              { key: 'cpu', label: $t('settings.statistics.queueCpu') }
            ] as queue}
              {#if stats.queues?.[queue.key] > 0}
                <div class="stat-detail">{queue.label}: {stats.queues[queue.key]}</div>
              {/if}
            {/each}
          </div>
        {/if}
        <div class="stat-detail stat-detail-hint">{$t('settings.statistics.viewDetails')}</div>
      </div>

      <!-- AI Models -->
      <!-- svelte-ignore a11y-click-events-have-key-events -->
      <!-- svelte-ignore a11y-no-static-element-interactions -->
      <div class="stat-card model-card stat-card-clickable" on:click={() => openProcessingDetails('models')}>
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
                  ⚠ {$t('settings.statistics.diarizationFallbackBadge')}
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
              {#each Object.entries(searchHealthStatus) as [name, info]}
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
          <div class="progress-bar">
            <div class="progress-fill" style="width: {parseFloat(stats.system?.cpu?.total_percent) || 0}%"></div>
          </div>
        </div>

        <div class="stat-section">
          <h4>{$t('settings.statistics.memoryUsage')}</h4>
          <div class="stat-value">{stats.system?.memory?.percent || '0%'}</div>
          <div class="stat-detail-compact">
            {stats.system?.memory?.used || $t('common.unknown')} / {stats.system?.memory?.total || $t('common.unknown')}
          </div>
          <div class="progress-bar">
            <div class="progress-fill" style="width: {parseFloat(stats.system?.memory?.percent) || 0}%"></div>
          </div>
        </div>
      </div>

      <div class="stat-card stat-card-with-bar">
        <div class="stat-card-content">
          <h4>{$t('settings.statistics.diskUsage')}</h4>
          <div class="stat-value">{stats.system?.disk?.percent || '0%'}</div>
          <div class="stat-detail">
            <span>{$t('settings.statistics.total')}: {stats.system?.disk?.total || $t('common.unknown')}</span>
            <span>{$t('settings.statistics.used')}: {stats.system?.disk?.used || $t('common.unknown')}</span>
            <span>{$t('settings.statistics.free')}: {stats.system?.disk?.free || $t('common.unknown')}</span>
          </div>
        </div>
        <div class="progress-bar">
          <div class="progress-fill" style="width: {parseFloat(stats.system?.disk?.percent) || 0}%"></div>
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
                  <button class="gpu-step-btn" on:click={() => currentGpuIndex = (currentGpuIndex - 1 + gpuCount) % gpuCount} aria-label={$t('settings.statistics.previousGpu')}>&#8249;</button>
                  <span class="gpu-step-label">GPU {currentGpuIndex + 1}/{gpuCount}</span>
                  <button class="gpu-step-btn" on:click={() => currentGpuIndex = (currentGpuIndex + 1) % gpuCount} aria-label={$t('settings.statistics.nextGpu')}>&#8250;</button>
                </div>
              {/if}
            </h4>
            <div class="stat-value">{activeGpu.memory_percent || '0%'}</div>
            <div class="stat-detail">
              <span>{$t('settings.statistics.gpu')}: {activeGpu.name || $t('common.unknown')}</span>
              <span>{$t('settings.statistics.total')}: {activeGpu.memory_total || $t('common.unknown')}</span>
              <span>{$t('settings.statistics.used')}: {activeGpu.memory_used || $t('common.unknown')}</span>
              <span>{$t('settings.statistics.free')}: {activeGpu.memory_free || $t('common.unknown')}</span>
              {#if activeGpu.utilization_percent && activeGpu.utilization_percent !== 'N/A'}
                <span>{$t('settings.statistics.gpuUtilization')}: {activeGpu.utilization_percent}</span>
              {/if}
              {#if activeGpu.temperature_celsius !== null && activeGpu.temperature_celsius !== undefined}
                <span>{$t('settings.statistics.gpuTemperature')}: {activeGpu.temperature_celsius}°C</span>
              {/if}
            </div>
          </div>
          <div class="progress-bar">
            <div class="progress-fill" style="width: {parseFloat(activeGpu.memory_percent) || 0}%"></div>
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
              {#each stats.tasks.recent as task}
                <tr>
                  <td>{task.id.substring(0, 8)}...</td>
                  <td>{task.type}</td>
                  <td>
                    <span class="status-badge status-{task.status}">{formatStatus(task.status)}</span>
                  </td>
                  <td>{new Date(task.created_at).toLocaleString()}</td>
                  <td>{formatTime(task.elapsed)}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      </div>
    {:else}
      <div class="recent-tasks" class:stats-refreshing={statsRefreshing}>
        <h4>{$t('settings.statistics.recentTasks')}</h4>
        <p class="empty-state">{$t('settings.statistics.noRecentTasks')}</p>
      </div>
    {/if}
  {/if}
</div>

<style>
  .cpu-mode-banner {
    background: rgba(var(--warning-color-rgb, 245, 158, 11), 0.08);
    border: 1px solid var(--warning-color, #f59e0b);
    border-left: 4px solid var(--warning-color, #f59e0b);
    border-radius: 6px;
    padding: 14px 18px;
    margin-bottom: 20px;
    color: var(--text-color);
  }

  .cpu-mode-banner-header {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 1rem;
    margin-bottom: 4px;
  }

  .cpu-mode-icon {
    font-size: 1.15rem;
  }

  .cpu-mode-banner-reason {
    font-size: 0.85rem;
    color: var(--text-secondary, #6b7280);
    margin-bottom: 10px;
  }

  .cpu-mode-banner-list {
    margin: 0 0 10px 0;
    padding-left: 22px;
    font-size: 0.9rem;
    line-height: 1.55;
  }

  .cpu-mode-banner-list code {
    background: var(--surface-color, rgba(0, 0, 0, 0.06));
    border: 1px solid var(--border-color, rgba(0, 0, 0, 0.1));
    border-radius: 3px;
    padding: 1px 5px;
    font-size: 0.85em;
  }

  .cpu-mode-banner-footer {
    font-size: 0.82rem;
    color: var(--text-secondary, #6b7280);
    border-top: 1px solid var(--border-color, rgba(0, 0, 0, 0.08));
    padding-top: 8px;
    margin-top: 4px;
  }

  .section-title {
    font-size: 1.125rem;
    font-weight: 600;
    margin: 0 0 0.25rem 0;
    color: var(--text-color);
  }

  .section-description {
    font-size: 0.8125rem;
    color: var(--text-secondary);
    margin: 0 0 1.25rem 0;
  }

  .btn {
    padding: 0.6rem 1.2rem;
    border-radius: 10px;
    border: none;
    font-size: 0.8125rem;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s ease;
  }

  .btn-secondary {
    background-color: var(--surface-color);
    color: var(--text-color);
    border: 1px solid var(--border-color);
  }

  .btn-secondary:hover:not(:disabled) {
    background-color: var(--button-hover);
    transform: scale(1.02);
    box-shadow: 0 4px 8px rgba(0, 0, 0, 0.1);
  }

  .btn-secondary:active:not(:disabled) {
    transform: scale(1);
  }

  .btn-secondary:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .btn-refresh {
    display: inline-flex;
    align-items: center;
    gap: 0.375rem;
  }

  .refresh-icon {
    flex-shrink: 0;
    transition: transform 0.3s ease;
  }

  .refresh-icon.spinning {
    animation: spin 1s linear infinite;
  }

  .stats-refreshing {
    opacity: 0.45;
    pointer-events: none;
    transition: opacity 0.3s ease;
  }

  .section-header-row {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 1rem;
    margin-bottom: 1rem;
    padding-right: 2rem;
  }

  .section-header-row .section-description {
    margin-bottom: 0;
  }

  .loading-state {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: 2rem;
    color: var(--text-secondary);
  }

  .loading-state p {
    margin: 0;
    font-size: 0.8125rem;
  }

  .stats-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
    gap: 1rem;
    margin-bottom: 1.5rem;
  }

  .stat-card {
    background-color: var(--background-color);
    border: 1px solid var(--border-color);
    border-radius: 8px;
    padding: 1rem;
  }

  .stat-card-clickable {
    cursor: pointer;
    transition: border-color 0.2s, box-shadow 0.2s;
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
    gap: 1rem;
  }

  .stat-section {
    display: flex;
    flex-direction: column;
  }

  .stat-section h4 {
    font-size: 0.6875rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--text-secondary);
    margin: 0 0 0.5rem 0;
  }

  .stat-section .stat-value {
    font-size: 1.5rem;
    font-weight: 700;
    color: var(--text-color);
    margin-bottom: 0.375rem;
  }

  .stat-detail-compact {
    font-size: 0.6875rem;
    color: var(--text-secondary);
    margin-bottom: 0.375rem;
  }

  .stat-card-content {
    flex: 1;
    display: flex;
    flex-direction: column;
    margin-bottom: 0.75rem;
  }

  .stat-card h4 {
    font-size: 0.6875rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--text-secondary);
    margin: 0 0 0.5rem 0;
  }

  .stat-value {
    font-size: 1.5rem;
    font-weight: 700;
    color: var(--text-color);
    margin-bottom: 0.375rem;
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
    font-size: 0.75rem;
    color: var(--text-secondary);
    margin-bottom: 0.125rem;
  }

  .stat-detail span {
    display: block;
    margin-bottom: 0.125rem;
  }

  .stat-detail-hint {
    margin-top: 0.5rem;
    font-size: 0.6875rem;
    opacity: 0.6;
    font-style: italic;
  }

  .stat-unit {
    font-size: 0.75rem;
    font-weight: 400;
    color: var(--text-secondary);
  }

  .stat-detail-warning {
    color: var(--warning-text, #92400e);
  }

  :global([data-theme='dark']) .stat-detail-warning {
    color: #fbbf24;
  }

  .stat-detail-active {
    color: var(--primary-on-surface);
    font-weight: 500;
  }

  .search-health-row {
    display: flex;
    align-items: center;
    gap: 0.375rem;
    margin-top: 0.375rem;
  }

  .search-health-dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: var(--text-secondary);
  }

  .search-health-dot.healthy {
    background: #22c55e;
  }

  .search-health-dot.error {
    background: #ef4444;
  }

  .search-health-label {
    font-size: 0.6875rem;
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
    background: none;
    border: 1px solid var(--border-color);
    border-radius: 3px;
    color: var(--text-secondary);
    cursor: pointer;
    font-size: 0.875rem;
    line-height: 1;
    padding: 0 0.3rem;
    transition: background-color 0.15s, color 0.15s;
  }

  .gpu-step-btn:hover {
    background-color: var(--primary-light, var(--border-color));
    color: var(--primary-on-surface);
    border-color: var(--primary-color);
  }

  .gpu-step-label {
    font-size: 0.6rem;
    font-weight: 600;
    color: var(--text-secondary);
    letter-spacing: 0.03em;
    white-space: nowrap;
  }

  .progress-bar {
    width: 100%;
    height: 8px;
    background-color: var(--border-color);
    border-radius: 4px;
    overflow: hidden;
    margin-top: 0;
  }

  .progress-fill {
    height: 100%;
    background-color: var(--primary-color);
    transition: width 0.3s ease;
  }

  .recent-tasks {
    margin-top: 1.5rem;
  }

  .recent-tasks h4 {
    font-size: 0.9375rem;
    font-weight: 600;
    color: var(--text-color);
    margin: 0 0 0.75rem 0;
  }

  .table-container {
    overflow-x: auto;
    border: 1px solid var(--border-color);
    border-radius: 8px;
  }

  .data-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.8125rem;
  }

  .data-table thead {
    background-color: var(--background-color);
  }

  .data-table th {
    padding: 0.5rem 0.75rem;
    text-align: left;
    font-size: 0.6875rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--text-secondary);
    border-bottom: 1px solid var(--border-color);
  }

  .data-table td {
    padding: 0.625rem 0.75rem;
    border-bottom: 1px solid var(--border-color);
    color: var(--text-color);
  }

  .data-table tbody tr:last-child td {
    border-bottom: none;
  }

  .data-table tbody tr:hover {
    background-color: var(--background-color);
  }

  .status-badge {
    display: inline-block;
    padding: 0.125rem 0.5rem;
    border-radius: 10px;
    font-size: 0.6875rem;
    font-weight: 500;
    text-transform: capitalize;
  }

  .status-completed,
  .status-success {
    background-color: #d1fae5;
    color: #065f46;
  }

  .status-running,
  .status-processing,
  .status-in_progress {
    background-color: #dbeafe;
    color: #1e40af;
  }

  .status-pending {
    background-color: #fef3c7;
    color: #92400e;
  }

  .status-failed,
  .status-error {
    background-color: #fee2e2;
    color: #991b1b;
  }

  :global([data-theme='dark']) .status-completed,
  :global([data-theme='dark']) .status-success {
    background-color: #064e3b;
    color: #6ee7b7;
  }

  :global([data-theme='dark']) .status-running,
  :global([data-theme='dark']) .status-processing,
  :global([data-theme='dark']) .status-in_progress {
    background-color: #1e3a8a;
    color: #93c5fd;
  }

  :global([data-theme='dark']) .status-pending {
    background-color: #78350f;
    color: #fde68a;
  }

  :global([data-theme='dark']) .status-failed,
  :global([data-theme='dark']) .status-error {
    background-color: #7f1d1d;
    color: #fca5a5;
  }

  .empty-state {
    text-align: center;
    padding: 1rem;
    color: var(--text-secondary);
    font-size: 0.8125rem;
    font-style: italic;
  }

  /* AI Models Card Styles */
  .model-card {
    grid-column: span 1;
  }

  .model-info {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }

  .model-item {
    display: flex;
    flex-direction: column;
    gap: 0.125rem;
  }

  .model-label {
    font-size: 0.6875rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--text-secondary);
  }

  .model-value {
    font-size: 0.8125rem;
    font-weight: 500;
    color: var(--text-color);
    font-family: 'Courier New', Courier, monospace;
  }

  .model-fallback-badge {
    display: inline-block;
    align-self: flex-start;
    margin-top: 0.125rem;
    font-size: 0.6875rem;
    font-weight: 600;
    color: var(--warning-text, #92400e);
    background: rgba(var(--warning-color-rgb, 245, 158, 11), 0.12);
    border: 1px solid var(--warning-color, #f59e0b);
    border-radius: 10px;
    padding: 0.125rem 0.5rem;
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
      gap: 0.5rem;
    }

    .section-header-row .btn-refresh {
      width: 100%;
      justify-content: center;
      min-height: 44px;
    }

    .stat-card {
      padding: 0.75rem;
    }

    .stat-value {
      font-size: 1.25rem;
    }

    .model-value {
      word-break: break-all;
    }

    .status-badge {
      padding: 0.1rem 0.35rem;
      font-size: 0.75rem;
      min-width: 24px;
      text-align: center;
    }

    .data-table th,
    .data-table td {
      padding: 0.4rem 0.5rem;
      font-size: 0.75rem;
    }
  }
</style>
