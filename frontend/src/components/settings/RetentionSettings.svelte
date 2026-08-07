<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import Spinner from '../ui/Spinner.svelte';
  import CacheSettings from './CacheSettings.svelte';
  import axiosInstance from '$lib/axios';
  import { toastStore } from '../../stores/toast';
  import { t } from '$stores/locale';
  import { settingsModalStore } from '../../stores/settingsModalStore';

  // ---- Types ---------------------------------------------------------------

  interface RetentionConfig {
    retention_enabled: boolean;
    retention_days: number;
    run_time: string;
    timezone: string;
    delete_error_files: boolean;
    last_run: string | null;
    last_run_deleted: number | null;
  }

  interface PreviewFile {
    uuid: string;
    title: string;
    owner_email: string;
    completed_at: string | null;
    age_days: number;
    size_bytes: number;
    status: string;
  }

  interface PreviewResult {
    file_count: number;
    total_size_bytes: number;
    files: PreviewFile[];
  }

  // ---- Constants -----------------------------------------------------------

  const TIMEZONES = [
    'UTC',
    'America/New_York',
    'America/Chicago',
    'America/Denver',
    'America/Los_Angeles',
    'America/Sao_Paulo',
    'Europe/London',
    'Europe/Paris',
    'Europe/Berlin',
    'Europe/Amsterdam',
    'Europe/Moscow',
    'Asia/Tokyo',
    'Asia/Shanghai',
    'Asia/Singapore',
    'Asia/Kolkata',
    'Australia/Sydney',
    'Pacific/Auckland',
  ];

  const BASE = '/admin/settings/retention-config';

  // ---- State ---------------------------------------------------------------

  let loading = true;
  let saving = false;
  let hasChanges = false;

  // Form fields
  let retentionEnabled = false;
  let retentionDays = 365;
  let runTime = '02:00';
  let timezone = 'UTC';
  let deleteErrorFiles = false;

  // Status
  let lastRun: string | null = null;
  let lastRunDeleted: number | null = null;

  // Original values for dirty tracking
  let origEnabled = false;
  let origDays = 365;
  let origRunTime = '02:00';
  let origTimezone = 'UTC';
  let origDeleteError = false;

  // Enable confirmation flow
  let showEnableConfirm = false;
  let enableConfirmChecked = false;
  let enableConfirmed = false;

  // Preview state
  let previewLoading = false;
  let previewResult: PreviewResult | null = null;

  // Run-now state
  let runNowPending = false;
  let runNowLoading = false;
  let runNowTaskId: string | null = null;
  let statusLoading = false;

  // ---- Lifecycle -----------------------------------------------------------

  onMount(async () => {
    await loadConfig();
  });

  onDestroy(() => {
    settingsModalStore.clearDirty('retention');
  });

  // ---- API -----------------------------------------------------------------

  async function loadConfig() {
    loading = true;
    try {
      const res = await axiosInstance.get<RetentionConfig>(BASE);
      applyConfig(res.data);
    } catch (err: unknown) {
      console.error('Error loading retention config:', err);
      toastStore.error($t('settings.retention.loadFailed'));
    } finally {
      loading = false;
    }
  }

  function applyConfig(cfg: RetentionConfig) {
    retentionEnabled = cfg.retention_enabled;
    retentionDays = cfg.retention_days;
    runTime = cfg.run_time;
    timezone = cfg.timezone;
    deleteErrorFiles = cfg.delete_error_files;
    lastRun = cfg.last_run;
    lastRunDeleted = cfg.last_run_deleted;

    origEnabled = cfg.retention_enabled;
    origDays = cfg.retention_days;
    origRunTime = cfg.run_time;
    origTimezone = cfg.timezone;
    origDeleteError = cfg.delete_error_files;

    hasChanges = false;
    showEnableConfirm = false;
    enableConfirmChecked = false;
    enableConfirmed = false;
  }

  async function saveConfig() {
    saving = true;
    try {
      const res = await axiosInstance.put<RetentionConfig>(BASE, {
        retention_enabled: retentionEnabled,
        retention_days: retentionDays,
        run_time: runTime,
        timezone,
        delete_error_files: deleteErrorFiles,
      });
      applyConfig(res.data);
      toastStore.success($t('settings.retention.saved'));
    } catch (err: unknown) {
      console.error('Error saving retention config:', err);
      const detail = (err as { response?: { data?: { detail?: string } } }).response?.data?.detail;
      toastStore.error(detail || $t('settings.retention.saveFailed'));
    } finally {
      saving = false;
    }
  }

  async function loadPreview() {
    previewLoading = true;
    previewResult = null;
    try {
      const res = await axiosInstance.get<PreviewResult>(
        `${BASE}/preview`,
        { params: { retention_days: retentionDays, delete_error_files: deleteErrorFiles } }
      );
      previewResult = res.data;
    } catch (err: unknown) {
      console.error('Error loading preview:', err);
      toastStore.error($t('settings.retention.previewFailed'));
    } finally {
      previewLoading = false;
    }
  }

  async function runNow() {
    runNowLoading = true;
    runNowTaskId = null;
    try {
      const res = await axiosInstance.post<{ task_id: string }>(`${BASE}/run`);
      runNowTaskId = res.data.task_id;
      runNowPending = false;
      toastStore.success($t('settings.retention.runNowQueued'));
    } catch (err: unknown) {
      console.error('Error triggering retention run:', err);
      toastStore.error($t('settings.retention.runNowFailed'));
    } finally {
      runNowLoading = false;
    }
  }

  async function refreshStatus() {
    statusLoading = true;
    try {
      const res = await axiosInstance.get<RetentionConfig>(`${BASE}/status`);
      lastRun = res.data.last_run;
      lastRunDeleted = res.data.last_run_deleted;
      toastStore.success($t('settings.retention.statusRefreshed'));
    } catch (err: unknown) {
      console.error('Error refreshing status:', err);
      toastStore.error($t('settings.retention.statusFailed'));
    } finally {
      statusLoading = false;
    }
  }

  // ---- Handlers ------------------------------------------------------------

  function handleEnableToggle() {
    if (retentionEnabled && !enableConfirmed) {
      // Turning ON without prior confirmation — show inline confirm block
      showEnableConfirm = true;
      enableConfirmChecked = false;
    } else if (!retentionEnabled) {
      // Turning OFF is always fine
      showEnableConfirm = false;
      enableConfirmed = false;
      enableConfirmChecked = false;
    }
  }

  function confirmEnable() {
    enableConfirmed = true;
    showEnableConfirm = false;
    retentionEnabled = true;
  }

  function cancelEnable() {
    retentionEnabled = false;
    showEnableConfirm = false;
    enableConfirmChecked = false;
    enableConfirmed = false;
  }

  function formatBytes(bytes: number): string {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
    return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
  }

  function formatDate(iso: string | null): string {
    if (!iso) return $t('settings.retention.lastRunNever');
    return new Date(iso).toLocaleString();
  }

  // ---- Reactive change detection -------------------------------------------

  // The nested cache panel has no section id of its own, so its unsaved state is
  // folded in here — otherwise closing the modal would skip the confirmation
  // prompt for a pending cache change.
  let cacheDirty = false;

  $: {
    retentionEnabled;
    retentionDays;
    runTime;
    timezone;
    deleteErrorFiles;
    hasChanges =
      retentionEnabled !== origEnabled ||
      retentionDays !== origDays ||
      runTime !== origRunTime ||
      timezone !== origTimezone ||
      deleteErrorFiles !== origDeleteError;
    settingsModalStore.setDirty('retention', hasChanges || cacheDirty);
  }

  $: saveDisabled = saving || !hasChanges || (retentionEnabled && showEnableConfirm);
