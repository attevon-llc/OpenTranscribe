<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { t } from '$stores/locale';
  import axiosInstance from '$lib/axios';
  import { toastStore } from '$stores/toast';
  import { capabilities, isCapabilityEnabled } from '$stores/capabilities';
  import Spinner from '../ui/Spinner.svelte';
  import ProgressBar from '../ui/ProgressBar.svelte';

  // Bulk processing state
  let pendingFiles = 0;
  let migrationInProgress = false;
  let migrationTotalFiles = 0;
  let migrationProcessedFiles = 0;
  let migrationFailedFiles: string[] = [];
  let stoppingMigration = false;
  let loadingMigrationStatus = false;
  let etaSeconds: number | null = null;
  let totalFiles = 0;
  let showForceReprocessConfirm = false;

  // Progress message from WS (e.g. "Queued — 6 batches waiting for GPU worker")
  let migrationProgressMessage = '';

  interface MigrationProgress {
    processed_files: number;
    total_files: number;
    failed_files: string[];
    progress: number;
    running: boolean;
    eta_seconds?: number | null;
    message?: string;
  }

  interface MigrationComplete {
    status: string;
    total_files: number;
    failed_files: string[];
    success_count: number;
  }

  function formatEta(seconds: number | null | undefined): string {
    if (seconds == null || seconds <= 0) return '';
    if (seconds < 60) return `${Math.round(seconds)}s`;
    const m = Math.floor(seconds / 60);
    const s = Math.round(seconds % 60);
    if (m < 60) return s > 0 ? `${m}m ${s}s` : `${m}m`;
    const h = Math.floor(m / 60);
    return `${h}h ${m % 60}m`;
  }

  function handleMigrationProgress(event: CustomEvent<MigrationProgress>) {
    const data = event.detail;
    migrationProcessedFiles = data.processed_files;
    migrationTotalFiles = data.total_files;
    migrationFailedFiles = data.failed_files || [];
    migrationInProgress = data.running;
    etaSeconds = data.eta_seconds ?? null;
    migrationProgressMessage = data.message || '';
  }

  function handleMigrationComplete(event: CustomEvent<MigrationComplete>) {
    const data = event.detail;
    migrationInProgress = false;
    etaSeconds = null;
    migrationProcessedFiles = data.total_files || 0;
    migrationTotalFiles = data.total_files || 0;
    migrationFailedFiles = data.failed_files || [];

    loadMigrationStatus(true);

    if (data.status === 'stopped') {
      toastStore.info($t('settings.speakerAttributes.migrationStopped'));
    } else if ((data.failed_files || []).length === 0) {
      toastStore.success($t('settings.speakerAttributes.migrationComplete'));
    } else {
      toastStore.warning(
        $t('settings.speakerAttributes.migrationCompleteWithErrors', {
          failed: (data.failed_files || []).length
        })
      );
    }
  }

  // The bulk re-detection job spans every file, so a deployment can withhold it
  // (the server 404s its routes when the capability is off).
  $: migrationAvailable = isCapabilityEnabled($capabilities, 'speaker_attributes.migration');

  onMount(() => {
    if (migrationAvailable) loadMigrationStatus();

    // WebSocket events provide real-time progress — no polling needed
    window.addEventListener('attribute-migration-progress', handleMigrationProgress as EventListener);
    window.addEventListener('attribute-migration-complete', handleMigrationComplete as EventListener);
  });

  onDestroy(() => {
    window.removeEventListener('attribute-migration-progress', handleMigrationProgress as EventListener);
    window.removeEventListener('attribute-migration-complete', handleMigrationComplete as EventListener);
  });

  async function loadMigrationStatus(silent = false) {
    if (!silent) loadingMigrationStatus = true;
    try {
      const { data } = await axiosInstance.get('/speaker-attributes/migration/status');
      pendingFiles = data.pending_files || 0;
      totalFiles = data.total_files || 0;

      const progress = data.progress;
      if (progress) {
        migrationInProgress = progress.running || false;
        migrationTotalFiles = progress.total_files || 0;
        migrationProcessedFiles = progress.processed_files || 0;
        migrationFailedFiles = progress.failed_files || [];
        if (progress.eta_seconds != null) {
          etaSeconds = progress.eta_seconds;
        }
      }
    } catch (err) {
      console.error('Failed to load attribute migration status:', err);
    } finally {
      loadingMigrationStatus = false;
    }
  }

  async function startMigration() {
    migrationInProgress = true;
    try {
      const { data } = await axiosInstance.post('/speaker-attributes/migration/start');
      if (data.status === 'already_running') return;

      toastStore.success($t('settings.speakerAttributes.migrationStarted'));
    } catch (err) {
      console.error('Failed to start attribute migration:', err);
      toastStore.error($t('settings.speakerAttributes.startFailed'));
      migrationInProgress = false;
    }
  }

  async function startForceMigration() {
    showForceReprocessConfirm = false;
    migrationInProgress = true;
    try {
      const { data } = await axiosInstance.post('/speaker-attributes/migration/start', null, {
        params: { force: true },
      });
      if (data.status === 'already_running') return;

      toastStore.success($t('settings.speakerAttributes.forceReprocessStarted'));
    } catch (err) {
      console.error('Failed to start force reprocessing:', err);
      toastStore.error($t('settings.speakerAttributes.forceReprocessFailed'));
      migrationInProgress = false;
    }
  }

  async function stopMigration() {
    stoppingMigration = true;
    try {
      await axiosInstance.post('/speaker-attributes/migration/stop');

      toastStore.success($t('settings.speakerAttributes.stopMigration'));
      await loadMigrationStatus();
    } catch (err) {
      console.error('Failed to stop attribute migration:', err);
      toastStore.error($t('settings.speakerAttributes.stopFailed'));
    } finally {
      stoppingMigration = false;
    }
  }
