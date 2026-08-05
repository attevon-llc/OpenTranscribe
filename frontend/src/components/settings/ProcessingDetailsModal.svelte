<script lang="ts">
  import { t } from '$stores/locale';
  import BaseModal from '../ui/BaseModal.svelte';

  export let isOpen = false;
  export let section = 'performance';
  export let stats: any = {};

  function close() {
    isOpen = false;
  }

  function formatTime(seconds: number): string {
    if (!seconds) return '0s';
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const secs = Math.floor(seconds % 60);
    let result = '';
    if (hours > 0) result += `${hours}h `;
    if (minutes > 0 || hours > 0) result += `${minutes}m `;
    result += `${secs}s`;
    return result.trim();
  }

  function formatDateTime(isoString: string | null): string {
    if (!isoString) return $t('common.notAvailable');
    return new Date(isoString).toLocaleString();
  }

  const queueDescriptions: Record<string, string> = {
    gpu: 'settings.statistics.queueDescGpu',
    download: 'settings.statistics.queueDescDownload',
    nlp: 'settings.statistics.queueDescNlp',
    embedding: 'settings.statistics.queueDescEmbedding',
    cpu: 'settings.statistics.queueDescCpu'
  };
</script>

<BaseModal {isOpen} title={$t('settings.statistics.processingDetails')} onClose={close} maxWidth="600px" zIndex={1300}>
        <!-- Tab navigation -->
        <div class="detail-tabs">
          <button class="tab" class:active={section === 'performance'} on:click={() => section = 'performance'}>
            {$t('settings.statistics.performance')}
          </button>
          <button class="tab" class:active={section === 'throughput'} on:click={() => section = 'throughput'}>
            {$t('settings.statistics.throughput')}
          </button>
          <button class="tab" class:active={section === 'queues'} on:click={() => section = 'queues'}>
            {$t('settings.statistics.queueDepths')}
          </button>
          <button class="tab" class:active={section === 'models'} on:click={() => section = 'models'}>
            {$t('settings.statistics.aiModels')}
          </button>
        </div>

        <!-- Performance Section -->
        {#if section === 'performance'}
          <div class="detail-section">
            <div class="detail-grid">
              <div class="detail-row">
                <span class="detail-label">{$t('settings.statistics.avgProcessTime')}</span>
                <span class="detail-value">{formatTime(stats.tasks?.avg_processing_time || 0)}</span>
              </div>
              <div class="detail-row">
                <span class="detail-label">{$t('settings.statistics.fileTimingAvg')}</span>
                <span class="detail-value">{formatTime(stats.file_timing?.avg_secs || 0)}</span>
              </div>
              <div class="detail-row">
                <span class="detail-label">{$t('settings.statistics.fileTimingMin')}</span>
                <span class="detail-value">{formatTime(stats.file_timing?.min_secs || 0)}</span>
              </div>
              <div class="detail-row">
                <span class="detail-label">{$t('settings.statistics.fileTimingMax')}</span>
                <span class="detail-value">{formatTime(stats.file_timing?.max_secs || 0)}</span>
              </div>
              <div class="detail-row">
                <span class="detail-label">{$t('settings.statistics.completedFiles')}</span>
                <span class="detail-value">{stats.file_timing?.files || 0}</span>
              </div>
              <div class="detail-row">
                <span class="detail-label">{$t('settings.statistics.speakers')}</span>
                <span class="detail-value">{stats.speakers?.total || 0} ({$t('settings.statistics.avgProcessTime')}: {$t('settings.statistics.perFile', { value: stats.speakers?.avg_per_file || 0 })})</span>
              </div>
              <div class="detail-row">
                <span class="detail-label">{$t('settings.statistics.successRate')}</span>
                <span class="detail-value">{stats.tasks?.success_rate || 0}%</span>
              </div>
            </div>
          </div>
        {/if}

        <!-- Throughput Section -->
        {#if section === 'throughput'}
          <div class="detail-section">
            <div class="detail-grid">
              <div class="detail-row">
                <span class="detail-label">{$t('settings.statistics.currentRate')}</span>
                <span class="detail-value">{stats.throughput?.rate_1h || 0} {$t('settings.statistics.filesPerHour')}</span>
              </div>
              <div class="detail-row">
                <span class="detail-label">{$t('settings.statistics.avgRate3h')}</span>
                <span class="detail-value">{stats.throughput?.rate_3h || 0} {$t('settings.statistics.filesPerHour')}</span>
              </div>
              <div class="detail-row">
                <span class="detail-label">{$t('settings.statistics.completed')} (1h)</span>
                <span class="detail-value">{stats.throughput?.last_1h || 0}</span>
              </div>
              <div class="detail-row">
                <span class="detail-label">{$t('settings.statistics.completed')} (3h)</span>
                <span class="detail-value">{stats.throughput?.last_3h || 0}</span>
              </div>
              <div class="detail-row">
                <span class="detail-label">{$t('settings.statistics.completed')} ({$t('settings.statistics.total')})</span>
                <span class="detail-value">{stats.throughput?.total_completed || 0}</span>
              </div>
            </div>

            {#if stats.eta?.remaining > 0}
              <h4 class="subsection-title">{$t('settings.statistics.estimatedCompletion')}</h4>
              <div class="detail-grid">
                <div class="detail-row">
                  <span class="detail-label">{$t('settings.statistics.remaining')}</span>
                  <span class="detail-value">{$t('settings.statistics.remainingFiles', { count: stats.eta.remaining })}</span>
                </div>
                <div class="detail-row">
                  <span class="detail-label">{$t('settings.statistics.filesPerHour')}</span>
                  <span class="detail-value">{stats.eta.files_per_hour}</span>
                </div>
                {#if stats.eta.hours_remaining !== null}
                  <div class="detail-row">
                    <span class="detail-label">{$t('settings.statistics.hoursRemaining')}</span>
                    <span class="detail-value">{stats.eta.hours_remaining}h</span>
                  </div>
                {/if}
                {#if stats.eta.est_completion}
                  <div class="detail-row">
                    <span class="detail-label">{$t('settings.statistics.estimatedCompletion')}</span>
                    <span class="detail-value">{formatDateTime(stats.eta.est_completion)}</span>
                  </div>
                {/if}
              </div>
            {:else}
              <div class="empty-notice">{$t('settings.statistics.noActiveProcessing')}</div>
            {/if}
          </div>
        {/if}

        <!-- Queues Section -->
        {#if section === 'queues'}
          <div class="detail-section">
            <div class="detail-grid">
              {#each ['gpu', 'download', 'nlp', 'embedding', 'cpu'] as queueName}
                <div class="detail-row">
                  <span class="detail-label">
                    {$t(`settings.statistics.queue${queueName.charAt(0).toUpperCase() + queueName.slice(1)}`)}
                    <span class="detail-sublabel">{$t(queueDescriptions[queueName])}</span>
                  </span>
                  <span class="detail-value" class:highlight={stats.queues?.[queueName] > 0}>
                    {stats.queues?.[queueName] || 0}
                  </span>
                </div>
              {/each}
              <div class="detail-row detail-row-total">
                <span class="detail-label">{$t('settings.statistics.queueTotal')}</span>
                <span class="detail-value" class:highlight={stats.queues?.total > 0}>{stats.queues?.total || 0}</span>
              </div>
            </div>
          </div>
        {/if}

        <!-- Models Section -->
        {#if section === 'models'}
          <div class="detail-section">
            {#if stats.models}
              <div class="detail-grid">
                {#each Object.entries(stats.models) as [key, model]}
                  {@const m = model as {name?: string; description?: string; purpose?: string}}
                  <div class="detail-row model-detail-row">
                    <div class="model-detail-info">
                      <span class="detail-label">{m.purpose || key}</span>
                      <span class="detail-sublabel">{m.description || ''}</span>
                    </div>
                    <span class="detail-value model-name">{m.name || $t('common.notAvailable')}</span>
                  </div>
                {/each}
              </div>
            {/if}
          </div>
        {/if}
</BaseModal>

<style>
  .detail-tabs {
    display: flex;
    gap: 0.25rem;
    margin-bottom: 1.25rem;
    border-bottom: 1px solid var(--border-color);
    padding-bottom: 0;
  }

  .tab {
    background: none;
    border: none;
    border-bottom: 2px solid transparent;
    border-radius: 0;
    padding: 0.5rem 0.75rem;
    font-size: 0.8125rem;
    font-weight: 500;
    color: var(--text-secondary);
    cursor: pointer;
    box-shadow: none;
    transition: color 0.2s, border-color 0.2s;
  }

  .tab:hover {
    color: var(--text-color);
    background: none;
    transform: none;
    box-shadow: none;
  }

  .tab.active {
    color: var(--primary-color);
    border-bottom-color: var(--primary-color);
  }

  .detail-section {
    animation: fadeIn 0.15s ease-out;
  }

  .detail-grid {
    display: flex;
    flex-direction: column;
    gap: 0;
  }

  .detail-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 0.625rem 0;
    border-bottom: 1px solid var(--border-color);
  }

  .detail-row:last-child {
    border-bottom: none;
  }

  .detail-row-total {
    border-top: 2px solid var(--border-color);
    font-weight: 600;
    margin-top: 0.25rem;
    padding-top: 0.75rem;
  }

  .detail-label {
    font-size: 0.8125rem;
    color: var(--text-secondary);
    display: flex;
    flex-direction: column;
    gap: 0.125rem;
  }

  .detail-sublabel {
    font-size: 0.6875rem;
    color: var(--text-secondary);
    opacity: 0.7;
  }

  .detail-value {
    font-size: 0.875rem;
    font-weight: 600;
    color: var(--text-color);
    text-align: right;
  }

  .detail-value.highlight {
    color: var(--primary-color);
  }

  .detail-value.model-name {
    font-family: 'Courier New', Courier, monospace;
    font-size: 0.8125rem;
  }

  .model-detail-row {
    align-items: flex-start;
    gap: 1rem;
  }

  .model-detail-info {
    display: flex;
    flex-direction: column;
    gap: 0.125rem;
    flex: 1;
  }

  .subsection-title {
    font-size: 0.8125rem;
    font-weight: 600;
    color: var(--text-color);
    margin: 1.25rem 0 0.5rem 0;
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }

  .empty-notice {
    text-align: center;
    padding: 1.5rem;
    color: var(--text-secondary);
    font-size: 0.8125rem;
    font-style: italic;
  }

  @media (max-width: 768px) {
    .detail-tabs {
      overflow-x: auto;
    }

    .tab {
      white-space: nowrap;
      min-height: 44px;
      padding: 0.5rem 0.625rem;
    }

    .detail-row {
      flex-direction: column;
      align-items: flex-start;
      gap: 0.25rem;
    }

    .detail-value {
      text-align: left;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .detail-section { animation: none; }
  }
</style>
