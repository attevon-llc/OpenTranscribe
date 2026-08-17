<script lang="ts">
  import { onMount } from 'svelte';
  import Spinner from '../ui/Spinner.svelte';
  import { toastStore } from '../../stores/toast';
  import { t } from '$stores/locale';
  import {
    getDirectorySyncSettings,
    updateDirectorySyncSettings,
    getDirectorySyncStatus,
    runDirectorySyncNow,
    type DirectorySyncSettings,
    type DirectorySyncStatus,
  } from '$lib/api/directorySyncApi';

  // ---- State ---------------------------------------------------------------

  let loading = true;
  let saving = false;

  // Form fields
  let enabled = false;
  let schedule = '0 4 * * *';
  let dryRun = true;
  let maxDisablesPerRun = 10;

  // Status
  let nextDue = false;
  let lastRunAt: string | null = null;
  let lastResult: DirectorySyncStatus['last_result'] = null;
  let statusLoading = false;

  // Original values for dirty tracking
  let origEnabled = false;
  let origSchedule = '0 4 * * *';
  let origDryRun = true;
  let origMaxDisablesPerRun = 10;

  // Run-now state
  let runNowPending = false;
  let runNowLoading = false;

  // ---- Lifecycle -------------------------------------------------------------

  onMount(() => {
    (async () => {
      await loadSettings();
    })();
  });

  // ---- API -------------------------------------------------------------------

  async function loadSettings() {
    loading = true;
    try {
      const cfg = await getDirectorySyncSettings();
      applySettings(cfg);
      await refreshStatus(false);
    } catch (err) {
      console.error('Error loading directory sync settings:', err);
      toastStore.error($t('settings.directorySync.loadFailed'));
    } finally {
      loading = false;
    }
  }

  function applySettings(cfg: DirectorySyncSettings) {
    enabled = cfg.enabled;
    schedule = cfg.schedule;
    dryRun = cfg.dry_run;
    maxDisablesPerRun = cfg.max_disables_per_run;

    origEnabled = cfg.enabled;
    origSchedule = cfg.schedule;
    origDryRun = cfg.dry_run;
    origMaxDisablesPerRun = cfg.max_disables_per_run;
  }

  async function saveSettings() {
    saving = true;
    try {
      const cfg = await updateDirectorySyncSettings({
        enabled,
        schedule,
        dry_run: dryRun,
        max_disables_per_run: maxDisablesPerRun,
      });
      applySettings(cfg);
      await refreshStatus(false);
      toastStore.success($t('settings.directorySync.saved'));
    } catch (err: unknown) {
      console.error('Error saving directory sync settings:', err);
      const detail = (err as { response?: { data?: { detail?: string } } }).response?.data?.detail;
      toastStore.error(detail || $t('settings.directorySync.saveFailed'));
    } finally {
      saving = false;
    }
  }

  async function refreshStatus(notify = true) {
    statusLoading = true;
    try {
      const st = await getDirectorySyncStatus();
      nextDue = st.next_due;
      lastRunAt = st.last_run_at ?? null;
      lastResult = st.last_result ?? null;
      if (notify) toastStore.success($t('settings.directorySync.statusRefreshed'));
    } catch (err) {
      console.error('Error refreshing directory sync status:', err);
      if (notify) toastStore.error($t('settings.directorySync.statusFailed'));
    } finally {
      statusLoading = false;
    }
  }

  async function runNow() {
    runNowLoading = true;
    try {
      await runDirectorySyncNow();
      runNowPending = false;
      toastStore.success($t('settings.directorySync.runNowQueued'));
    } catch (err) {
      console.error('Error triggering directory sync:', err);
      toastStore.error($t('settings.directorySync.runNowFailed'));
    } finally {
      runNowLoading = false;
    }
  }

  // ---- Formatting --------------------------------------------------------------

  function formatDate(iso: string | null | undefined): string {
    if (!iso) return $t('settings.directorySync.lastRunNever');
    return new Date(iso).toLocaleString();
  }

  // ---- Reactive change detection -------------------------------------------------

  $: hasChanges =
    enabled !== origEnabled ||
    schedule !== origSchedule ||
    dryRun !== origDryRun ||
    maxDisablesPerRun !== origMaxDisablesPerRun;

  $: saveDisabled = saving || !hasChanges;
</script>

