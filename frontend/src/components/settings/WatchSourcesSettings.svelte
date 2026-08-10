<script lang="ts">
  import { onMount } from 'svelte';
  import Spinner from '$components/ui/Spinner.svelte';
  import EmptyState from '$components/ui/EmptyState.svelte';
  import ConfirmationModal from '../ConfirmationModal.svelte';
  import WatchSourceModal from './WatchSourceModal.svelte';
  import EmailConfigModal from './EmailConfigModal.svelte';
  import { t } from '$stores/locale';
  import { toastStore } from '$stores/toast';
  import { user } from '$stores/auth';
  import { getErrorMessage } from '$lib/utils/apiError';
  import {
    getCapabilities,
    getWatchSources,
    deleteWatchSource,
    updateWatchSource,
    testWatchSource,
    scanWatchSource,
    getWatchSourceStats,
    getEmailConfigs,
    deleteEmailConfig,
    testEmailConfig,
    getGlobalSettings,
    updateGlobalSettings,
    getSourceTypeLabel,
    type WatchSource,
    type Capabilities,
    type WatchSourceStats,
    type EmailConfig,
    type GlobalWatchSettings,
  } from '$lib/api/watchSourcesApi';

  $: isAdmin = $user?.role === 'admin' || $user?.role === 'super_admin';
  // Which config carries password resets and invitations is a deployment-wide
  // credential decision, so it sits one tier above managing the configs.
  $: isSuperAdmin = $user?.role === 'super_admin';

  let loading = true;
  let saving = false;
  let capabilities: Capabilities = {
    watch_source_enabled: true,
    local_enabled: false,
    fs_events_enabled: false,
    fs_events_mode: 'auto',
  };
  let sources: WatchSource[] = [];
  let statsMap: Record<string, WatchSourceStats> = {};
  let scope: 'own' | 'all' = 'own';
  let testingUuid: string | null = null;

  // modals
  let showSourceModal = false;
  let editingSource: WatchSource | null = null;
  let showDeleteModal = false;
  let sourceToDelete: WatchSource | null = null;

  // admin: email configs + global settings
  let emailConfigs: EmailConfig[] = [];
  let showEmailModal = false;
  let editingEmail: EmailConfig | null = null;
  let showEmailHelp = false;
  let globalSettings: GlobalWatchSettings | null = null;

  async function loadSources() {
    sources = await getWatchSources(scope);
    const entries = await Promise.all(
      sources.map(async (s) => {
        try {
          return [s.uuid, await getWatchSourceStats(s.uuid)] as const;
        } catch {
          return [s.uuid, null] as const;
        }
      })
    );
    statsMap = Object.fromEntries(entries.filter(([, v]) => v)) as Record<string, WatchSourceStats>;
  }

  async function loadAll() {
    loading = true;
    try {
      capabilities = await getCapabilities();
      await loadSources();
      // Email configs and the global watch settings are super_admin endpoints
      // (`get_current_active_superuser`). Fetching them as a plain admin only
      // produced two swallowed 403s and a panel that looked empty rather than
      // forbidden, so don't ask for them unless the tier is right.
      if (isSuperAdmin) {
        emailConfigs = await getEmailConfigs().catch(() => []);
        globalSettings = await getGlobalSettings().catch(() => null);
      }
    } catch (err) {
      toastStore.error(getErrorMessage(err, $t('settings.watchSources.loadFailed')));
    } finally {
      loading = false;
    }
  }

  onMount(() => {
    loadAll();
    const onScan = () => loadSources();
    window.addEventListener('watch-source-scan', onScan);
    return () => window.removeEventListener('watch-source-scan', onScan);
  });

  function openCreate() {
    editingSource = null;
    showSourceModal = true;
  }
  function openEdit(s: WatchSource) {
    editingSource = s;
    showSourceModal = true;
  }
  async function handleSaved() {
    showSourceModal = false;
    editingSource = null;
    await loadSources();
  }

  async function toggleEnabled(s: WatchSource) {
    saving = true;
    const newVal = !s.is_enabled;
    sources = sources.map((x) => (x.uuid === s.uuid ? { ...x, is_enabled: newVal } : x));
    try {
      await updateWatchSource(s.uuid, { is_enabled: newVal });
    } catch (err) {
      sources = sources.map((x) => (x.uuid === s.uuid ? { ...x, is_enabled: !newVal } : x));
      toastStore.error(getErrorMessage(err, $t('settings.watchSources.saveFailed')));
    } finally {
      saving = false;
    }
  }

  async function handleTest(s: WatchSource) {
    testingUuid = s.uuid;
    try {
      const res = await testWatchSource(s.uuid);
      if (res.success) toastStore.success(`${s.name}: ${res.message}`);
      else toastStore.error(`${s.name}: ${res.message}`, 6000);
    } catch (err) {
      toastStore.error(getErrorMessage(err, $t('settings.watchSources.testFailed')));
    } finally {
      testingUuid = null;
    }
  }

  async function handleScan(s: WatchSource) {
    try {
      await scanWatchSource(s.uuid);
      toastStore.success($t('settings.watchSources.scanStarted', { name: s.name }));
    } catch (err) {
      toastStore.error(getErrorMessage(err, $t('settings.watchSources.scanFailed')));
    }
  }

  function confirmDelete(s: WatchSource) {
    sourceToDelete = s;
    showDeleteModal = true;
  }
  async function doDelete() {
    if (!sourceToDelete) return;
    try {
      await deleteWatchSource(sourceToDelete.uuid);
      toastStore.success($t('settings.watchSources.deleted'));
      await loadSources();
    } catch (err) {
      toastStore.error(getErrorMessage(err, $t('settings.watchSources.deleteFailed')));
    } finally {
      showDeleteModal = false;
      sourceToDelete = null;
    }
  }

  function scanBadgeClass(status?: string | null): string {
    if (status === 'success') return 'badge-success';
    if (status === 'error') return 'badge-error';
    if (status === 'running') return 'badge-running';
    return 'badge-idle';
  }

  /**
   * Does this source opt into event-driven watching at all? Only local sources
   * can — S3/SMB have no equivalent, so they never show a watch-mode badge.
   */
  function showsWatchMode(s: WatchSource): boolean {
    return s.source_type === 'local' && s.use_fs_events && capabilities.fs_events_enabled;
  }

  /** Short badge label for the observer a source actually ended up with. */
  function watchModeLabel(s: WatchSource): string {
    const mode = s.fs_events?.mode;
    if (mode === 'native') return $t('settings.watchSources.watchMode.native');
    if (mode === 'polling') return $t('settings.watchSources.watchMode.polling');
    if (mode === 'error') return $t('settings.watchSources.watchMode.error');
    if (mode === 'unavailable') return $t('settings.watchSources.watchMode.unavailable');
    return $t('settings.watchSources.watchMode.scanOnly', {
      minutes: s.polling_interval_minutes,
    });
  }

  function watchModeClass(s: WatchSource): string {
    const mode = s.fs_events?.mode;
    if (mode === 'native') return 'badge-success';
    if (mode === 'polling') return 'badge-running';
    if (mode === 'error' || mode === 'unavailable') return 'badge-error';
    return 'badge-idle';
  }

  /** Tooltip: the backend's own explanation, or why nothing is watching. */
  function watchModeTitle(s: WatchSource): string {
    if (s.fs_events?.detail) return s.fs_events.detail;
    return $t('settings.watchSources.watchMode.scanOnlyHelp', {
      minutes: s.polling_interval_minutes,
    });
  }

  // admin email configs
  function openEmailCreate() {
    editingEmail = null;
    showEmailModal = true;
  }
  function openEmailEdit(c: EmailConfig) {
    editingEmail = c;
    showEmailModal = true;
  }
  async function handleEmailSaved() {
    showEmailModal = false;
    editingEmail = null;
    emailConfigs = await getEmailConfigs();
  }
  async function handleEmailTest(c: EmailConfig) {
    try {
      const res = await testEmailConfig(c.uuid);
      if (res.success) toastStore.success(`${c.name}: ${res.message}`);
      else toastStore.error(`${c.name}: ${res.message}`, 6000);
      emailConfigs = await getEmailConfigs();
    } catch (err) {
      toastStore.error(getErrorMessage(err, $t('settings.watchSources.testFailed')));
    }
  }
  async function handleEmailDelete(c: EmailConfig) {
    try {
      await deleteEmailConfig(c.uuid);
      emailConfigs = emailConfigs.filter((x) => x.uuid !== c.uuid);
      toastStore.success($t('settings.emailNotifications.deleted'));
    } catch (err) {
      toastStore.error(getErrorMessage(err, $t('settings.emailNotifications.deleteFailed')));
    }
  }

  async function saveGlobalSettings() {
    if (!globalSettings) return;
    saving = true;
    try {
      globalSettings = await updateGlobalSettings(globalSettings);
      toastStore.success($t('settings.watchSources.settingsSaved'));
    } catch (err) {
      toastStore.error(getErrorMessage(err, $t('settings.watchSources.saveFailed')));
    } finally {
      saving = false;
    }
  }
