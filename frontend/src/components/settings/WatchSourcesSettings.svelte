<script lang="ts">
  /**
   * Watch Sources settings panel — the coordinator.
   *
   * Owns every API call and all source-of-truth state; the children under
   * `watchSources/` are presentational and dispatch events up. See
   * `watchSources/CLAUDE.md`.
   */
  import { onMount } from 'svelte';
  import Spinner from '$components/ui/Spinner.svelte';
  import EmptyState from '$components/ui/EmptyState.svelte';
  import ConfirmationModal from '../ConfirmationModal.svelte';
  import WatchSourceModal from './WatchSourceModal.svelte';
  import EmailConfigModal from './EmailConfigModal.svelte';
  import WatchSourceCard from './watchSources/WatchSourceCard.svelte';
  import EmailConfigList from './watchSources/EmailConfigList.svelte';
  import GlobalWatchSettingsForm from './watchSources/GlobalWatchSettingsForm.svelte';
  import WatchSourceFilesModal from './watchSources/WatchSourceFilesModal.svelte';
  import WatchSourceEmailLinksModal from './watchSources/WatchSourceEmailLinksModal.svelte';
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
  let filesSource: WatchSource | null = null;
  let linksSource: WatchSource | null = null;

  // admin: email configs + global settings
  let emailConfigs: EmailConfig[] = [];
  let showEmailModal = false;
  let editingEmail: EmailConfig | null = null;
  let showEmailHelp = false;
  let globalSettings: GlobalWatchSettings | null = null;
  let configToDelete: EmailConfig | null = null;

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
  async function doDeleteEmailConfig() {
    if (!configToDelete) return;
    const target = configToDelete;
    configToDelete = null;
    try {
      await deleteEmailConfig(target.uuid);
      emailConfigs = emailConfigs.filter((x) => x.uuid !== target.uuid);
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
      <button
        class="btn"
        class:btn-primary={scope === 'own'}
        class:btn-secondary={scope !== 'own'}
        on:click={() => {
          scope = 'own';
          loadSources();
        }}
      >
        {$t('settings.watchSources.myScope')}
      </button>
      <button
        class="btn"
        class:btn-primary={scope === 'all'}
        class:btn-secondary={scope !== 'all'}
        on:click={() => {
          scope = 'all';
          loadSources();
        }}
      >
        {$t('settings.watchSources.allScope')}
      </button>
    </div>
  {/if}

  <div class="section-head">
    <h4>{$t('settings.watchSources.sourcesHeading')}</h4>
    <button class="btn btn-primary" on:click={openCreate}>
      + {$t('settings.watchSources.addSource')}
    </button>
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
        <WatchSourceCard
          source={s}
          stats={statsMap[s.uuid]}
          {capabilities}
          {saving}
          testing={testingUuid === s.uuid}
          on:toggle={(e) => toggleEnabled(e.detail)}
          on:test={(e) => handleTest(e.detail)}
          on:scan={(e) => handleScan(e.detail)}
          on:edit={(e) => openEdit(e.detail)}
          on:delete={(e) => confirmDelete(e.detail)}
          on:files={(e) => (filesSource = e.detail)}
          on:notifications={(e) => (linksSource = e.detail)}
        />
      {/each}
    </div>
  {/if}

  {#if isSuperAdmin}
    <EmailConfigList
      configs={emailConfigs}
      showHelp={showEmailHelp}
      on:create={openEmailCreate}
      on:edit={(e) => openEmailEdit(e.detail)}
      on:test={(e) => handleEmailTest(e.detail)}
      on:delete={(e) => (configToDelete = e.detail)}
      on:toggleHelp={() => (showEmailHelp = !showEmailHelp)}
    />

    {#if globalSettings}
      <GlobalWatchSettingsForm bind:settings={globalSettings} {saving} on:save={saveGlobalSettings} />
    {/if}
  {/if}
{/if}

<WatchSourceModal
  bind:show={showSourceModal}
  {editingSource}
  {capabilities}
  on:saved={handleSaved}
  on:close={() => {
    showSourceModal = false;
    editingSource = null;
  }}
/>

<EmailConfigModal
  bind:show={showEmailModal}
  editingConfig={editingEmail}
  on:saved={handleEmailSaved}
  on:close={() => {
    showEmailModal = false;
    editingEmail = null;
  }}
/>

<WatchSourceFilesModal
  show={filesSource !== null}
  source={filesSource}
  on:changed={loadSources}
  on:close={() => (filesSource = null)}
/>

<WatchSourceEmailLinksModal
  show={linksSource !== null}
  source={linksSource}
  on:close={() => (linksSource = null)}
/>

<ConfirmationModal
  isOpen={showDeleteModal}
  title={$t('settings.watchSources.deleteConfirmTitle')}
  message={sourceToDelete
    ? $t('settings.watchSources.deleteConfirmMessage', { name: sourceToDelete.name })
    : ''}
  confirmText={$t('common.delete')}
  cancelText={$t('common.cancel')}
  confirmButtonClass="modal-delete-button"
  cancelButtonClass="modal-cancel-button"
  on:confirm={doDelete}
  on:cancel={() => {
    showDeleteModal = false;
    sourceToDelete = null;
  }}
  on:close={() => {
    showDeleteModal = false;
    sourceToDelete = null;
  }}
/>

<ConfirmationModal
  isOpen={configToDelete !== null}
  title={$t('settings.emailNotifications.links.deleteConfigTitle')}
  message={configToDelete
    ? $t('settings.emailNotifications.links.deleteConfigMessage', {
        name: configToDelete.name,
        count: configToDelete.linked_source_count ?? 0,
      })
    : ''}
  confirmText={$t('common.delete')}
  cancelText={$t('common.cancel')}
  confirmButtonClass="modal-delete-button"
  cancelButtonClass="modal-cancel-button"
  on:confirm={doDeleteEmailConfig}
  on:cancel={() => (configToDelete = null)}
  on:close={() => (configToDelete = null)}
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
  .source-list {
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
</style>