</script>

<div class="retention-settings">
  <div class="title-row">
    <h3 class="section-title">{$t('settings.retention.title')}</h3>
  </div>
  <p class="section-desc warning-banner">{$t('settings.retention.warningBanner')}</p>

  {#if loading}
    <div class="loading-state">
      <Spinner size="small" />
    </div>
  {:else}
    <!-- Enable toggle -->
    <div class="field-row">
      <label class="toggle-label">
        <input
          type="checkbox"
          class="toggle-input"
          bind:checked={retentionEnabled}
          on:change={handleEnableToggle}
        />
        <span class="toggle-switch"></span>
        <span class="toggle-text">{$t('settings.retention.enableLabel')}</span>
      </label>
    </div>

    <!-- Opt-in confirmation block -->
    {#if showEnableConfirm}
      <div class="confirm-block">
        <p class="confirm-warning">
          {$t('settings.retention.confirmEnableBody', { days: retentionDays })}
        </p>
        <label class="confirm-check-label">
          <input type="checkbox" bind:checked={enableConfirmChecked} />
          <span>{$t('settings.retention.confirmEnableCheckbox')}</span>
        </label>
        <div class="confirm-actions">
          <button type="button" class="btn btn-secondary" on:click={cancelEnable}>
            {$t('settings.retention.cancel')}
          </button>
          <button
            type="button"
            class="btn btn-danger"
            disabled={!enableConfirmChecked}
            on:click={confirmEnable}
          >
            {$t('settings.retention.confirmEnableConfirm')}
          </button>
        </div>
      </div>
    {/if}

    <!-- Config fields (always visible) -->
    <div class="fields-grid">
      <div class="field-group">
        <label class="field-label" for="retention-days">{$t('settings.retention.retentionDays')}</label>
        <div class="inline-input">
          <input
            id="retention-days"
            type="number"
            bind:value={retentionDays}
            min="1"
            max="3650"
            class="form-input number-input"
            disabled={!retentionEnabled}
          />
          <span class="input-suffix">{$t('settings.retention.daysUnit')}</span>
        </div>
      </div>

      <div class="field-group">
        <label class="field-label" for="run-time">{$t('settings.retention.runTime')}</label>
        <input
          id="run-time"
          type="time"
          bind:value={runTime}
          class="form-input"
          disabled={!retentionEnabled}
        />
      </div>

      <div class="field-group">
        <label class="field-label" for="timezone">{$t('settings.retention.timezone')}</label>
        <select id="timezone" bind:value={timezone} class="form-input" disabled={!retentionEnabled}>
          {#each TIMEZONES as tz}
            <option value={tz}>{tz}</option>
          {/each}
        </select>
      </div>

      <div class="field-group toggle-field">
        <label class="toggle-label">
          <input type="checkbox" class="toggle-input" bind:checked={deleteErrorFiles} disabled={!retentionEnabled} />
          <span class="toggle-switch"></span>
          <span class="toggle-text">{$t('settings.retention.deleteErrorFiles')}</span>
        </label>
      </div>
    </div>

    <!-- Status display -->
    <div class="status-block">
      <div class="status-item">
        <span class="status-label">{$t('settings.retention.lastRunSection')}:</span>
        <span class="status-value">{formatDate(lastRun)}</span>
        {#if lastRun && lastRunDeleted !== null}
          <span class="status-badge">{$t('settings.retention.lastRunLabel', { datetime: formatDate(lastRun), count: lastRunDeleted })}</span>
        {/if}
      </div>
      <div class="status-item">
        <span class="status-label">{$t('settings.retention.nextRunSection')}:</span>
        <span class="status-value">
          {retentionEnabled
            ? $t('settings.retention.nextRunLabel', { time: runTime, timezone })
            : $t('settings.retention.disabledStatus')}
        </span>
      </div>
    </div>

    <!-- Preview section -->
    <div class="action-row">
      <button
        type="button"
        class="btn btn-secondary"
        on:click={loadPreview}
        disabled={previewLoading}
      >
        {#if previewLoading}
          <Spinner size="small" />
        {/if}
        {$t('settings.retention.previewButton')}
      </button>

      <!-- Run now -->
      {#if !runNowPending}
        <button
          type="button"
          class="btn btn-secondary btn-danger-outline"
          on:click={() => { runNowPending = true; }}
          disabled={runNowLoading}
        >
          {$t('settings.retention.runNowButton')}
        </button>
      {:else}
        <span class="run-confirm-text">{$t('settings.retention.runNowConfirmBody', { days: retentionDays })}</span>
        <button type="button" class="btn btn-secondary" on:click={() => { runNowPending = false; }}>
          {$t('settings.retention.cancel')}
        </button>
        <button
          type="button"
          class="btn btn-danger"
          on:click={runNow}
          disabled={runNowLoading}
        >
          {#if runNowLoading}<Spinner size="small" />{/if}
          {$t('settings.retention.runNowButton')}
        </button>
      {/if}

      {#if runNowTaskId}
        <button type="button" class="btn btn-link" on:click={refreshStatus} disabled={statusLoading}>
          {statusLoading ? $t('settings.retention.refreshingStatus') : $t('settings.retention.refreshStatus')}
        </button>
      {/if}
    </div>

    <!-- Preview results -->
    {#if previewResult !== null}
      <div class="preview-block">
        {#if previewResult.file_count === 0}
          <p class="preview-empty">{$t('settings.retention.previewEmpty')}</p>
        {:else}
          <p class="preview-note">{$t('settings.retention.previewNote')}</p>
          <p class="preview-summary">
            {$t('settings.retention.previewCount', { count: previewResult.file_count, size: formatBytes(previewResult.total_size_bytes) })}
            {#if previewResult.file_count > previewResult.files.length}
              &nbsp;{$t('settings.retention.previewMore', { count: previewResult.file_count - previewResult.files.length })}
            {/if}
          </p>
          <div class="preview-table-wrap">
            <table class="preview-table">
              <thead>
                <tr>
                  <th>{$t('settings.retention.columns.title')}</th>
                  <th>{$t('settings.retention.columns.owner')}</th>
                  <th>{$t('settings.retention.columns.completed')}</th>
                  <th>{$t('settings.retention.columns.age')}</th>
                  <th>{$t('settings.retention.columns.size')}</th>
                  <th>{$t('settings.retention.columns.status')}</th>
                </tr>
              </thead>
              <tbody>
                {#each previewResult.files as file}
                  <tr>
                    <td class="col-title">{file.title}</td>
                    <td>{file.owner_email}</td>
                    <td>{formatDate(file.completed_at)}</td>
                    <td>{file.age_days}d</td>
                    <td>{formatBytes(file.size_bytes)}</td>
                    <td>{file.status}</td>
                  </tr>
                {/each}
              </tbody>
            </table>
          </div>
        {/if}
      </div>
    {/if}

    <!-- Save / Reset buttons -->
    <div class="button-row">
      <button
        type="button"
        class="btn btn-secondary"
        on:click={loadConfig}
        disabled={saving}
      >
        {$t('settings.retention.resetButton')}
      </button>
      <button
        type="button"
        class="btn btn-primary"
        on:click={saveConfig}
        disabled={saveDisabled}
      >
        {saving ? $t('settings.retention.savingButton') : $t('settings.retention.saveButton')}
      </button>
    </div>

    <!-- Derived media cache (regenerable duplicates of originals) -->
    <hr class="section-divider" />
    <CacheSettings on:dirty={(e) => (cacheDirty = e.detail)} />
  {/if}
</div>

<style>
  .retention-settings {
    padding: 0.5rem 0;
  }

  .section-divider {
    border: none;
    border-top: 1px solid var(--border-color);
    margin: 1.5rem 0 0.5rem 0;
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

  .loading-state {
    display: flex;
    align-items: center;
    padding: 1rem;
  }

  .field-row {
    margin-bottom: 0.75rem;
  }

  .fields-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
    gap: 0.75rem 1.25rem;
    margin-top: 0.75rem;
    margin-bottom: 0.75rem;
  }

  .field-group {
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
  }

  .toggle-field {
    justify-content: flex-end;
  }

  .field-label {
    font-size: 0.78rem;
    color: var(--text-muted);
    font-weight: 500;
  }

  .inline-input {
    display: flex;
    align-items: center;
    gap: 0.4rem;
  }

  .input-suffix {
    font-size: 0.75rem;
    color: var(--text-muted);
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
    background-color: #3b82f6;
  }

  .toggle-input:checked + .toggle-switch::after {
    transform: translateX(16px);
  }

  .toggle-input:disabled + .toggle-switch {
    opacity: 0.5;
    cursor: not-allowed;
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

  .form-input:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  select.form-input {
    width: 100%;
  }

  .number-input {
    width: 70px;
    text-align: center;
  }

  /* Confirmation block */
  .confirm-block {
    border: 1px solid var(--warning-color, #f59e0b);
    border-radius: 6px;
    padding: 0.75rem 1rem;
    margin: 0.5rem 0 0.75rem 0;
    background-color: var(--warning-bg, rgba(245, 158, 11, 0.08));
  }

  .confirm-warning {
    font-size: 0.82rem;
    color: var(--warning-color, #b45309);
    margin: 0 0 0.5rem 0;
    line-height: 1.4;
  }

  .confirm-check-label {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    font-size: 0.82rem;
    color: var(--text-color);
    cursor: pointer;
    margin-bottom: 0.65rem;
    user-select: none;
  }

  .confirm-check-label input[type='checkbox'] {
    appearance: none;
    -webkit-appearance: none;
    width: 16px;
    height: 16px;
    min-width: 16px;
    border: 2px solid var(--border-color);
    border-radius: 3px;
    background-color: var(--background-color);
    cursor: pointer;
    position: relative;
    transition: background-color 0.15s ease, border-color 0.15s ease;
  }

  .confirm-check-label input[type='checkbox']:checked {
    background-color: #3b82f6;
    border-color: var(--primary-color);
  }

  .confirm-check-label input[type='checkbox']:checked::after {
    content: '';
    position: absolute;
    left: 3px;
    top: 0px;
    width: 5px;
    height: 9px;
    border: 2px solid white;
    border-top: none;
    border-left: none;
    transform: rotate(45deg);
  }

  .confirm-check-label input[type='checkbox']:focus {
    outline: 2px solid var(--primary-color);
    outline-offset: 1px;
  }

  .confirm-actions {
    display: flex;
    gap: 0.5rem;
  }

  /* Status */
  .status-block {
    display: flex;
    flex-direction: column;
    gap: 0.3rem;
    margin: 0.75rem 0;
    padding: 0.6rem 0.75rem;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    background-color: var(--background-secondary, rgba(0,0,0,0.03));
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
    background-color: #3b82f6;
    color: white;
    border-radius: 10px;
    padding: 0 0.5rem;
    font-size: 0.72rem;
    line-height: 1.6;
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

  /* Preview */
  .preview-block {
    margin-bottom: 0.75rem;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    overflow: hidden;
  }

  .preview-empty {
    padding: 0.75rem 1rem;
    font-size: 0.82rem;
    color: var(--text-muted);
    margin: 0;
  }

  .preview-summary {
    padding: 0.5rem 0.75rem;
    font-size: 0.8rem;
    color: var(--text-muted);
    border-bottom: 1px solid var(--border-color);
    margin: 0;
    background-color: var(--background-secondary, rgba(0,0,0,0.02));
  }

  .preview-table-wrap {
    overflow-x: auto;
  }

  .preview-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.78rem;
  }

  .preview-table th {
    background-color: var(--background-secondary, rgba(0,0,0,0.04));
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
    max-width: 200px;
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

  .btn-danger-outline {
    background-color: #ef4444;
    color: white;
    border: none;
    box-shadow: 0 2px 4px rgba(239, 68, 68, 0.2);
  }

  .btn-danger-outline:hover:not(:disabled) {
    background-color: #dc2626;
    transform: scale(1.02);
    box-shadow: 0 4px 8px rgba(239, 68, 68, 0.25);
  }

  .btn-danger-outline:active:not(:disabled) {
    transform: scale(1);
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
    transform: scale(1.02);
    box-shadow: 0 4px 8px rgba(0, 0, 0, 0.1);
  }

  .btn-link:active:not(:disabled) {
    transform: scale(1);
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

    .run-confirm-text {
      text-align: center;
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

    .preview-table-wrap {
      overflow-x: auto;
      -webkit-overflow-scrolling: touch;
    }
  }
</style>
