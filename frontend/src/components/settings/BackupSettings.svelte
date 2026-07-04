<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import Spinner from '../ui/Spinner.svelte';
  import { toastStore } from '../../stores/toast';
  import { t } from '$stores/locale';
  import { settingsModalStore } from '../../stores/settingsModalStore';
  import {
    getBackupSettings,
    updateBackupSettings,
    getBackupStatus,
    runBackupNow,
    listBackups,
    testS3Connection,
    type BackupSettings,
    type BackupStatus,
    type BackupFile,
    type DestinationStatus,
    type S3Status,
    type BackupDestinationType,
  } from '$lib/api/backupApi';

  // ---- State ---------------------------------------------------------------

  let loading = true;
  let saving = false;
  let hasChanges = false;

  // Form fields
  let enabled = false;
  let schedule = '0 3 * * *';
  let destination = '/backups';
  let retentionDaily = 7;
  let retentionWeekly = 4;
  let retentionMonthly = 12;
  let encrypt = false;
  let passphraseFile = '';
  let includeOpensearch = false;

  // Destination type + S3 fields
  let destinationType: BackupDestinationType = 'local';
  let s3EndpointUrl = '';
  let s3Region = '';
  let s3Bucket = '';
  let s3Prefix = 'opentranscribe/';
  let s3AccessKeyId = '';
  let s3SecretKey = ''; // write-only; blank means "leave the stored secret unchanged"
  let s3SecretKeySet = false;

  // Status
  let destinationStatus: DestinationStatus | null = null;
  let s3Status: S3Status | null = null;
  let pgDumpAvailable = true;
  let lastRunAt: string | null = null;
  let lastResult: BackupStatus['last_result'] = null;
  let osSnapshotStatus: BackupStatus['opensearch_snapshot_status'] = null;

  // S3 connection-test state
  let s3TestLoading = false;
  let s3TestResult: { ok: boolean; error?: string | null } | null = null;

  // Original values for dirty tracking
  let origEnabled = false;
  let origSchedule = '0 3 * * *';
  let origDestination = '/backups';
  let origRetentionDaily = 7;
  let origRetentionWeekly = 4;
  let origRetentionMonthly = 12;
  let origEncrypt = false;
  let origPassphraseFile = '';
  let origIncludeOpensearch = false;
  let origDestinationType: BackupDestinationType = 'local';
  let origS3EndpointUrl = '';
  let origS3Region = '';
  let origS3Bucket = '';
  let origS3Prefix = 'opentranscribe/';
  let origS3AccessKeyId = '';

  // Run-now / list state
  let runNowPending = false;
  let runNowLoading = false;
  let statusLoading = false;
  let backups: BackupFile[] = [];
  let backupsLoading = false;

  // ---- Lifecycle -----------------------------------------------------------

  onMount(() => {
    (async () => {
      await loadSettings();
    })();
  });

  onDestroy(() => {
    settingsModalStore.clearDirty('backup');
  });

  // ---- API -----------------------------------------------------------------

  async function loadSettings() {
    loading = true;
    try {
      const cfg = await getBackupSettings();
      applySettings(cfg);
      await Promise.all([refreshStatus(false), loadBackups()]);
    } catch (err) {
      console.error('Error loading backup settings:', err);
      toastStore.error($t('settings.backup.loadFailed'));
    } finally {
      loading = false;
    }
  }

  function applySettings(cfg: BackupSettings) {
    enabled = cfg.enabled;
    schedule = cfg.schedule;
    destination = cfg.destination;
    retentionDaily = cfg.retention_daily;
    retentionWeekly = cfg.retention_weekly;
    retentionMonthly = cfg.retention_monthly;
    encrypt = cfg.encrypt;
    passphraseFile = cfg.passphrase_file;
    includeOpensearch = cfg.include_opensearch;
    destinationType = cfg.destination_type;
    s3EndpointUrl = cfg.s3_endpoint_url;
    s3Region = cfg.s3_region;
    s3Bucket = cfg.s3_bucket;
    s3Prefix = cfg.s3_prefix;
    s3AccessKeyId = cfg.s3_access_key_id;
    s3SecretKeySet = cfg.s3_secret_key_set;
    s3SecretKey = ''; // never populate the secret field from the server
    destinationStatus = cfg.destination_status;
    s3Status = cfg.s3_status ?? null;

    origEnabled = cfg.enabled;
    origSchedule = cfg.schedule;
    origDestination = cfg.destination;
    origRetentionDaily = cfg.retention_daily;
    origRetentionWeekly = cfg.retention_weekly;
    origRetentionMonthly = cfg.retention_monthly;
    origEncrypt = cfg.encrypt;
    origPassphraseFile = cfg.passphrase_file;
    origIncludeOpensearch = cfg.include_opensearch;
    origDestinationType = cfg.destination_type;
    origS3EndpointUrl = cfg.s3_endpoint_url;
    origS3Region = cfg.s3_region;
    origS3Bucket = cfg.s3_bucket;
    origS3Prefix = cfg.s3_prefix;
    origS3AccessKeyId = cfg.s3_access_key_id;

    hasChanges = false;
  }

  async function saveSettings() {
    saving = true;
    try {
      const cfg = await updateBackupSettings({
        enabled,
        schedule,
        destination,
        retention_daily: retentionDaily,
        retention_weekly: retentionWeekly,
        retention_monthly: retentionMonthly,
        encrypt,
        passphrase_file: passphraseFile,
        include_opensearch: includeOpensearch,
        destination_type: destinationType,
        s3_endpoint_url: s3EndpointUrl,
        s3_region: s3Region,
        s3_bucket: s3Bucket,
        s3_prefix: s3Prefix,
        s3_access_key_id: s3AccessKeyId,
        // Only send the secret when the admin actually typed one (keeps the stored value).
        ...(s3SecretKey ? { s3_secret_key: s3SecretKey } : {}),
      });
      applySettings(cfg);
      await refreshStatus(false);
      toastStore.success($t('settings.backup.saved'));
    } catch (err: unknown) {
      console.error('Error saving backup settings:', err);
      const detail = (err as { response?: { data?: { detail?: string } } }).response?.data?.detail;
      toastStore.error(detail || $t('settings.backup.saveFailed'));
    } finally {
      saving = false;
    }
  }

  async function refreshStatus(notify = true) {
    statusLoading = true;
    try {
      const st: BackupStatus = await getBackupStatus();
      lastRunAt = st.last_run_at ?? null;
      lastResult = st.last_result ?? null;
      destinationStatus = st.destination_status;
      s3Status = st.s3_status ?? null;
      pgDumpAvailable = st.pg_dump_available;
      osSnapshotStatus = st.opensearch_snapshot_status ?? null;
      if (notify) toastStore.success($t('settings.backup.statusRefreshed'));
    } catch (err) {
      console.error('Error refreshing backup status:', err);
      if (notify) toastStore.error($t('settings.backup.statusFailed'));
    } finally {
      statusLoading = false;
    }
  }

  async function loadBackups() {
    backupsLoading = true;
    try {
      const res = await listBackups();
      backups = res.backups;
      destinationStatus = res.destination_status;
      s3Status = res.s3_status ?? null;
    } catch (err) {
      console.error('Error listing backups:', err);
    } finally {
      backupsLoading = false;
    }
  }

  async function testS3() {
    s3TestLoading = true;
    s3TestResult = null;
    try {
      const res = await testS3Connection({
        s3_endpoint_url: s3EndpointUrl,
        s3_region: s3Region,
        s3_bucket: s3Bucket,
        s3_prefix: s3Prefix,
        s3_access_key_id: s3AccessKeyId,
        ...(s3SecretKey ? { s3_secret_key: s3SecretKey } : {}),
      });
      s3TestResult = { ok: res.ok, error: res.error };
      if (res.ok) {
        toastStore.success($t('settings.backup.s3TestOk'));
      } else {
        toastStore.error($t('settings.backup.s3TestFailed'));
      }
    } catch (err) {
      console.error('Error testing S3 connection:', err);
      s3TestResult = { ok: false, error: $t('settings.backup.s3TestFailed') };
      toastStore.error($t('settings.backup.s3TestFailed'));
    } finally {
      s3TestLoading = false;
    }
  }

  async function runNow() {
    runNowLoading = true;
    try {
      await runBackupNow();
      runNowPending = false;
      toastStore.success($t('settings.backup.runNowQueued'));
    } catch (err) {
      console.error('Error triggering backup:', err);
      toastStore.error($t('settings.backup.runNowFailed'));
    } finally {
      runNowLoading = false;
    }
  }

  // ---- Formatting ----------------------------------------------------------

  function formatBytes(bytes: number): string {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
    return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
  }

  function formatDate(iso: string | null | undefined): string {
    if (!iso) return $t('settings.backup.lastRunNever');
    return new Date(iso).toLocaleString();
  }

  // ---- Reactive change detection -------------------------------------------

  $: {
    enabled;
    schedule;
    destination;
    retentionDaily;
    retentionWeekly;
    retentionMonthly;
    encrypt;
    passphraseFile;
    includeOpensearch;
    destinationType;
    s3EndpointUrl;
    s3Region;
    s3Bucket;
    s3Prefix;
    s3AccessKeyId;
    s3SecretKey;
    hasChanges =
      enabled !== origEnabled ||
      schedule !== origSchedule ||
      destination !== origDestination ||
      retentionDaily !== origRetentionDaily ||
      retentionWeekly !== origRetentionWeekly ||
      retentionMonthly !== origRetentionMonthly ||
      encrypt !== origEncrypt ||
      passphraseFile !== origPassphraseFile ||
      includeOpensearch !== origIncludeOpensearch ||
      destinationType !== origDestinationType ||
      s3EndpointUrl !== origS3EndpointUrl ||
      s3Region !== origS3Region ||
      s3Bucket !== origS3Bucket ||
      s3Prefix !== origS3Prefix ||
      s3AccessKeyId !== origS3AccessKeyId ||
      s3SecretKey !== '';
    settingsModalStore.setDirty('backup', hasChanges);
  }

  $: isS3 = destinationType === 's3';
  $: mountOk = destinationStatus?.mounted ?? false;
  // The destination is "ready" (run-now allowed) when the active backend is usable.
  $: destinationReady = isS3 ? (s3Status?.reachable ?? false) : mountOk;
  $: saveDisabled = saving || !hasChanges;