</script>

    {#if migrationAvailable}
  <div class="bulk-section">
    <div class="bulk-separator"></div>
    <h4 class="bulk-title">{$t('settings.speakerAttributes.bulkProcessing')}</h4>
    <p class="bulk-description">{$t('settings.speakerAttributes.bulkDescription')}</p>

    {#if loadingMigrationStatus}
      <div class="skeleton-bulk">
        <div class="skeleton-bar"></div>
        <div class="skeleton-btn"></div>
      </div>
    {:else if migrationInProgress}
      <div class="progress-section">
        {#if migrationProcessedFiles === 0}
          <div class="queued-message">
            {migrationProgressMessage || $t('settings.speakerAttributes.queued')}
          </div>
          <ProgressBar percent={null} />
        {:else}
          <div class="progress-header">
            <span class="progress-text">
              {$t('settings.speakerAttributes.migrationProgress', {
                processed: migrationProcessedFiles,
                total: migrationTotalFiles,
              })}
            </span>
            <span class="progress-percent">
              {Math.round((migrationProcessedFiles / Math.max(migrationTotalFiles, 1)) * 100)}%
              {#if formatEta(etaSeconds)}
                ({formatEta(etaSeconds)} {$t('common.remaining')})
              {/if}
            </span>
          </div>
          <div class="progress-bar-container">
            <div
              class="progress-bar-fill"
              style="width: {(migrationProcessedFiles / Math.max(migrationTotalFiles, 1)) * 100}%"
            ></div>
          </div>
        {/if}
        {#if migrationFailedFiles.length > 0}
          <div class="failed-info">
            {$t('settings.embeddingMigration.failedFiles', { count: migrationFailedFiles.length })}
          </div>
        {/if}
        <button
          class="btn btn-danger btn-stop"
          on:click={stopMigration}
          disabled={stoppingMigration}
        >
          {#if stoppingMigration}
            <Spinner size="small" color="white" />
          {/if}
          {$t('settings.speakerAttributes.stopMigration')}
        </button>
      </div>
    {:else if pendingFiles > 0}
      <div class="pending-info">
        <span>{$t('settings.speakerAttributes.filesWithoutPredictions', { count: pendingFiles })}</span>
      </div>
      <button class="btn btn-primary" on:click={startMigration}>
        {$t('settings.speakerAttributes.runDetection')}
      </button>
    {:else if totalFiles > 0}
      <div class="all-processed">
        {$t('settings.speakerAttributes.noFilesToProcess')}
      </div>
      {#if showForceReprocessConfirm}
        <div class="reextract-confirm" style="margin-top: 0.75rem;">
          <p>{$t('settings.speakerAttributes.forceReprocessConfirm', { count: totalFiles })}</p>
          <div class="confirm-buttons">
            <button class="btn btn-danger" on:click={startForceMigration}>
              {$t('settings.speakerAttributes.forceReprocessConfirmBtn')}
            </button>
            <button class="btn btn-secondary" on:click={() => showForceReprocessConfirm = false}>
              {$t('common.cancel')}
            </button>
          </div>
        </div>
      {:else}
        <button class="btn btn-secondary" style="margin-top: 0.75rem;" on:click={() => showForceReprocessConfirm = true}>
          {$t('settings.speakerAttributes.forceReprocess')}
        </button>
      {/if}
    {:else}
      <div class="all-processed">
        {$t('settings.speakerAttributes.noFilesToProcess')}
      </div>
    {/if}
  </div>
  {/if}

<style>
  .skeleton-bulk {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
    padding: 0.5rem 0;
  }

  .skeleton-bar {
    height: 32px;
    width: 100%;
    border-radius: 6px;
    background: var(--border-color, #444);
    animation: skeleton-pulse 1.5s ease-in-out infinite;
  }

  .skeleton-btn {
    height: 36px;
    width: 140px;
    border-radius: 6px;
    background: var(--border-color, #444);
    animation: skeleton-pulse 1.5s ease-in-out infinite;
  }

  .bulk-section {
    margin-top: 1rem;
  }

  .bulk-separator {
    border-top: 1px solid var(--border-color, #444);
    margin-bottom: 1rem;
  }

  .bulk-title {
    font-size: 0.95rem;
    font-weight: 600;
    color: var(--text-color, #e0e0e0);
    margin: 0 0 0.25rem 0;
  }

  .bulk-description {
    font-size: 0.8rem;
    color: var(--text-secondary, #999);
    margin: 0 0 1rem 0;
    line-height: 1.4;
  }

  .pending-info {
    font-size: 0.85rem;
    color: var(--text-secondary, #999);
    margin-bottom: 0.75rem;
    padding: 0.625rem 0.875rem;
    background: rgba(59, 130, 246, 0.08);
    border: 1px solid rgba(59, 130, 246, 0.2);
    border-radius: 6px;
  }

  .all-processed {
    font-size: 0.85rem;
    color: var(--success-color, #51cf66);
    padding: 0.625rem 0.875rem;
    background: rgba(81, 207, 102, 0.08);
    border: 1px solid rgba(81, 207, 102, 0.2);
    border-radius: 6px;
  }

  .progress-section {
    padding: 1rem;
    background: var(--background-color, #2a2a2a);
    border-radius: 6px;
    border: 1px solid var(--border-color, #444);
  }

  .progress-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 0.5rem;
  }

  .progress-text {
    font-size: 0.8rem;
    color: var(--text-secondary, #999);
  }

  .progress-percent {
    font-size: 0.8rem;
    font-weight: 600;
    color: var(--primary-color, #4a9eff);
  }

  .progress-bar-container {
    height: 8px;
    background: var(--border-color, #444);
    border-radius: 4px;
    overflow: hidden;
  }

  .progress-bar-fill {
    height: 100%;
    background: var(--primary-color, #4a9eff);
    border-radius: 4px;
    transition: width 0.3s ease;
  }

  .queued-message {
    font-size: 0.8rem;
    color: var(--text-secondary, #999);
    margin-bottom: 0.5rem;
    font-style: italic;
  }

  .failed-info {
    font-size: 0.75rem;
    color: var(--error-color, #ff6b6b);
    margin-top: 0.5rem;
  }

  .btn-stop {
    margin-top: 0.75rem;
  }

  .reextract-confirm {
    padding: 0.875rem;
    background: rgba(239, 68, 68, 0.08);
    border: 1px solid rgba(239, 68, 68, 0.2);
    border-radius: 6px;
  }

  .reextract-confirm p {
    font-size: 0.85rem;
    color: var(--text-color, #e0e0e0);
    margin: 0 0 0.75rem 0;
    line-height: 1.4;
  }

  .confirm-buttons {
    display: flex;
    gap: 0.5rem;
  }

  @media (max-width: 768px) {
    .confirm-buttons {
      flex-wrap: wrap;
    }

    .confirm-buttons .btn {
      flex: 1;
      min-height: 44px;
    }
  }
</style>
