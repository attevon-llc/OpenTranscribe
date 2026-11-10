<script lang="ts">
  import SettingsSkeleton from '$components/ui/SettingsSkeleton.svelte';
  import { onMount, onDestroy } from 'svelte';
  import { t } from '$stores/locale';
  import axiosInstance from '$lib/axios';
  import { toastStore } from '$stores/toast';
  import { capabilities, isCapabilityEnabled } from '$stores/capabilities';
  import LiveProgressCard from '$components/ui/LiveProgressCard.svelte';

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
      <SettingsSkeleton variant="form" count={2} label={$t('common.loading')} />
    {:else if migrationInProgress}
      <div class="progress-section">
        <LiveProgressCard
          state={stoppingMigration ? 'stopping' : migrationProcessedFiles === 0 ? 'starting' : 'running'}
          countLabel={migrationProcessedFiles === 0 ? '' : $t('settings.speakerAttributes.migrationProgress', { processed: migrationProcessedFiles, total: migrationTotalFiles })}
          percent={migrationProcessedFiles === 0 ? null : (migrationProcessedFiles / Math.max(migrationTotalFiles, 1)) * 100}
          etaSeconds={etaSeconds}
          remainingLabel={$t('common.remaining')}
          message={migrationProcessedFiles === 0 ? migrationProgressMessage || $t('settings.speakerAttributes.queued') : ''}
          barLabel={$t('settings.speakerAttributes.progressBar')}
          stopLabel={$t('settings.speakerAttributes.stopMigration')}
          onStop={stopMigration}
        >
          {#if migrationFailedFiles.length > 0}
            <div class="failed-info">
              {$t('settings.embeddingMigration.failedFiles', { count: migrationFailedFiles.length })}
            </div>
          {/if}
        </LiveProgressCard>
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

  .bulk-section {
    margin-top: var(--space-4);
  }

  .bulk-separator {
    border-top: 1px solid var(--border-color);
    margin-bottom: var(--space-4);
  }

  .bulk-title {
    font-size: 0.95rem;
    font-weight: var(--font-weight-semibold);
    color: var(--text-color);
    margin: 0 0 var(--space-1) 0;
  }

  .bulk-description {
    font-size: 0.8rem;
    color: var(--text-secondary);
    margin: 0 0 var(--space-4) 0;
    line-height: 1.4;
  }

  .pending-info {
    font-size: 0.85rem;
    color: var(--text-secondary);
    margin-bottom: var(--space-3);
    padding: var(--space-2-5) var(--space-3-5);
    background: rgba(var(--accent-rgb), 0.08);
    border: 1px solid rgba(var(--accent-rgb), 0.2);
    border-radius: var(--radius-md);
  }

  .all-processed {
    font-size: 0.85rem;
    color: var(--success-text);
    padding: var(--space-2-5) var(--space-3-5);
    background: rgba(81, 207, 102, 0.08);
    border: 1px solid rgba(81, 207, 102, 0.2);
    border-radius: var(--radius-md);
  }

  .progress-section {
    margin-top: var(--space-3);
  }

  .failed-info {
    font-size: var(--font-size-xs);
    color: var(--error-text);
    margin-top: var(--space-2);
  }

  .reextract-confirm {
    padding: var(--space-3-5);
    background: rgba(239, 68, 68, 0.08);
    border: 1px solid rgba(239, 68, 68, 0.2);
    border-radius: var(--radius-md);
  }

  .reextract-confirm p {
    font-size: 0.85rem;
    color: var(--text-color);
    margin: 0 0 var(--space-3) 0;
    line-height: 1.4;
  }

  .confirm-buttons {
    display: flex;
    gap: var(--space-2);
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