</script>

<div class="backup-settings">
  <div class="title-row">
    <h3 class="section-title">{$t('settings.backup.title')}</h3>
  </div>
  <p class="section-desc">{$t('settings.backup.description')}</p>

  {#if loading}
    <div class="loading-state">
      <Spinner size="small" />
    </div>
  {:else}
    <!-- Mount status banner (local destination only) -->
    {#if !isS3 && !mountOk}
      <div class="banner banner-warning">
        {$t('settings.backup.mountMissing', { destination })}
      </div>
    {/if}
    {#if isS3 && s3Status && !s3Status.reachable}
      <div class="banner banner-warning">
        {$t('settings.backup.s3Unreachable')}{s3Status.error ? `: ${s3Status.error}` : ''}
      </div>
    {/if}
    {#if !pgDumpAvailable}
      <div class="banner banner-warning">{$t('settings.backup.pgDumpMissing')}</div>
    {/if}

    <!-- Enable toggle -->
    <div class="field-row">
      <label class="toggle-label">
        <input type="checkbox" class="toggle-input" bind:checked={enabled} />
        <span class="toggle-switch"></span>
        <span class="toggle-text">{$t('settings.backup.enableLabel')}</span>
      </label>
    </div>

    <!-- Schedule -->
    <div class="fields-grid">
      <div class="field-group">
        <label class="field-label" for="backup-schedule">{$t('settings.backup.schedule')}</label>
        <input
          id="backup-schedule"
          type="text"
          bind:value={schedule}
          class="form-input"
          placeholder="0 3 * * *"
          spellcheck="false"
        />
        <span class="field-hint">{$t('settings.backup.scheduleHint')}</span>
      </div>
    </div>

    <!-- Destination type -->
    <h4 class="subsection-title">{$t('settings.backup.destinationTitle')}</h4>
    <div class="field-group destination-type-group">
      <label class="field-label" for="backup-dest-type">{$t('settings.backup.destinationType')}</label>
      <select id="backup-dest-type" class="form-input" bind:value={destinationType}>
        <option value="local">{$t('settings.backup.destinationLocal')}</option>
        <option value="s3">{$t('settings.backup.destinationS3')}</option>
      </select>
    </div>

    {#if !isS3}
      <!-- Local destination -->
      <div class="fields-grid">
        <div class="field-group">
          <label class="field-label" for="backup-destination">{$t('settings.backup.destination')}</label>
          <input id="backup-destination" type="text" bind:value={destination} class="form-input" spellcheck="false" />
          <span class="field-hint">
            {#if mountOk}
              <span class="mount-ok">● {$t('settings.backup.mountOk')}</span>
            {:else}
              <span class="mount-bad">● {$t('settings.backup.mountNotReady')}</span>
            {/if}
          </span>
        </div>
      </div>
    {:else}
      <!-- S3-compatible destination -->
      <p class="subsection-desc">{$t('settings.backup.s3Hint')}</p>
      <div class="fields-grid">
        <div class="field-group">
          <label class="field-label" for="s3-endpoint">{$t('settings.backup.s3Endpoint')}</label>
          <input id="s3-endpoint" type="text" bind:value={s3EndpointUrl} class="form-input" placeholder="https://s3.amazonaws.com" spellcheck="false" />
          <span class="field-hint">{$t('settings.backup.s3EndpointHint')}</span>
        </div>
        <div class="field-group">
          <label class="field-label" for="s3-region">{$t('settings.backup.s3Region')}</label>
          <input id="s3-region" type="text" bind:value={s3Region} class="form-input" placeholder="us-east-1" spellcheck="false" />
        </div>
        <div class="field-group">
          <label class="field-label" for="s3-bucket">{$t('settings.backup.s3Bucket')}</label>
          <input id="s3-bucket" type="text" bind:value={s3Bucket} class="form-input" spellcheck="false" />
        </div>
        <div class="field-group">
          <label class="field-label" for="s3-prefix">{$t('settings.backup.s3Prefix')}</label>
          <input id="s3-prefix" type="text" bind:value={s3Prefix} class="form-input" placeholder="opentranscribe/" spellcheck="false" />
        </div>
        <div class="field-group">
          <label class="field-label" for="s3-access-key">{$t('settings.backup.s3AccessKey')}</label>
          <input id="s3-access-key" type="text" bind:value={s3AccessKeyId} class="form-input" autocomplete="off" spellcheck="false" />
        </div>
        <div class="field-group">
          <label class="field-label" for="s3-secret-key">{$t('settings.backup.s3SecretKey')}</label>
          <input
            id="s3-secret-key"
            type="password"
            bind:value={s3SecretKey}
            class="form-input"
            autocomplete="new-password"
            spellcheck="false"
            placeholder={s3SecretKeySet ? $t('settings.backup.s3SecretConfigured') : ''}
          />
          <span class="field-hint">
            {#if s3SecretKeySet}
              <span class="mount-ok">● {$t('settings.backup.s3SecretConfigured')}</span>
            {:else}
              <span class="mount-bad">● {$t('settings.backup.s3SecretNotSet')}</span>
            {/if}
          </span>
        </div>
      </div>
      <div class="action-row">
        <button type="button" class="btn btn-secondary" on:click={testS3} disabled={s3TestLoading || !s3Bucket}>
          {#if s3TestLoading}<Spinner size="small" />{/if}
          {$t('settings.backup.s3TestButton')}
        </button>
        {#if s3TestResult}
          {#if s3TestResult.ok}
            <span class="mount-ok">● {$t('settings.backup.s3TestOk')}</span>
          {:else}
            <span class="mount-bad" title={s3TestResult.error ?? ''}>● {$t('settings.backup.s3TestFailed')}</span>
          {/if}
        {/if}
      </div>
    {/if}

    <!-- Retention (GFS) -->
    <h4 class="subsection-title">{$t('settings.backup.retentionTitle')}</h4>
    <p class="subsection-desc">{$t('settings.backup.retentionHint')}</p>
    <div class="fields-grid">
      <div class="field-group">
        <label class="field-label" for="ret-daily">{$t('settings.backup.retentionDaily')}</label>
        <input id="ret-daily" type="number" bind:value={retentionDaily} min="0" max="3650" class="form-input number-input" />
      </div>
      <div class="field-group">
        <label class="field-label" for="ret-weekly">{$t('settings.backup.retentionWeekly')}</label>
        <input id="ret-weekly" type="number" bind:value={retentionWeekly} min="0" max="520" class="form-input number-input" />
      </div>
      <div class="field-group">
        <label class="field-label" for="ret-monthly">{$t('settings.backup.retentionMonthly')}</label>
        <input id="ret-monthly" type="number" bind:value={retentionMonthly} min="0" max="600" class="form-input number-input" />
      </div>
    </div>

    <!-- Encryption -->
    <div class="field-row encryption-row">
      <label class="toggle-label">
        <input type="checkbox" class="toggle-input" bind:checked={encrypt} />
        <span class="toggle-switch"></span>
        <span class="toggle-text">{$t('settings.backup.encryptLabel')}</span>
      </label>
    </div>
    {#if encrypt}
      <div class="field-group encryption-field">
        <label class="field-label" for="passphrase-file">{$t('settings.backup.passphraseFile')}</label>
        <input id="passphrase-file" type="text" bind:value={passphraseFile} class="form-input" placeholder="/backups/.passphrase" spellcheck="false" />
        <span class="field-hint">{$t('settings.backup.passphraseFileHint')}</span>
      </div>
    {/if}

    <!-- OpenSearch snapshot -->
    <div class="field-row opensearch-row">
      <label class="toggle-label">
        <input type="checkbox" class="toggle-input" bind:checked={includeOpensearch} />
        <span class="toggle-switch"></span>
        <span class="toggle-text">{$t('settings.backup.includeOpensearchLabel')}</span>
      </label>
      <span class="field-hint opensearch-hint">{$t('settings.backup.includeOpensearchHint')}</span>
    </div>
    {#if includeOpensearch && osSnapshotStatus}
      <div class="field-hint opensearch-status">
        {#if osSnapshotStatus.reachable && osSnapshotStatus.repository_registered}
          <span class="mount-ok">● {$t('settings.backup.opensearchReady')}</span>
        {:else if osSnapshotStatus.reachable}
          <span class="mount-bad">● {$t('settings.backup.opensearchRepoMissing')}</span>
        {:else}
          <span class="mount-bad">● {$t('settings.backup.opensearchUnreachable')}</span>
        {/if}
        {#if osSnapshotStatus.last_snapshot}
          <span class="status-value"> · {$t('settings.backup.opensearchLastSnapshot', { name: osSnapshotStatus.last_snapshot })}</span>
        {/if}
      </div>
    {/if}

    <!-- Status display -->
    <div class="status-block">
      <div class="status-item">
        <span class="status-label">{$t('settings.backup.lastRunSection')}:</span>
        <span class="status-value">{formatDate(lastRunAt)}</span>
        {#if lastResult}
          {#if lastResult.ok}
            <span class="status-badge ok">{$t('settings.backup.lastRunOk', { size: formatBytes(lastResult.size_bytes ?? 0), seconds: lastResult.duration_s ?? 0 })}</span>
          {:else}
            <span class="status-badge bad" title={lastResult.error ?? ''}>{$t('settings.backup.lastRunFailed')}</span>
          {/if}
        {/if}
      </div>
      {#if lastResult && !lastResult.ok && lastResult.error}
        <div class="status-error">{lastResult.error}</div>
      {/if}
      {#if lastResult?.opensearch}
        <div class="status-item">
          <span class="status-label">{$t('settings.backup.opensearchSnapshotSection')}:</span>
          {#if lastResult.opensearch.status === 'ok'}
            <span class="status-badge ok">{lastResult.opensearch.snapshot}</span>
          {:else if lastResult.opensearch.status === 'skipped'}
            <span class="status-value">{$t('settings.backup.opensearchSkipped')}</span>
          {:else}
            <span class="status-badge bad" title={lastResult.opensearch.error ?? ''}>{$t('settings.backup.opensearchSnapshotFailed')}</span>
          {/if}
        </div>
        {#if lastResult.opensearch.status !== 'ok' && lastResult.opensearch.status !== 'skipped' && lastResult.opensearch.error}
          <div class="status-error">{lastResult.opensearch.error}</div>
        {/if}
      {/if}
      {#if lastResult?.prune_error}
        <div class="status-item">
          <span class="status-label">{$t('settings.backup.pruneSection')}:</span>
          <span class="status-badge bad" title={lastResult.prune_error}>{$t('settings.backup.pruneFailed')}</span>
        </div>
        <div class="status-error">{lastResult.prune_error}</div>
      {/if}
      {#if lastResult?.recovery}
        <div class="status-item">
          <span class="status-label">{$t('settings.backup.recoverySection')}:</span>
          {#if lastResult.recovery.status === 'keys_included'}
            <span class="status-badge ok">{$t('settings.backup.recoveryKeysIncluded')}</span>
          {:else if lastResult.recovery.status === 'readme_written'}
            <span class="status-value">{$t('settings.backup.recoveryReadmeWritten')}</span>
          {:else}
            <span class="status-badge bad" title={lastResult.recovery.error ?? ''}>{$t('settings.backup.recoveryFailed')}</span>
          {/if}
        </div>
        {#if lastResult.recovery.status === 'readme_written'}
          <div class="field-hint">{$t('settings.backup.recoveryKeysHint')}</div>
        {/if}
        {#if lastResult.recovery.status === 'error' && lastResult.recovery.error}
          <div class="status-error">{lastResult.recovery.error}</div>
        {/if}
      {/if}
    </div>

    <!-- Actions -->
    <div class="action-row">
      {#if !runNowPending}
        <button type="button" class="btn btn-secondary" on:click={() => (runNowPending = true)} disabled={runNowLoading || !destinationReady}>
          {$t('settings.backup.runNowButton')}
        </button>
      {:else}
        <span class="run-confirm-text">{$t('settings.backup.runNowConfirmBody')}</span>
        <button type="button" class="btn btn-secondary" on:click={() => (runNowPending = false)}>
          {$t('settings.backup.cancel')}
        </button>
        <button type="button" class="btn btn-primary" on:click={runNow} disabled={runNowLoading}>
          {#if runNowLoading}<Spinner size="small" />{/if}
          {$t('settings.backup.runNowButton')}
        </button>
      {/if}
      <button type="button" class="btn btn-link" on:click={() => refreshStatus(true)} disabled={statusLoading}>
        {statusLoading ? $t('settings.backup.refreshingStatus') : $t('settings.backup.refreshStatus')}
      </button>
    </div>

    <!-- Existing backups -->
    <h4 class="subsection-title">{$t('settings.backup.existingTitle')}</h4>
    {#if backupsLoading}
      <div class="loading-state"><Spinner size="small" /></div>
    {:else if backups.length === 0}
      <p class="empty-note">{$t('settings.backup.existingEmpty')}</p>
    {:else}
      <div class="preview-table-wrap">
        <table class="preview-table">
          <thead>
            <tr>
              <th>{$t('settings.backup.columns.filename')}</th>
              <th>{$t('settings.backup.columns.created')}</th>
              <th>{$t('settings.backup.columns.size')}</th>
              <th>{$t('settings.backup.columns.encrypted')}</th>
            </tr>
          </thead>
          <tbody>
            {#each backups as file}
              <tr>
                <td class="col-title">{file.filename}</td>
                <td>{formatDate(file.created_at)}</td>
                <td>{formatBytes(file.size_bytes)}</td>
                <td>{file.encrypted ? $t('settings.backup.yes') : $t('settings.backup.no')}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}

    <!-- Save / Reset -->
    <div class="button-row">
      <button type="button" class="btn btn-secondary" on:click={loadSettings} disabled={saving}>
        {$t('settings.backup.resetButton')}
      </button>
      <button type="button" class="btn btn-primary" on:click={saveSettings} disabled={saveDisabled}>
        {saving ? $t('settings.backup.savingButton') : $t('settings.backup.saveButton')}
      </button>
    </div>
  {/if}
</div>

<style>
  .backup-settings {
    padding: 0.5rem 0;
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

  .subsection-title {
    font-size: 0.85rem;
    font-weight: 600;
    margin: 1.25rem 0 0.25rem 0;
    color: var(--text-color);
  }

  .section-desc,
  .subsection-desc {
    font-size: 0.8rem;
    color: var(--text-muted);
    margin: 0.25rem 0 1rem 0;
  }

  .subsection-desc {
    margin: 0 0 0.75rem 0;
  }

  .loading-state {
    display: flex;
    align-items: center;
    padding: 1rem;
  }

  .banner {
    border-radius: 6px;
    padding: 0.6rem 0.75rem;
    margin-bottom: 0.75rem;
    font-size: 0.82rem;
    line-height: 1.4;
  }

  .banner-warning {
    border: 1px solid var(--warning-color, #f59e0b);
    background-color: var(--warning-bg, rgba(245, 158, 11, 0.08));
    color: var(--warning-color, #b45309);
  }

  .field-row {
    margin-bottom: 0.75rem;
  }

  .encryption-row {
    margin-top: 1rem;
  }

  .opensearch-row {
    margin-top: 1rem;
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
    align-items: flex-start;
  }

  .opensearch-hint {
    margin-left: 2.75rem;
  }

  .opensearch-status {
    margin: 0.25rem 0 0.25rem 2.75rem;
  }

  .fields-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
    gap: 0.75rem 1.25rem;
    margin-top: 0.75rem;
    margin-bottom: 0.75rem;
  }

  .field-group {
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
  }

  .encryption-field {
    max-width: 400px;
  }

  .destination-type-group {
    max-width: 280px;
    margin: 0.5rem 0 0.75rem 0;
  }

  select.form-input {
    cursor: pointer;
  }

  .field-label {
    font-size: 0.78rem;
    color: var(--text-muted);
    font-weight: 500;
  }

  .field-hint {
    font-size: 0.72rem;
    color: var(--text-muted);
  }

  .mount-ok {
    color: var(--success-color, #16a34a);
  }

  .mount-bad {
    color: var(--warning-color, #b45309);
  }

  .toggle-label {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    cursor: pointer;
    user-select: none;
  }

  .toggle-input {
    position: absolute;
    opacity: 0;
    width: 0;
    height: 0;
  }

  .toggle-switch {
    position: relative;
    width: 36px;
    height: 20px;
    background-color: var(--border-color);
    border-radius: 10px;
    transition: background-color 0.2s ease;
    flex-shrink: 0;
  }

  .toggle-switch::after {
    content: '';
    position: absolute;
    top: 2px;
    left: 2px;
    width: 16px;
    height: 16px;
    background-color: white;
    border-radius: 50%;
    transition: transform 0.2s ease;
    box-shadow: 0 1px 2px rgba(0, 0, 0, 0.2);
  }

  .toggle-input:checked + .toggle-switch {
    background-color: var(--primary-color);
  }

  .toggle-input:checked + .toggle-switch::after {
    transform: translateX(16px);
  }

  .toggle-text {
    font-size: 0.875rem;
    color: var(--text-color);
  }

  .form-input {
    padding: 0.375rem 0.5rem;
    border: 1px solid var(--border-color);
    border-radius: 4px;
    background-color: var(--background-color);
    color: var(--text-color);
    font-size: 0.875rem;
  }

  .form-input:focus {
    outline: none;
    border-color: var(--primary-color);
  }

  .number-input {
    width: 90px;
    text-align: center;
  }

  /* Status */
  .status-block {
    display: flex;
    flex-direction: column;
    gap: 0.3rem;
    margin: 1rem 0 0.75rem 0;
    padding: 0.6rem 0.75rem;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    background-color: var(--background-secondary, rgba(0, 0, 0, 0.03));
    font-size: 0.8rem;
  }

  .status-item {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    flex-wrap: wrap;
  }

  .status-label {
    color: var(--text-muted);
    font-weight: 500;
  }

  .status-value {
    color: var(--text-color);
  }

  .status-badge {
    border-radius: 10px;
    padding: 0 0.5rem;
    font-size: 0.72rem;
    line-height: 1.6;
    color: white;
  }

  .status-badge.ok {
    background-color: var(--success-color, #16a34a);
  }

  .status-badge.bad {
    background-color: var(--error-color, #ef4444);
  }

  .status-error {
    font-size: 0.75rem;
    color: var(--error-color, #ef4444);
    word-break: break-word;
  }

  /* Actions */
  .action-row {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex-wrap: wrap;
    margin-bottom: 0.75rem;
  }

  .run-confirm-text {
    font-size: 0.8rem;
    color: var(--text-muted);
  }

  .empty-note {
    font-size: 0.82rem;
    color: var(--text-muted);
    margin: 0.25rem 0 0.75rem 0;
  }

  /* Table */
  .preview-table-wrap {
    overflow-x: auto;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    margin-bottom: 0.75rem;
  }

  .preview-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.78rem;
  }

  .preview-table th {
    background-color: var(--background-secondary, rgba(0, 0, 0, 0.04));
    color: var(--text-muted);
    font-weight: 600;
    text-align: left;
    padding: 0.35rem 0.6rem;
    border-bottom: 1px solid var(--border-color);
    white-space: nowrap;
  }

  .preview-table td {
    padding: 0.3rem 0.6rem;
    border-bottom: 1px solid var(--border-color);
    color: var(--text-color);
    vertical-align: middle;
  }

  .preview-table tr:last-child td {
    border-bottom: none;
  }

  .col-title {
    max-width: 260px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  /* Button row */
  .button-row {
    display: flex;
    gap: 0.5rem;
    justify-content: flex-end;
    margin-top: 0.5rem;
  }

  .button-row .btn-secondary {
    margin-right: auto;
  }

  .btn-link {
    background-color: var(--surface-color);
    border: 1px solid var(--border-color);
    color: var(--text-color);
    padding: 0.375rem 0.625rem;
    border-radius: 4px;
    cursor: pointer;
    font-size: 0.8rem;
    font-weight: 500;
    transition: all 0.2s ease;
  }

  .btn-link:hover:not(:disabled) {
    background-color: var(--button-hover);
  }

  @media (max-width: 768px) {
    .fields-grid {
      grid-template-columns: 1fr;
    }

    .action-row {
      flex-direction: column;
      align-items: stretch;
    }

    .action-row .btn {
      width: 100%;
      min-height: 44px;
    }

    .button-row {
      flex-wrap: wrap;
    }

    .button-row .btn-secondary {
      margin-right: 0;
    }

    .button-row .btn {
      flex: 1;
      min-height: 44px;
    }
  }
</style>