<div class="directory-sync-settings">
  <div class="title-row">
    <h3 class="section-title">{$t('settings.directorySync.title')}</h3>
  </div>
  <p class="section-desc">{$t('settings.directorySync.description')}</p>

  {#if loading}
    <div class="loading-state">
      <Spinner size="small" />
    </div>
  {:else}
    {#if enabled && dryRun}
      <div class="banner banner-info">{$t('settings.directorySync.dryRunActiveBanner')}</div>
    {/if}

    <!-- Enable toggle -->
    <div class="field-row">
      <label class="toggle-label">
        <input type="checkbox" class="toggle-input" bind:checked={enabled} />
        <span class="toggle-switch"></span>
        <span class="toggle-text">{$t('settings.directorySync.enableLabel')}</span>
      </label>
      <span class="field-hint enable-hint">{$t('settings.directorySync.enableHint')}</span>
    </div>

    <!-- Schedule -->
    <div class="fields-grid">
      <div class="field-group">
        <label class="field-label" for="dirsync-schedule">{$t('settings.directorySync.schedule')}</label>
        <input
          id="dirsync-schedule"
          type="text"
          bind:value={schedule}
          class="form-input"
          placeholder="0 4 * * *"
          spellcheck="false"
        />
        <span class="field-hint">{$t('settings.directorySync.scheduleHint')}</span>
      </div>
      <div class="field-group">
        <label class="field-label" for="dirsync-max-disables">
          {$t('settings.directorySync.maxDisablesPerRun')}
        </label>
        <input
          id="dirsync-max-disables"
          type="number"
          bind:value={maxDisablesPerRun}
          min="1"
          max="100000"
          class="form-input number-input"
        />
        <span class="field-hint">{$t('settings.directorySync.maxDisablesPerRunHint')}</span>
      </div>
    </div>

    <!-- Dry run -->
    <div class="field-row dry-run-row">
      <label class="toggle-label">
        <input type="checkbox" class="toggle-input" bind:checked={dryRun} />
        <span class="toggle-switch"></span>
        <span class="toggle-text">{$t('settings.directorySync.dryRunLabel')}</span>
      </label>
      <span class="field-hint enable-hint">{$t('settings.directorySync.dryRunHint')}</span>
    </div>

    <!-- Status display -->
    <div class="status-block">
      <div class="status-item">
        <span class="status-label">{$t('settings.directorySync.lastRunSection')}:</span>
        <span class="status-value">{formatDate(lastRunAt)}</span>
        {#if lastResult}
          {#if lastResult.status === 'ok'}
            <span class="status-badge ok">
              {$t('settings.directorySync.lastRunOk', {
                disabled: lastResult.dry_run ? (lastResult.would_disable ?? 0) : (lastResult.disabled ?? 0),
                reconciled: lastResult.reconciled ?? 0,
              })}
            </span>
          {:else}
            <span class="status-badge bad" title={lastResult.error ?? ''}>
              {$t('settings.directorySync.lastRunFailed')}
            </span>
          {/if}
        {/if}
      </div>
      {#if lastResult && lastResult.status !== 'ok' && lastResult.error}
        <div class="status-error">{lastResult.error}</div>
      {/if}
      {#if lastResult?.capped}
        <div class="status-item">
          <span class="status-badge bad">{$t('settings.directorySync.cappedWarning')}</span>
        </div>
      {/if}
      <div class="status-item">
        <span class="status-label">{$t('settings.directorySync.nextDueSection')}:</span>
        {#if nextDue}
          <span class="mount-ok">● {$t('settings.directorySync.due')}</span>
        {:else}
          <span class="status-value">{$t('settings.directorySync.notDue')}</span>
        {/if}
      </div>
    </div>

    <!-- Actions -->
    <div class="action-row">
      {#if !runNowPending}
        <button type="button" class="btn btn-secondary" on:click={() => (runNowPending = true)} disabled={runNowLoading}>
          {$t('settings.directorySync.runNowButton')}
        </button>
      {:else}
        <span class="run-confirm-text">
          {dryRun
            ? $t('settings.directorySync.runNowConfirmDryRun')
            : $t('settings.directorySync.runNowConfirmLive')}
        </span>
        <button type="button" class="btn btn-secondary" on:click={() => (runNowPending = false)}>
          {$t('settings.directorySync.cancel')}
        </button>
        <button type="button" class="btn btn-primary" on:click={runNow} disabled={runNowLoading}>
          {#if runNowLoading}<Spinner size="small" />{/if}
          {$t('settings.directorySync.runNowButton')}
        </button>
      {/if}
      <button type="button" class="btn btn-link" on:click={() => refreshStatus(true)} disabled={statusLoading}>
        {statusLoading ? $t('settings.directorySync.refreshingStatus') : $t('settings.directorySync.refreshStatus')}
      </button>
    </div>

    <!-- Save / Reset -->
    <div class="button-row">
      <button type="button" class="btn btn-secondary" on:click={loadSettings} disabled={saving}>
        {$t('settings.directorySync.resetButton')}
      </button>
      <button type="button" class="btn btn-primary" on:click={saveSettings} disabled={saveDisabled}>
        {saving ? $t('settings.directorySync.savingButton') : $t('settings.directorySync.saveButton')}
      </button>
    </div>
  {/if}
</div>

<style>
  .directory-sync-settings {
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

  .banner {
    border-radius: 6px;
    padding: 0.6rem 0.75rem;
    margin-bottom: 0.75rem;
    font-size: 0.82rem;
    line-height: 1.4;
  }

  .banner-info {
    border: 1px solid var(--primary-color);
    background-color: var(--background-secondary, rgba(0, 0, 0, 0.03));
    color: var(--text-color);
  }

  .field-row {
    margin-bottom: 0.75rem;
  }

  .dry-run-row {
    margin-top: 0.5rem;
  }

  .enable-hint {
    display: block;
    margin: 0.25rem 0 0 2.75rem;
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
    width: 110px;
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
