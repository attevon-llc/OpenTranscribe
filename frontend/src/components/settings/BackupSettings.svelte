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
    type BackupSettings,
    type BackupStatus,
    type BackupFile,
    type DestinationStatus,
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

  // Status
  let destinationStatus: DestinationStatus | null = null;
  let pgDumpAvailable = true;
  let lastRunAt: string | null = null;
  let lastResult: BackupStatus['last_result'] = null;

  // Original values for dirty tracking
  let origEnabled = false;
  let origSchedule = '0 3 * * *';
  let origDestination = '/backups';
  let origRetentionDaily = 7;
  let origRetentionWeekly = 4;
  let origRetentionMonthly = 12;
  let origEncrypt = false;
  let origPassphraseFile = '';

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
    destinationStatus = cfg.destination_status;

    origEnabled = cfg.enabled;
    origSchedule = cfg.schedule;
    origDestination = cfg.destination;
    origRetentionDaily = cfg.retention_daily;
    origRetentionWeekly = cfg.retention_weekly;
    origRetentionMonthly = cfg.retention_monthly;
    origEncrypt = cfg.encrypt;
    origPassphraseFile = cfg.passphrase_file;

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
      pgDumpAvailable = st.pg_dump_available;
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
    } catch (err) {
      console.error('Error listing backups:', err);
    } finally {
      backupsLoading = false;
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
    hasChanges =
      enabled !== origEnabled ||
      schedule !== origSchedule ||
      destination !== origDestination ||
      retentionDaily !== origRetentionDaily ||
      retentionWeekly !== origRetentionWeekly ||
      retentionMonthly !== origRetentionMonthly ||
      encrypt !== origEncrypt ||
      passphraseFile !== origPassphraseFile;
    settingsModalStore.setDirty('backup', hasChanges);
  }

  $: mountOk = destinationStatus?.mounted ?? false;
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
    <!-- Mount status banner -->
    {#if !mountOk}
      <div class="banner banner-warning">
        {$t('settings.backup.mountMissing', { destination })}
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

    <!-- Config fields -->
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
    </div>

    <!-- Actions -->
    <div class="action-row">
      {#if !runNowPending}
        <button type="button" class="btn btn-secondary" on:click={() => (runNowPending = true)} disabled={runNowLoading || !mountOk}>
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