</script>

{#if loading}
  <div class="ws-loading"><Spinner /></div>
{:else}
  {#if isAdmin}
    <div class="scope-toggle">
      <button class="btn" class:btn-primary={scope === 'own'} class:btn-secondary={scope !== 'own'} on:click={() => { scope = 'own'; loadSources(); }}>
        {$t('settings.watchSources.myScope')}
      </button>
      <button class="btn" class:btn-primary={scope === 'all'} class:btn-secondary={scope !== 'all'} on:click={() => { scope = 'all'; loadSources(); }}>
        {$t('settings.watchSources.allScope')}
      </button>
    </div>
  {/if}

  <div class="section-head">
    <h4>{$t('settings.watchSources.sourcesHeading')}</h4>
    <button class="btn btn-primary" on:click={openCreate}>+ {$t('settings.watchSources.addSource')}</button>
  </div>

  {#if sources.length === 0}
    <EmptyState
      title={$t('settings.watchSources.emptyTitle')}
      description={$t('settings.watchSources.emptyDescription')}
    >
      <svelte:fragment slot="icon">
        <svg
          width="40"
          height="40"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="1.6"
          stroke-linecap="round"
          stroke-linejoin="round"
          aria-hidden="true"
        >
          <path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7Z" />
          <circle cx="12" cy="12" r="3" />
        </svg>
      </svelte:fragment>
    </EmptyState>
  {:else}
    <div class="source-list">
      {#each sources as s (s.uuid)}
        <div class="source-card">
          <div class="source-main">
            <div class="source-badges">
              <span class="badge type-badge">{getSourceTypeLabel(s.source_type)}</span>
              <span class="badge {scanBadgeClass(s.last_scan_status)}">
                {s.last_scan_status || $t('settings.watchSources.neverScanned')}
              </span>
              {#if showsWatchMode(s)}
                <span class="badge {watchModeClass(s)}" title={watchModeTitle(s)}>
                  {watchModeLabel(s)}
                </span>
              {/if}
              {#if !s.is_own}
                <span class="badge owner-badge">{s.owner_name}</span>
              {/if}
            </div>
            <div class="source-name">{s.name}</div>
            <div class="source-meta">
              {#if statsMap[s.uuid]}
                {$t('settings.watchSources.statsLine', {
                  imported: statsMap[s.uuid].imported,
                  skipped: statsMap[s.uuid].skipped,
                  error: statsMap[s.uuid].error,
                })}
              {/if}
              {#if s.last_scan_message}<span class="scan-msg"> · {s.last_scan_message}</span>{/if}
            </div>
            {#if s.source_type === 'local' && !s.delete_after_import}
              <div class="disk-banner">{$t('settings.watchSources.diskBanner')}</div>
            {/if}
          </div>
          <div class="source-actions">
            <label class="enable-toggle" title={$t('settings.watchSources.fields.enabled')}>
              <input type="checkbox" checked={s.is_enabled} on:change={() => toggleEnabled(s)} disabled={saving} />
              <span class="slider"></span>
            </label>
            <button class="btn btn-secondary btn-sm" on:click={() => handleTest(s)} disabled={testingUuid === s.uuid}>
              {#if testingUuid === s.uuid}<Spinner size="small" />{:else}{$t('settings.watchSources.test')}{/if}
            </button>
            <button class="btn btn-secondary btn-sm" on:click={() => handleScan(s)} disabled={!s.is_enabled}>
              {$t('settings.watchSources.scanNow')}
            </button>
            <button class="btn btn-secondary btn-sm" on:click={() => openEdit(s)}>{$t('common.edit')}</button>
            <button class="btn btn-danger btn-sm" on:click={() => confirmDelete(s)}>{$t('common.delete')}</button>
          </div>
        </div>
      {/each}
    </div>
  {/if}

  {#if isSuperAdmin}
    <!-- Email notification configs (super_admin tier) -->
    <div class="section-head admin-section">
      <div class="email-heading">
        <h4>{$t('settings.emailNotifications.heading')}</h4>
        <span class="experimental-badge" title={$t('settings.emailNotifications.experimentalNote')}>
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path d="M9 3h6M10 3v6.5L5.2 18a2 2 0 0 0 1.8 3h10a2 2 0 0 0 1.8-3L14 9.5V3" />
            <path d="M7.5 14h9" />
          </svg>
          {$t('settings.emailNotifications.experimental')}
        </span>
        <button
          class="info-icon"
          type="button"
          aria-expanded={showEmailHelp}
          aria-label={$t('settings.emailNotifications.setupTitle')}
          title={$t('settings.emailNotifications.setupTitle')}
          on:click={() => (showEmailHelp = !showEmailHelp)}
        >
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <circle cx="12" cy="12" r="10" />
            <line x1="12" y1="16" x2="12" y2="12" />
            <line x1="12" y1="8" x2="12.01" y2="8" />
          </svg>
        </button>
      </div>
      <button class="btn btn-primary" on:click={openEmailCreate}>+ {$t('settings.emailNotifications.addConfig')}</button>
    </div>
    <p class="experimental-note">{$t('settings.emailNotifications.experimentalNote')}</p>
    {#if showEmailHelp}
      <div class="email-help">
        <h5>{$t('settings.emailNotifications.setupTitle')}</h5>
        <p><strong>SMTP (Gmail, Outlook, Yahoo)</strong> — {$t('settings.emailNotifications.smtpHelp')}</p>
        <p><strong>Microsoft 365</strong> — {$t('settings.emailNotifications.m365Help')}</p>
        <p><strong>Exchange</strong> — {$t('settings.emailNotifications.exchangeHelp')}</p>
      </div>
    {/if}
    {#if emailConfigs.length === 0}
      <p class="muted">{$t('settings.emailNotifications.empty')}</p>
    {:else}
      <div class="email-list">
        {#each emailConfigs as c (c.uuid)}
          <div class="email-card">
            <div>
              <span class="badge type-badge">{c.provider.toUpperCase()}</span>
              <span class="email-name">{c.name}</span>
              <span class="email-from">{c.from_address}</span>
              {#if c.test_status}<span class="badge {c.test_status === 'success' ? 'badge-success' : 'badge-error'}">{c.test_status}</span>{/if}
            </div>
            <div class="email-actions">
              <button class="btn btn-secondary btn-sm" on:click={() => handleEmailTest(c)}>{$t('settings.emailNotifications.test')}</button>
              <button class="btn btn-secondary btn-sm" on:click={() => openEmailEdit(c)}>{$t('common.edit')}</button>
              <button class="btn btn-danger btn-sm" on:click={() => handleEmailDelete(c)}>{$t('common.delete')}</button>
            </div>
          </div>
        {/each}
      </div>
    {/if}

    <!-- Global settings -->
    {#if globalSettings}
      <div class="section-head admin-section">
        <h4>{$t('settings.watchSources.globalHeading')}</h4>
      </div>
      <div class="global-settings">
        <label class="checkbox-row">
          <input type="checkbox" bind:checked={globalSettings.enabled} />
          <span>{$t('settings.watchSources.global.enabled')}</span>
        </label>
        <div class="form-row">
          <div class="form-group">
            <label for="gs-stab">{$t('settings.watchSources.global.fileStability')}</label>
            <input id="gs-stab" type="number" min="0" class="form-input" bind:value={globalSettings.file_stability_seconds} />
          </div>
          <div class="form-group">
            <label for="gs-max-per-scan">{$t('settings.watchSources.global.maxImportsPerScan')}</label>
            <input id="gs-max-per-scan" type="number" min="1" class="form-input" bind:value={globalSettings.max_imports_per_scan} />
            <small class="form-hint">{$t('settings.watchSources.global.maxImportsPerScanHelp')}</small>
          </div>
        </div>
        <label class="checkbox-row">
          <input type="checkbox" bind:checked={globalSettings.fs_events_enabled} />
          <span>{$t('settings.watchSources.global.fsEvents')}</span>
        </label>
        {#if globalSettings.fs_events_enabled}
          <div class="form-row">
            <div class="form-group">
              <label for="gs-fs-mode">{$t('settings.watchSources.global.fsEventsMode')}</label>
              <select id="gs-fs-mode" class="form-input" bind:value={globalSettings.fs_events_mode}>
                <option value="auto">{$t('settings.watchSources.global.fsEventsModeAuto')}</option>
                <option value="native">{$t('settings.watchSources.global.fsEventsModeNative')}</option>
                <option value="polling">{$t('settings.watchSources.global.fsEventsModePolling')}</option>
                <option value="off">{$t('settings.watchSources.global.fsEventsModeOff')}</option>
              </select>
              <small class="form-hint">{$t('settings.watchSources.global.fsEventsModeHelp')}</small>
            </div>
            <div class="form-group">
              <label for="gs-fs-poll">{$t('settings.watchSources.global.fsEventsPollSeconds')}</label>
              <input
                id="gs-fs-poll"
                type="number"
                min="1"
                class="form-input"
                bind:value={globalSettings.fs_events_poll_seconds}
              />
              <small class="form-hint">{$t('settings.watchSources.global.fsEventsPollSecondsHelp')}</small>
            </div>
          </div>
        {/if}
        <button class="btn btn-primary" on:click={saveGlobalSettings} disabled={saving}>
          {saving ? $t('common.saving') : $t('common.save')}
        </button>
      </div>
    {/if}
  {/if}
{/if}

<WatchSourceModal
  bind:show={showSourceModal}
  {editingSource}
  {capabilities}
  on:saved={handleSaved}
  on:close={() => { showSourceModal = false; editingSource = null; }}
/>

<EmailConfigModal
  bind:show={showEmailModal}
  editingConfig={editingEmail}
  on:saved={handleEmailSaved}
  on:close={() => { showEmailModal = false; editingEmail = null; }}
/>

<ConfirmationModal
  isOpen={showDeleteModal}
  title={$t('settings.watchSources.deleteConfirmTitle')}
  message={sourceToDelete ? $t('settings.watchSources.deleteConfirmMessage', { name: sourceToDelete.name }) : ''}
  confirmText={$t('common.delete')}
  cancelText={$t('common.cancel')}
  confirmButtonClass="modal-delete-button"
  cancelButtonClass="modal-cancel-button"
  on:confirm={doDelete}
  on:cancel={() => { showDeleteModal = false; sourceToDelete = null; }}
  on:close={() => { showDeleteModal = false; sourceToDelete = null; }}
/>

<style>
  .ws-loading {
    display: flex;
    justify-content: center;
    padding: 32px;
  }
  .scope-toggle {
    display: flex;
    gap: 8px;
    margin-bottom: 16px;
  }
  .section-head {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin: 8px 0 12px;
  }
  .section-head h4 {
    margin: 0;
  }
  .admin-section {
    margin-top: 28px;
    border-top: 1px solid var(--border-color);
    padding-top: 16px;
  }
  .source-list,
  .email-list {
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  .source-card,
  .email-card {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 12px;
    padding: 12px 14px;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    background: var(--surface-color);
  }
  .email-card {
    align-items: center;
  }
  .source-badges {
    display: flex;
    gap: 6px;
    margin-bottom: 4px;
    flex-wrap: wrap;
  }
  .badge {
    font-size: 0.7rem;
    padding: 2px 8px;
    border-radius: 10px;
    font-weight: 600;
  }
  .type-badge {
    background: var(--button-hover);
    color: var(--text-secondary);
  }
  .owner-badge {
    background: rgba(99, 102, 241, 0.15);
    color: var(--primary-color);
  }
  .badge-success {
    background: rgba(34, 197, 94, 0.15);
    color: var(--success-color, #16a34a);
  }
  .badge-error {
    background: rgba(239, 68, 68, 0.15);
    color: var(--error-color, #dc2626);
  }
  .badge-running {
    background: rgba(234, 179, 8, 0.15);
    color: #ca8a04;
  }
  .badge-idle {
    background: var(--button-hover);
    color: var(--text-secondary);
  }
  .source-name,
  .email-name {
    font-weight: 600;
    font-size: 0.95rem;
  }
  .email-from {
    color: var(--text-secondary);
    font-size: 0.85rem;
    margin-left: 8px;
  }
  .source-meta {
    font-size: 0.8rem;
    color: var(--text-secondary);
  }
  .scan-msg {
    font-style: italic;
  }
  .disk-banner {
    margin-top: 6px;
    font-size: 0.75rem;
    color: var(--warning-color);
  }
  .source-actions,
  .email-actions {
    display: flex;
    align-items: center;
    gap: 6px;
    flex-wrap: wrap;
  }
  .btn-sm {
    padding: 4px 10px;
    font-size: 0.8rem;
  }
  .enable-toggle {
    position: relative;
    display: inline-block;
    width: 36px;
    height: 20px;
  }
  .enable-toggle input {
    opacity: 0;
    width: 0;
    height: 0;
  }
  .slider {
    position: absolute;
    cursor: pointer;
    inset: 0;
    background: var(--border-color);
    border-radius: 20px;
    transition: 0.2s;
  }
  .slider::before {
    content: '';
    position: absolute;
    height: 14px;
    width: 14px;
    left: 3px;
    bottom: 3px;
    background: white;
    border-radius: 50%;
    transition: 0.2s;
  }
  .enable-toggle input:checked + .slider {
    background: var(--primary-color);
  }
  .enable-toggle input:checked + .slider::before {
    transform: translateX(16px);
  }
  .global-settings,
  .form-group {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .form-row {
    display: flex;
    gap: 12px;
  }
  .form-group {
    flex: 1;
    gap: 4px;
  }
  .form-group label {
    font-size: 0.85rem;
    color: var(--text-secondary);
  }
  .form-hint {
    display: block;
    margin-top: 0.35rem;
    font-size: 0.8rem;
    color: var(--text-secondary);
    font-style: italic;
  }
  /* .form-input inherits the global input styling (form-elements.css). */
  .checkbox-row {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 0.9rem;
    cursor: pointer;
  }
  /* Override the global `input { width:100% }` base so checkboxes stay square. */
  .checkbox-row input[type='checkbox'] {
    width: 16px;
    height: 16px;
    min-height: 0;
    margin: 0;
    padding: 0;
    flex: none;
    cursor: pointer;
    accent-color: var(--primary-color);
  }
  .muted {
    color: var(--text-secondary);
    font-size: 0.85rem;
  }
  .email-heading {
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .email-heading h4 {
    margin: 0;
  }
  .experimental-badge {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    font-size: 0.68rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.03em;
    padding: 2px 8px;
    border-radius: 10px;
    background: rgba(234, 179, 8, 0.15);
    color: #b45309;
  }
  :global([data-theme='dark']) .experimental-badge {
    color: #fbbf24;
  }
  .info-icon {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    background: transparent;
    border: none;
    box-shadow: none;
    padding: 2px;
    cursor: pointer;
    color: var(--text-secondary);
    border-radius: 50%;
  }
  .info-icon:hover {
    color: var(--primary-color);
    background: var(--button-hover);
    transform: none;
    box-shadow: none;
  }
  .experimental-note {
    font-size: 0.8rem;
    color: var(--text-secondary);
    margin: 0 0 10px;
  }
  .email-help {
    border: 1px solid var(--border-color);
    border-left: 3px solid var(--primary-color);
    border-radius: 6px;
    background: var(--button-hover);
    padding: 10px 14px;
    margin-bottom: 12px;
  }
  .email-help h5 {
    margin: 0 0 6px;
    font-size: 0.85rem;
  }
  .email-help p {
    margin: 4px 0;
    font-size: 0.8rem;
    color: var(--text-secondary);
    line-height: 1.5;
  }
</style>
