<script lang="ts">
  import Toggle from '$components/ui/Toggle.svelte';
  import { formatDateTime } from '$lib/utils/formatting';
  import MenuIcon from '$components/ui/MenuIcon.svelte';
  import { onMount } from 'svelte';
  import Spinner from '$components/ui/Spinner.svelte';
  import { toastStore } from '$stores/toast';
  import { t, locale } from '$stores/locale';
  import Badge from '$components/ui/Badge.svelte';
  import Banner from '$components/ui/Banner.svelte';
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
    void loadSettings();
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

  // ---- Reactive change detection -------------------------------------------------

  $: hasChanges =
    enabled !== origEnabled ||
    schedule !== origSchedule ||
    dryRun !== origDryRun ||
    maxDisablesPerRun !== origMaxDisablesPerRun;

  $: saveDisabled = saving || !hasChanges;
</script>

<div class="directory-sync-settings ops-panel">
  <div class="title-row">
    <h3 class="section-title">{$t('settings.directorySync.title')}</h3>
  </div>
  <p class="section-desc">{$t('settings.directorySync.description')}</p>

  {#if loading}
    <div class="loading-state">
      <Spinner label={$t('common.loading')} size="small" />
    </div>
  {:else}
    {#if enabled && dryRun}
      <Banner tone="info">{$t('settings.directorySync.dryRunActiveBanner')}</Banner>
    {/if}

    <!-- Enable toggle -->
    <div class="field-row">
      <div class="toggle-label">
        <Toggle checked={enabled} onchange={(next) => (enabled = next)} id="dirsync-toggle-label-1" />
        <label class="toggle-text" for="dirsync-toggle-label-1">{$t('settings.directorySync.enableLabel')}</label>
      </div>
      <span class="field-hint enable-hint">{$t('settings.directorySync.enableHint')}</span>
    </div>

    <!-- Schedule -->
    <div class="fields-grid">
      <div class="field-group">
        <label class="field-label" for="dirsync-schedule">{$t('settings.directorySync.schedule')}</label>
        <input dir="ltr"
          id="dirsync-schedule"
          type="text"
          bind:value={schedule}
          class="ops-form-input"
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
          class="ops-form-input number-input"
        />
        <span class="field-hint">{$t('settings.directorySync.maxDisablesPerRunHint')}</span>
      </div>
    </div>

    <!-- Dry run -->
    <div class="field-row dry-run-row">
      <div class="toggle-label">
        <Toggle checked={dryRun} onchange={(next) => (dryRun = next)} id="dirsync-toggle-label-2" />
        <label class="toggle-text" for="dirsync-toggle-label-2">{$t('settings.directorySync.dryRunLabel')}</label>
      </div>
      <span class="field-hint enable-hint">{$t('settings.directorySync.dryRunHint')}</span>
    </div>

    <!-- Status display -->
    <div class="status-block">
      <div class="status-item">
        <span class="status-label">{$t('settings.directorySync.lastRunSection')}:</span>
        <span class="status-value">{formatDateTime(lastRunAt, $t('settings.directorySync.lastRunNever'), $locale)}</span>
        {#if lastResult}
          {#if lastResult.status === 'ok'}
            <Badge variant="success" size="md" class="status-badge">
              {$t('settings.directorySync.lastRunOk', {
                disabled: lastResult.dry_run ? (lastResult.would_disable ?? 0) : (lastResult.disabled ?? 0),
                reconciled: lastResult.reconciled ?? 0,
              })}
            </Badge>
          {:else}
            <Badge variant="error" size="md" class="status-badge" title={lastResult.error || null}>
              {$t('settings.directorySync.lastRunFailed')}
            </Badge>
          {/if}
        {/if}
      </div>
      {#if lastResult && lastResult.status !== 'ok' && lastResult.error}
        <div class="status-error">{lastResult.error}</div>
      {/if}
      {#if lastResult?.capped}
        <div class="status-item">
          <Badge variant="error" size="md" class="status-badge">{$t('settings.directorySync.cappedWarning')}</Badge>
        </div>
      {/if}
      <div class="status-item">
        <span class="status-label">{$t('settings.directorySync.nextDueSection')}:</span>
        {#if nextDue}
          <span class="mount-ok"><MenuIcon name="dot" size={10} /> {$t('settings.directorySync.due')}</span>
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
      <button type="button" class="btn btn-secondary" on:click={() => refreshStatus(true)} disabled={statusLoading}>
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
  .section-title {
    font-size: 0.95rem;
    font-weight: var(--font-weight-semibold);
    margin: 0;
    color: var(--text-color);
  }

  .section-desc {
    font-size: 0.8rem;
    color: var(--text-muted);
    margin: var(--space-1) 0 var(--space-4) 0;
  }

  .dry-run-row {
    margin-top: var(--space-2);
  }

  .enable-hint {
    display: block;
    margin: var(--space-1) 0 0 2.75rem;
  }

  .fields-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
    gap: var(--space-3) var(--space-5);
    margin-top: var(--space-3);
    margin-bottom: var(--space-3);
  }

  .field-group {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
  }

  .field-label {
    font-size: 0.78rem;
    color: var(--text-muted);
    font-weight: var(--font-weight-medium);
  }

  .field-hint {
    font-size: 0.72rem;
    color: var(--text-muted);
  }

  .mount-ok {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    color: var(--success-color);
  }

  .toggle-label {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    user-select: none;
  }

  .toggle-text {
    cursor: pointer;
    font-size: var(--font-size-md);
    color: var(--text-color);
  }

  .number-input {
    width: 110px;
  }

  /* Status */
  .status-block {
    display: flex;
    flex-direction: column;
    gap: 0.3rem;
    margin: var(--space-4) 0 var(--space-3) 0;
    padding: 0.6rem var(--space-3);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-md);
    background-color: var(--background-secondary);
    font-size: 0.8rem;
  }

  .status-item {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    flex-wrap: wrap;
  }

  .run-confirm-text {
    font-size: 0.8rem;
    color: var(--text-muted);
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
