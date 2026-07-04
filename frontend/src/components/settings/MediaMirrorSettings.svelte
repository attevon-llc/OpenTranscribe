<script lang="ts">
  import { createEventDispatcher, onMount } from 'svelte';
  import Spinner from '../ui/Spinner.svelte';
  import { toastStore } from '../../stores/toast';
  import { t } from '$stores/locale';
  import {
    getMediaMirrorSettings,
    updateMediaMirrorSettings,
    runMediaMirrorNow,
    testMirrorS3Connection,
    type MediaMirrorSettings,
    type MirrorDestinationStatus,
    type MirrorDestinationType,
    type MirrorResult,
    type MirrorS3Status,
  } from '$lib/api/mediaMirrorApi';

  // Bubbles dirty state up to BackupSettings (which owns the section's dirty flag).
  const dispatch = createEventDispatcher<{ dirty: boolean }>();

  // ---- State ---------------------------------------------------------------

  let loading = true;
  let saving = false;
  let hasChanges = false;

  // Form fields
  let enabled = false;
  let schedule = '30 3 * * *';
  let destinationType: MirrorDestinationType = 'local';
  let destination = '/media-mirror';
  let throttleMs = 0;
  let s3EndpointUrl = '';
  let s3Region = '';
  let s3Bucket = '';
  let s3Prefix = 'opentranscribe-media/';
  let s3AccessKeyId = '';
  let s3SecretKey = ''; // write-only; blank means "leave the stored secret unchanged"
  let s3SecretKeySet = false;

  // Status
  let destinationStatus: MirrorDestinationStatus | null = null;
  let s3Status: MirrorS3Status | null = null;
  let lastRunAt: string | null = null;
  let lastResult: MirrorResult | null = null;
  let running = false;

  // Originals for dirty tracking
  let orig = {
    enabled: false,
    schedule: '30 3 * * *',
    destinationType: 'local' as MirrorDestinationType,
    destination: '/media-mirror',
    throttleMs: 0,
    s3EndpointUrl: '',
    s3Region: '',
    s3Bucket: '',
    s3Prefix: 'opentranscribe-media/',
    s3AccessKeyId: '',
  };

  // Action state
  let runNowPending = false;
  let runNowLoading = false;
  let statusLoading = false;
  let s3TestLoading = false;
  let s3TestResult: { ok: boolean; error?: string | null } | null = null;

  // ---- Lifecycle -----------------------------------------------------------

  onMount(() => {
    (async () => {
      await loadSettings();
    })();
  });

  // ---- API -----------------------------------------------------------------

  async function loadSettings() {
    loading = true;
    try {
      applySettings(await getMediaMirrorSettings());
    } catch (err) {
      console.error('Error loading media mirror settings:', err);
      toastStore.error($t('settings.backup.mirror.loadFailed'));
    } finally {
      loading = false;
    }
  }

  function applySettings(cfg: MediaMirrorSettings) {
    enabled = cfg.enabled;
    schedule = cfg.schedule;
    destinationType = cfg.destination_type;
    destination = cfg.destination;
    throttleMs = cfg.throttle_ms;
    s3EndpointUrl = cfg.s3_endpoint_url;
    s3Region = cfg.s3_region;
    s3Bucket = cfg.s3_bucket;
    s3Prefix = cfg.s3_prefix;
    s3AccessKeyId = cfg.s3_access_key_id;
    s3SecretKeySet = cfg.s3_secret_key_set;
    s3SecretKey = ''; // never populate the secret field from the server
    destinationStatus = cfg.destination_status;
    s3Status = cfg.s3_status ?? null;
    lastRunAt = cfg.last_run_at ?? null;
    lastResult = cfg.last_result ?? null;
    running = cfg.running;

    orig = {
      enabled,
      schedule,
      destinationType,
      destination,
      throttleMs,
      s3EndpointUrl,
      s3Region,
      s3Bucket,
      s3Prefix,
      s3AccessKeyId,
    };
    hasChanges = false;
  }

  async function saveSettings() {
    saving = true;
    try {
      const cfg = await updateMediaMirrorSettings({
        enabled,
        schedule,
        destination_type: destinationType,
        destination,
        throttle_ms: throttleMs,
        s3_endpoint_url: s3EndpointUrl,
        s3_region: s3Region,
        s3_bucket: s3Bucket,
        s3_prefix: s3Prefix,
        s3_access_key_id: s3AccessKeyId,
        // Only send the secret when the admin actually typed one (keeps the stored value).
        ...(s3SecretKey ? { s3_secret_key: s3SecretKey } : {}),
      });
      applySettings(cfg);
      toastStore.success($t('settings.backup.mirror.saved'));
    } catch (err: unknown) {
      console.error('Error saving media mirror settings:', err);
      const detail = (err as { response?: { data?: { detail?: string } } }).response?.data?.detail;
      toastStore.error(detail || $t('settings.backup.mirror.saveFailed'));
    } finally {
      saving = false;
    }
  }

  async function refreshStatus() {
    statusLoading = true;
    try {
      applySettings(await getMediaMirrorSettings());
    } catch (err) {
      console.error('Error refreshing media mirror status:', err);
      toastStore.error($t('settings.backup.mirror.loadFailed'));
    } finally {
      statusLoading = false;
    }
  }

  async function testS3() {
    s3TestLoading = true;
    s3TestResult = null;
    try {
      const res = await testMirrorS3Connection({
        s3_endpoint_url: s3EndpointUrl,
        s3_region: s3Region,
        s3_bucket: s3Bucket,
        s3_prefix: s3Prefix,
        s3_access_key_id: s3AccessKeyId,
        ...(s3SecretKey ? { s3_secret_key: s3SecretKey } : {}),
      });
      s3TestResult = { ok: res.ok, error: res.error };
      if (res.ok) {
        toastStore.success($t('settings.backup.mirror.s3TestOk'));
      } else {
        toastStore.error($t('settings.backup.mirror.s3TestFailed'));
      }
    } catch (err) {
      console.error('Error testing mirror S3 connection:', err);
      s3TestResult = { ok: false, error: $t('settings.backup.mirror.s3TestFailed') };
      toastStore.error($t('settings.backup.mirror.s3TestFailed'));
    } finally {
      s3TestLoading = false;
    }
  }

  async function runNow() {
    runNowLoading = true;
    try {
      await runMediaMirrorNow();
      runNowPending = false;
      toastStore.success($t('settings.backup.mirror.runNowQueued'));
    } catch (err) {
      console.error('Error triggering media mirror:', err);
      toastStore.error($t('settings.backup.mirror.runNowFailed'));
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
    if (!iso) return $t('settings.backup.mirror.lastRunNever');
    return new Date(iso).toLocaleString();
  }

  // ---- Reactive change detection -------------------------------------------

  $: {
    hasChanges =
      enabled !== orig.enabled ||
      schedule !== orig.schedule ||
      destinationType !== orig.destinationType ||
      destination !== orig.destination ||
      throttleMs !== orig.throttleMs ||
      s3EndpointUrl !== orig.s3EndpointUrl ||
      s3Region !== orig.s3Region ||
      s3Bucket !== orig.s3Bucket ||
      s3Prefix !== orig.s3Prefix ||
      s3AccessKeyId !== orig.s3AccessKeyId ||
      s3SecretKey !== '';
    dispatch('dirty', hasChanges);
  }

  $: isS3 = destinationType === 's3';
  $: mountOk = destinationStatus?.mounted ?? false;
  $: destinationReady = isS3 ? (s3Status?.reachable ?? false) : mountOk;
  $: saveDisabled = saving || !hasChanges;
</script>

<div class="mirror-settings">
  <h4 class="subsection-title">{$t('settings.backup.mirror.title')}</h4>
  <p class="subsection-desc">{$t('settings.backup.mirror.description')}</p>
  <p class="never-delete-note">{$t('settings.backup.mirror.neverDeleteNote')}</p>

  {#if loading}
    <div class="loading-state">
      <Spinner size="small" />
    </div>
  {:else}
    {#if enabled && !isS3 && !mountOk}
      <div class="banner banner-warning">
        {$t('settings.backup.mirror.mountMissing', { destination })}
      </div>
    {/if}
    {#if enabled && isS3 && s3Status && !s3Status.reachable}
      <div class="banner banner-warning">
        {$t('settings.backup.mirror.s3Unreachable')}{s3Status.error ? `: ${s3Status.error}` : ''}
      </div>
    {/if}

    <!-- Enable toggle -->
    <div class="field-row">
      <label class="toggle-label">
        <input type="checkbox" class="toggle-input" bind:checked={enabled} />
        <span class="toggle-switch"></span>
        <span class="toggle-text">{$t('settings.backup.mirror.enableLabel')}</span>
      </label>
    </div>

    <!-- Schedule + throttle -->
    <div class="fields-grid">
      <div class="field-group">
        <label class="field-label" for="mirror-schedule">{$t('settings.backup.mirror.schedule')}</label>
        <input
          id="mirror-schedule"
          type="text"
          bind:value={schedule}
          class="form-input"
          placeholder="30 3 * * *"
          spellcheck="false"
        />
        <span class="field-hint">{$t('settings.backup.mirror.scheduleHint')}</span>
      </div>
      <div class="field-group">
        <label class="field-label" for="mirror-throttle">{$t('settings.backup.mirror.throttle')}</label>
        <input
          id="mirror-throttle"
          type="number"
          bind:value={throttleMs}
          min="0"
          max="60000"
          class="form-input number-input"
        />
        <span class="field-hint">{$t('settings.backup.mirror.throttleHint')}</span>
      </div>
    </div>

    <!-- Destination type -->
    <div class="field-group destination-type-group">
      <label class="field-label" for="mirror-dest-type">{$t('settings.backup.mirror.destinationType')}</label>
      <select id="mirror-dest-type" class="form-input" bind:value={destinationType}>
        <option value="local">{$t('settings.backup.mirror.destinationLocal')}</option>
        <option value="s3">{$t('settings.backup.mirror.destinationS3')}</option>
      </select>
    </div>

    {#if !isS3}
      <div class="fields-grid">
        <div class="field-group">
          <label class="field-label" for="mirror-destination">{$t('settings.backup.mirror.destination')}</label>
          <input id="mirror-destination" type="text" bind:value={destination} class="form-input" spellcheck="false" />
          <span class="field-hint">
            {#if mountOk}
              <span class="mount-ok">● {$t('settings.backup.mirror.mountOk')}</span>
            {:else}
              <span class="mount-bad">● {$t('settings.backup.mirror.mountNotReady')}</span>
            {/if}
          </span>
        </div>
      </div>
    {:else}
      <p class="subsection-desc">{$t('settings.backup.mirror.s3Hint')}</p>
      <div class="fields-grid">
        <div class="field-group">
          <label class="field-label" for="mirror-s3-endpoint">{$t('settings.backup.mirror.s3Endpoint')}</label>
          <input id="mirror-s3-endpoint" type="text" bind:value={s3EndpointUrl} class="form-input" placeholder="https://s3.amazonaws.com" spellcheck="false" />
        </div>
        <div class="field-group">
          <label class="field-label" for="mirror-s3-region">{$t('settings.backup.mirror.s3Region')}</label>
          <input id="mirror-s3-region" type="text" bind:value={s3Region} class="form-input" placeholder="us-east-1" spellcheck="false" />
        </div>
        <div class="field-group">
          <label class="field-label" for="mirror-s3-bucket">{$t('settings.backup.mirror.s3Bucket')}</label>
          <input id="mirror-s3-bucket" type="text" bind:value={s3Bucket} class="form-input" spellcheck="false" />
        </div>
        <div class="field-group">
          <label class="field-label" for="mirror-s3-prefix">{$t('settings.backup.mirror.s3Prefix')}</label>
          <input id="mirror-s3-prefix" type="text" bind:value={s3Prefix} class="form-input" placeholder="opentranscribe-media/" spellcheck="false" />
        </div>
        <div class="field-group">
          <label class="field-label" for="mirror-s3-access-key">{$t('settings.backup.mirror.s3AccessKey')}</label>
          <input id="mirror-s3-access-key" type="text" bind:value={s3AccessKeyId} class="form-input" autocomplete="off" spellcheck="false" />
        </div>
        <div class="field-group">
          <label class="field-label" for="mirror-s3-secret-key">{$t('settings.backup.mirror.s3SecretKey')}</label>
          <input
            id="mirror-s3-secret-key"
            type="password"
            bind:value={s3SecretKey}
            class="form-input"
            autocomplete="new-password"
            spellcheck="false"
            placeholder={s3SecretKeySet ? $t('settings.backup.mirror.s3SecretConfigured') : ''}
          />
          <span class="field-hint">
            {#if s3SecretKeySet}
              <span class="mount-ok">● {$t('settings.backup.mirror.s3SecretConfigured')}</span>
            {:else}
              <span class="mount-bad">● {$t('settings.backup.mirror.s3SecretNotSet')}</span>
            {/if}
          </span>
        </div>
      </div>
      <div class="action-row">
        <button type="button" class="btn btn-secondary" on:click={testS3} disabled={s3TestLoading || !s3Bucket}>
          {#if s3TestLoading}<Spinner size="small" />{/if}
          {$t('settings.backup.mirror.s3TestButton')}
        </button>
        {#if s3TestResult}
          {#if s3TestResult.ok}
            <span class="mount-ok">● {$t('settings.backup.mirror.s3TestOk')}</span>
          {:else}
            <span class="mount-bad" title={s3TestResult.error ?? ''}>● {$t('settings.backup.mirror.s3TestFailed')}</span>
          {/if}
        {/if}
      </div>
    {/if}

    <!-- Last-run status -->
    <div class="status-block">
      <div class="status-item">
        <span class="status-label">{$t('settings.backup.mirror.lastRunSection')}:</span>
        <span class="status-value">{formatDate(lastRunAt)}</span>
        {#if running}
          <span class="status-badge running">{$t('settings.backup.mirror.runningBadge')}</span>
        {/if}
        {#if lastResult}
          {#if lastResult.ok}
            <span class="status-badge ok">
              {$t('settings.backup.mirror.lastRunOk', {
                copied: lastResult.objects_copied ?? 0,
                skipped: lastResult.objects_skipped ?? 0,
                bytes: formatBytes(lastResult.bytes_copied ?? 0),
              })}
            </span>
          {:else}
            <span class="status-badge bad" title={lastResult.error ?? ''}>{$t('settings.backup.mirror.lastRunFailed')}</span>
          {/if}
        {/if}
      </div>
      {#if lastResult && !lastResult.ok && lastResult.error}
        <div class="status-error">{lastResult.error}</div>
      {/if}
      {#if lastResult?.ok && (lastResult.objects_failed ?? 0) > 0}
        <div class="status-item">
          <span class="status-badge bad">
            {$t('settings.backup.mirror.failedObjects', { count: lastResult.objects_failed ?? 0 })}
          </span>
        </div>
        {#each lastResult.errors ?? [] as errLine}
          <div class="status-error">{errLine}</div>
        {/each}
      {/if}
    </div>

    <!-- Actions -->
    <div class="action-row">
      {#if !runNowPending}
        <button
          type="button"
          class="btn btn-secondary"
          on:click={() => (runNowPending = true)}
          disabled={runNowLoading || running || !destinationReady}
        >
          {$t('settings.backup.mirror.runNowButton')}
        </button>
      {:else}
        <span class="run-confirm-text">{$t('settings.backup.mirror.runNowConfirmBody')}</span>
        <button type="button" class="btn btn-secondary" on:click={() => (runNowPending = false)}>
          {$t('settings.backup.mirror.cancel')}
        </button>
        <button type="button" class="btn btn-primary" on:click={runNow} disabled={runNowLoading}>
          {#if runNowLoading}<Spinner size="small" />{/if}
          {$t('settings.backup.mirror.runNowButton')}
        </button>
      {/if}
      <button type="button" class="btn btn-link" on:click={refreshStatus} disabled={statusLoading}>
        {statusLoading ? $t('settings.backup.mirror.refreshingStatus') : $t('settings.backup.mirror.refreshStatus')}
      </button>
    </div>

    <!-- Save / Reset -->
    <div class="button-row">
      <button type="button" class="btn btn-secondary" on:click={loadSettings} disabled={saving}>
        {$t('settings.backup.mirror.resetButton')}
      </button>
      <button type="button" class="btn btn-primary" on:click={saveSettings} disabled={saveDisabled}>
        {saving ? $t('settings.backup.mirror.savingButton') : $t('settings.backup.mirror.saveButton')}
      </button>
    </div>
  {/if}
</div>

<style>
  .mirror-settings {
    margin-top: 1.5rem;
    padding-top: 1rem;
    border-top: 1px solid var(--border-color);
  }

  .subsection-title {
    font-size: 0.85rem;
    font-weight: 600;
    margin: 0 0 0.25rem 0;
    color: var(--text-color);
  }

  .subsection-desc {
    font-size: 0.8rem;
    color: var(--text-muted);
    margin: 0 0 0.5rem 0;
  }

  .never-delete-note {
    font-size: 0.75rem;
    color: var(--text-muted);
    margin: 0 0 0.75rem 0;
    font-style: italic;
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
    width: 110px;
    text-align: center;
  }

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

  .status-badge.running {
    background-color: var(--primary-color);
  }

  .status-error {
    font-size: 0.75rem;
    color: var(--error-color, #ef4444);
    word-break: break-word;
  }

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
