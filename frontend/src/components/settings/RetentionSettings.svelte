<script lang="ts">
  import Toggle from '$components/ui/Toggle.svelte';
  import { formatDateTime, formatFileSize } from '$lib/utils/formatting';
  import { onMount, onDestroy } from 'svelte';
  import Spinner from '$components/ui/Spinner.svelte';
  import SettingsSkeleton from '$components/ui/SettingsSkeleton.svelte';
  import TrashRetentionSetting from '$components/settings/TrashRetentionSetting.svelte';
  import axiosInstance from '$lib/axios';
  import {
    loadSectionData,
    peekSectionData,
    sameSectionData,
    setSectionData,
    SectionDataKey,
    fetchRetentionConfig,
  } from '$lib/settings/sectionData';
  import { toastStore } from '$stores/toast';
  import { t, locale } from '$stores/locale';
  import { settingsModalStore } from '$stores/settingsModalStore';
  import Badge from '$components/ui/Badge.svelte';

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

  // Kept as a literal: backend/tests/unit/test_route_has_a_caller.py finds a route's frontend
  // caller by scanning for its URL string, which an imported constant would hide.
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
    // Stale-while-revalidate: show the last seen settings at once, refresh behind them.
    const cached = peekSectionData<RetentionConfig>(SectionDataKey.retention);
    if (cached) {
      applyConfig(cached);
      loading = false;
    } else loading = true;
    try {
      const fresh = await loadSectionData(SectionDataKey.retention, () =>
        fetchRetentionConfig<RetentionConfig>()
      );
      // Never overwrite edits made while the refresh was in flight.
      if (!cached || (!hasChanges && !sameSectionData(fresh, cached))) applyConfig(fresh);
    } catch (err: unknown) {
      console.error('Error loading retention config:', err);
      if (!cached) toastStore.error($t('settings.retention.loadFailed'));
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
      setSectionData(SectionDataKey.retention, res.data);
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

  // ---- Reactive change detection -------------------------------------------

  $: {
    hasChanges =
      retentionEnabled !== origEnabled ||
      retentionDays !== origDays ||
      runTime !== origRunTime ||
      timezone !== origTimezone ||
      deleteErrorFiles !== origDeleteError;
    settingsModalStore.setDirty('retention', hasChanges);
  }

  $: saveDisabled = saving || !hasChanges || (retentionEnabled && showEnableConfirm);
</script>

<div class="retention-settings ops-panel">
  <div class="title-row">
    <h3 class="section-title">{$t('settings.retention.title')}</h3>
  </div>
  <p class="section-desc warning-banner">{$t('settings.retention.warningBanner')}</p>

  {#if loading}
    <SettingsSkeleton variant="form" count={3} label={$t('common.loading')} />
  {:else}
    <!-- Enable toggle -->
    <div class="field-row">
      <div class="toggle-label">
        <Toggle id="retentionsettings-toggle-1" checked={retentionEnabled} onchange={(v) => { retentionEnabled = v; handleEnableToggle(); }} />
        <label class="toggle-text" for="retentionsettings-toggle-1">{$t('settings.retention.enableLabel')}</label>
      </div>
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
            class="ops-form-input number-input"
            disabled={!retentionEnabled}
          />
          <span class="ops-input-suffix">{$t('settings.retention.daysUnit')}</span>
        </div>
      </div>

      <div class="field-group">
        <label class="field-label" for="run-time">{$t('settings.retention.runTime')}</label>
        <input
          id="run-time"
          type="time"
          bind:value={runTime}
          class="ops-form-input"
          disabled={!retentionEnabled}
        />
      </div>

      <div class="field-group">
        <label class="field-label" for="timezone">{$t('settings.retention.timezone')}</label>
        <select id="timezone" bind:value={timezone} class="ops-form-input" disabled={!retentionEnabled}>
          {#each TIMEZONES as tz (tz)}
            <option value={tz}>{tz}</option>
          {/each}
        </select>
      </div>

      <div class="field-group toggle-field">
        <div class="toggle-label">
          <Toggle id="retentionsettings-toggle-2" checked={deleteErrorFiles} onchange={(v) => (deleteErrorFiles = v)} disabled={!retentionEnabled} />
          <label class="toggle-text" for="retentionsettings-toggle-2">{$t('settings.retention.deleteErrorFiles')}</label>
        </div>
      </div>
    </div>

    <!-- Status display -->
    <div class="status-block">
      <div class="status-item">
        <span class="status-label">{$t('settings.retention.lastRunSection')}:</span>
        <span class="status-value">{formatDateTime(lastRun, $t('settings.retention.lastRunNever'), $locale)}</span>
        {#if lastRun && lastRunDeleted !== null}
          <Badge variant="info" size="md" class="status-badge">{$t('settings.retention.lastRunLabel', { datetime: formatDateTime(lastRun, $t('settings.retention.lastRunNever'), $locale), count: lastRunDeleted })}</Badge>
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
          class="btn btn-danger"
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
        <button type="button" class="btn btn-secondary" on:click={refreshStatus} disabled={statusLoading}>
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
            {$t('settings.retention.previewCount', { count: previewResult.file_count, size: formatFileSize(previewResult.total_size_bytes) })}
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
                {#each previewResult.files as file (file.uuid)}
                  <tr>
                    <td class="col-title">{file.title}</td>
                    <td>{file.owner_email}</td>
                    <td>{formatDateTime(file.completed_at, $t('settings.retention.lastRunNever'), $locale)}</td>
                    <td>{file.age_days}d</td>
                    <td>{formatFileSize(file.size_bytes)}</td>
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

    <TrashRetentionSetting />
  {/if}
</div>

<style>
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

  .fields-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
    gap: var(--space-3) var(--space-5);
    margin-top: var(--space-3);
    margin-bottom: var(--space-3);
  }

  .field-group {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
  }

  .toggle-field {
    justify-content: flex-end;
  }

  .field-label {
    font-size: var(--font-size-sm);
    color: var(--text-muted);
    font-weight: var(--font-weight-medium);
  }

  .inline-input {
    display: flex;
    align-items: center;
    gap: var(--space-1-5);
  }

  .toggle-label {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    cursor: pointer;
    user-select: none;
  }

  .toggle-text {
    font-size: var(--font-size-md);
    color: var(--text-color);
  }

  .ops-form-input:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  select.ops-form-input {
    width: 100%;
  }

  .number-input {
    width: 70px;
    text-align: center;
  }

  /* Confirmation block */
  .confirm-block {
    border: 1px solid var(--warning-color);
    border-radius: var(--radius-md);
    padding: var(--space-3) var(--space-4);
    margin: var(--space-2) 0 var(--space-3) 0;
    background-color: var(--warning-bg);
  }

  .confirm-warning {
    font-size: var(--font-size-md);
    color: var(--warning-text);
    margin: 0 0 var(--space-2) 0;
    line-height: 1.4;
  }

  .confirm-check-label {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    font-size: var(--font-size-md);
    color: var(--text-color);
    cursor: pointer;
    margin-bottom: var(--space-2-5);
    user-select: none;
  }

  .confirm-check-label input[type='checkbox'] {
    appearance: none;
    -webkit-appearance: none;
    width: 16px;
    height: 16px;
    min-width: 16px;
    border: 2px solid var(--border-color);
    border-radius: var(--radius-sm);
    background-color: var(--background-color);
    cursor: pointer;
    position: relative;
    transition: background-color var(--duration-fast) ease, border-color var(--duration-fast) ease;
  }

  .confirm-check-label input[type='checkbox']:checked {
    background-color: var(--primary-color);
    border-color: var(--primary-color);
  }

  .confirm-check-label input[type='checkbox']:checked::after {
    content: '';
    position: absolute;
    left: 3px;
    top: 0;
    width: 5px;
    height: 9px;
    border: 2px solid var(--text-on-solid);
    border-top: none;
    border-inline-start: none;
    transform: rotate(45deg);
  }

  .confirm-check-label input[type='checkbox']:focus {
    outline: 2px solid var(--focus-ring-color);
    outline-offset: 1px;
  }

  .confirm-actions {
    display: flex;
    gap: var(--space-2);
  }

  /* Status */
  .status-block {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
    margin: var(--space-3) 0;
    padding: var(--space-2-5) var(--space-3);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-md);
    background-color: var(--background-secondary);
    font-size: var(--font-size-sm);
  }

  .status-item {
    display: flex;
    align-items: center;
    gap: var(--space-1-5);
    flex-wrap: wrap;
  }

  .run-confirm-text {
    font-size: var(--font-size-sm);
    color: var(--text-muted);
  }

  /* Preview */
  .preview-block {
    margin-bottom: var(--space-3);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-md);
    overflow: hidden;
  }

  .preview-empty {
    padding: var(--space-3) var(--space-4);
    font-size: var(--font-size-md);
    color: var(--text-muted);
    margin: 0;
  }

  .preview-summary {
    padding: var(--space-2) var(--space-3);
    font-size: var(--font-size-sm);
    color: var(--text-muted);
    border-bottom: 1px solid var(--border-color);
    margin: 0;
    background-color: var(--background-secondary);
  }

  .preview-table-wrap {
    overflow-x: auto;
  }

  .preview-table {
    width: 100%;
    border-collapse: collapse;
    font-size: var(--font-size-sm);
  }

  .preview-table th {
    background-color: var(--background-secondary);
    color: var(--text-muted);
    font-weight: var(--font-weight-semibold);
    text-align: start;
    padding: var(--space-1-5) var(--space-2-5);
    border-bottom: 1px solid var(--border-color);
    white-space: nowrap;
  }

  .preview-table td {
    padding: var(--space-1) var(--space-2-5);
    border-bottom: 1px solid var(--border-color);
    color: var(--text-color);
    vertical-align: middle;
  }

  .col-title {
    max-width: 200px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .button-row .btn-secondary {
    margin-inline-end: auto;
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
      margin-inline-end: 0;
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
