<script lang="ts">
  import type { SystemStats } from '$lib/settings/adminLoaders';
  import { formatDateTime, formatElapsed } from '$lib/utils/formatting';
  import MenuIcon from '$components/ui/MenuIcon.svelte';
  import { QUEUE_NAME_KEYS, labelFor } from '$lib/i18n/keyMaps';
  import { t, locale } from '$stores/locale';
  import BaseModal from '$components/ui/BaseModal.svelte';

  export let isOpen = false;
  export let section = 'performance';
  export let stats: SystemStats;

  function close() {
    isOpen = false;
  }

  const queueDescriptions: Record<string, string> = {
    gpu: 'settings.statistics.queueDescGpu',
    download: 'settings.statistics.queueDescDownload',
    nlp: 'settings.statistics.queueDescNlp',
    embedding: 'settings.statistics.queueDescEmbedding',
    cpu: 'settings.statistics.queueDescCpu'
  };
</script>

<BaseModal {isOpen} title={$t('settings.statistics.processingDetails')} onClose={close} size="md" zIndex={1300}>
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
                <span class="detail-value">{formatElapsed(stats.tasks?.avg_processing_time || 0)}</span>
              </div>
              <div class="detail-row">
                <span class="detail-label">{$t('settings.statistics.fileTimingAvg')}</span>
                <span class="detail-value">{formatElapsed(stats.file_timing?.avg_secs || 0)}</span>
              </div>
              <div class="detail-row">
                <span class="detail-label">{$t('settings.statistics.fileTimingMin')}</span>
                <span class="detail-value">{formatElapsed(stats.file_timing?.min_secs || 0)}</span>
              </div>
              <div class="detail-row">
                <span class="detail-label">{$t('settings.statistics.fileTimingMax')}</span>
                <span class="detail-value">{formatElapsed(stats.file_timing?.max_secs || 0)}</span>
              </div>
              <div class="detail-row">
                <span class="detail-label">{$t('settings.statistics.completedFiles')}</span>
                <span class="detail-value">{stats.file_timing?.files || 0}</span>
              </div>
              <div class="detail-row">
                <span class="detail-label">{$t('settings.statistics.speakers')}</span>
                <span class="detail-value">{stats.speakers?.total_is_estimate ? '≈' : ''}{stats.speakers?.total || 0} ({$t('settings.statistics.avgProcessTime')}: {$t('settings.statistics.perFile', { value: stats.speakers?.avg_per_file || 0 })})</span>
              </div>
              <div class="detail-row">
                <span class="detail-label">{$t('settings.statistics.successRate')}</span>
                <span class="detail-value" dir="ltr">{stats.tasks?.success_rate || 0}%</span>
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
                    <span class="detail-value">{formatDateTime(stats.eta.est_completion, $t('common.notAvailable'), $locale)}</span>
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
            {#if stats.queues_available === false}
              <div class="empty-notice">{$t('settings.statistics.queuesUnavailable')}</div>
            {/if}
            <div class="detail-grid">
              {#each ['gpu', 'download', 'nlp', 'embedding', 'cpu'] as queueName (queueName)}
                {@const description = queueDescriptions[queueName]}
                <div class="detail-row">
                  <span class="detail-label">
                    {labelFor($t, QUEUE_NAME_KEYS, queueName)}
                    {#if description}
                      <span class="detail-sublabel">{$t(description)}</span>
                    {/if}
                  </span>
                  <span class="detail-value" class:highlight={(stats.queues?.[queueName] ?? 0) > 0}>
                    {stats.queues?.[queueName] || 0}
                  </span>
                </div>
              {/each}
              <div class="detail-row detail-row-total">
                <span class="detail-label">{$t('settings.statistics.queueTotal')}</span>
                <span class="detail-value" class:highlight={(stats.queues?.total ?? 0) > 0}>{stats.queues?.total || 0}</span>
              </div>
            </div>
          </div>
        {/if}

        <!-- Models Section -->
        {#if section === 'models'}
          <div class="detail-section">
            {#if stats.models}
              <div class="detail-grid">
                {#each Object.entries(stats.models) as [key, model] (key)}
                  {@const m = model as {
                    name?: string;
                    description?: string;
                    purpose?: string;
                    configured_backend?: string;
                    configured_description?: string;
                    effective_backend?: string;
                    using_fallback?: boolean;
                  }}
                  <div class="detail-row model-detail-row">
                    <div class="model-detail-info">
                      <span class="detail-label">{m.purpose || key}</span>
                      <span class="detail-sublabel">{m.description || ''}</span>
                    </div>
                    <span class="detail-value model-name">{m.name || $t('common.notAvailable')}</span>
                  </div>
                  {#if m.configured_backend && m.effective_backend}
                    <div class="detail-row model-engine-row">
                      <span class="detail-label">{$t('settings.statistics.diarizationConfigured')}</span>
                      <span class="detail-value model-name">{m.configured_description || m.configured_backend}</span>
                    </div>
                    <div class="detail-row model-engine-row">
                      <span class="detail-label">{$t('settings.statistics.diarizationEffective')}</span>
                      <span class="detail-value model-name">{m.description || m.effective_backend}</span>
                    </div>
                    {#if m.using_fallback}
                      <div class="detail-row fallback-warning-row" role="alert">
                        <span class="fallback-warning-icon" aria-hidden="true"><MenuIcon name="warning" size="md" /></span>
                        <span class="fallback-warning-text">
                          {$t('settings.statistics.diarizationFallbackWarning', {
                            configured: m.configured_description || m.configured_backend,
                            effective: m.description || m.effective_backend
                          })}
                        </span>
                      </div>
                    {/if}
                  {/if}
                {/each}
              </div>
            {/if}
          </div>
        {/if}
</BaseModal>

<style>
  .detail-tabs {
    display: flex;
    gap: var(--space-1);
    margin-bottom: var(--space-5);
    border-bottom: 1px solid var(--border-color);
    padding-bottom: 0;
  }

  .tab {
    background: none;
    border: none;
    border-bottom: 2px solid transparent;
    border-radius: 0;
    padding: var(--space-2) var(--space-3);
    font-size: var(--font-size-sm);
    font-weight: var(--font-weight-medium);
    color: var(--text-secondary);
    cursor: pointer;
    box-shadow: none;
    transition: color var(--duration-base), border-color var(--duration-base);
  }

  .tab:hover {
    color: var(--text-color);
    background: none;
    transform: none;
    box-shadow: none;
  }

  .tab.active {
    color: var(--primary-on-surface);
    border-bottom-color: var(--primary-color);
  }

  .detail-section {
    animation: fade-in 0.15s ease-out;
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
    padding: var(--space-2-5) 0;
    border-bottom: 1px solid var(--border-color);
  }

  .detail-row:last-child {
    border-bottom: none;
  }

  .detail-row-total {
    border-top: 2px solid var(--border-color);
    font-weight: var(--font-weight-semibold);
    margin-top: var(--space-1);
    padding-top: var(--space-3);
  }

  .detail-label {
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
    display: flex;
    flex-direction: column;
    gap: var(--space-0-5);
  }

  .detail-sublabel {
    font-size: var(--font-size-2xs);
    color: var(--text-secondary);
    opacity: 0.7;
  }

  .detail-value {
    font-size: var(--font-size-md);
    font-weight: var(--font-weight-semibold);
    color: var(--text-color);
    text-align: right;
  }

  .detail-value.highlight {
    color: var(--primary-on-surface);
  }

  .detail-value.model-name {
    font-family: 'Courier New', Courier, monospace;
    font-size: var(--font-size-sm);
  }

  .model-detail-row {
    align-items: flex-start;
    gap: var(--space-4);
  }

  .model-detail-info {
    display: flex;
    flex-direction: column;
    gap: var(--space-0-5);
    flex: 1;
  }

  .model-engine-row {
    padding-left: var(--space-4);
    padding-top: var(--space-1-5);
    padding-bottom: var(--space-1-5);
  }

  .model-engine-row .detail-label {
    font-size: var(--font-size-xs);
  }

  .model-engine-row .detail-value {
    font-size: var(--font-size-xs);
  }

  .fallback-warning-row {
    display: flex;
    align-items: flex-start;
    gap: var(--space-2);
    background: rgba(var(--warning-color-rgb), 0.1);
    border: 1px solid var(--warning-color);
    border-radius: var(--radius-md);
    padding: var(--space-2) var(--space-2-5);
    margin: var(--space-1-5) 0;
  }

  .fallback-warning-icon {
    display: inline-flex;
    margin-top: 0.1rem;
    color: var(--warning-text);
    flex-shrink: 0;
  }

  .fallback-warning-text {
    font-size: var(--font-size-xs);
    color: var(--text-color);
    font-weight: var(--font-weight-medium);
  }

  :global([data-theme='dark']) .fallback-warning-row {
    background: rgba(251, 191, 36, 0.12);
    border-color: #fbbf24;
  }

  :global([data-theme='dark']) .fallback-warning-icon {
    color: #fbbf24;
  }

  .subsection-title {
    font-size: var(--font-size-sm);
    font-weight: var(--font-weight-medium);
    color: var(--text-color);
    margin: var(--space-5) 0 var(--space-2) 0;
  }

  .empty-notice {
    text-align: center;
    padding: var(--space-6);
    color: var(--text-secondary);
    font-size: var(--font-size-sm);
    font-style: italic;
  }

  @media (max-width: 768px) {
    .detail-tabs {
      overflow-x: auto;
    }

    .tab {
      white-space: nowrap;
      min-height: 44px;
      padding: var(--space-2) var(--space-2-5);
    }

    .detail-row {
      flex-direction: column;
      align-items: flex-start;
      gap: var(--space-1);
    }

    .detail-value {
      text-align: left;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .detail-section { animation: none; }
  }
</style>
