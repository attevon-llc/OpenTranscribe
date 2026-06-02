<script lang="ts">
  import { onMount } from 'svelte';
  import { lockScroll, unlockScroll } from '$lib/scrollLock';
  import Spinner from '../ui/Spinner.svelte';
  import {
    ASRSettingsApi,
    type UserASRSettingsResponse,
    type ASRProviderInfo,
  } from '../../lib/api/asrSettings';
  import ConfirmationModal from '../ConfirmationModal.svelte';
  import ASRConfigModal from './ASRConfigModal.svelte';
  import { toastStore } from '../../stores/toast';
  import { t } from '$stores/locale';
  import { getErrorMessage } from '$lib/utils/apiError';

  export let onSettingsChange: (() => void) | null = null;
  export let isAdmin = false;

  let loading = false;
  let saving = false;
  let testing = false;

  let configurations: UserASRSettingsResponse[] = [];
  let sharedConfigurations: UserASRSettingsResponse[] = [];
  let activeConfigurationId: string | null = null;
  let providers: ASRProviderInfo[] = [];
  let usingLocalDefault = true;

  function isProviderExperimental(providerId: string): boolean {
    const p = providers.find(pr => pr.id === providerId);
    return p?.status === 'experimental';
  }

  // Local model state (visible to all users; admin can change)
  let activeLocalModel = '';
  let activeLocalModelSource: 'database' | 'environment' = 'environment';
  let availableLocalModels: { short_name: string; repo_id: string; downloaded: boolean }[] = [];
  let localModelInfo: {
    display_name: string;
    description: string;
    supports_translation: boolean;
    supports_diarization: boolean;
    language_support: string;
  } | null = null;
  let selectedLocalModel = '';
  let modelChangeInProgress = false;
  let restartInProgress = false;

  let showConfigModal = false;
  let editingConfiguration: UserASRSettingsResponse | null = null;

  let showDeleteModal = false;
  let configToDelete: UserASRSettingsResponse | null = null;
  let showDeleteAllModal = false;

  let testingConfigId: string | null = null;

  onMount(async () => {
    await loadData();
  });

  async function loadData() {
    loading = true;
    try {
      const [providersResp, settingsResp, localModelResp] = await Promise.all([
        ASRSettingsApi.getProviders(),
        ASRSettingsApi.getSettings().catch(() => ({ configurations: [], shared_configurations: [], active_configuration_id: undefined, total: 0 })),
        ASRSettingsApi.getActiveLocalModel().catch(() => null),
      ]);
      providers = providersResp.providers;
      configurations = settingsResp.configurations;
      sharedConfigurations = settingsResp.shared_configurations || [];
      activeConfigurationId = settingsResp.active_configuration_id || null;
      usingLocalDefault = configurations.length === 0 || !activeConfigurationId;

      // Load local model info (visible to all users)
      if (localModelResp) {
        activeLocalModel = localModelResp.active_model;
        activeLocalModelSource = localModelResp.source;
        availableLocalModels = localModelResp.available_models || [];
        localModelInfo = localModelResp.model_info || null;
        selectedLocalModel = activeLocalModel;
      }
    } catch (err: unknown) {
      console.error('Error loading ASR settings:', err);
      toastStore.error(getErrorMessage(err, $t('settings.asrProvider.loadError')), 5000);
    } finally {
      loading = false;
    }
  }

  async function activateConfiguration(uuid: string) {
    if (uuid === activeConfigurationId) return;
    saving = true;
    try {
      await ASRSettingsApi.setActive(uuid);
      activeConfigurationId = uuid;
      usingLocalDefault = false;
      toastStore.success($t('settings.asrProvider.setActive') + ' — ' + (configurations.find(c => c.uuid === uuid)?.name || ''));
      if (onSettingsChange) onSettingsChange();
    } catch (err: unknown) {
      toastStore.error(getErrorMessage(err, 'Failed to activate ASR configuration'), 5000);
    } finally {
      saving = false;
    }
  }

  async function switchToLocal() {
    if (usingLocalDefault) return;
    saving = true;
    try {
      await ASRSettingsApi.clearActive();
      activeConfigurationId = null;
      usingLocalDefault = true;
      toastStore.success($t('settings.asrProvider.switchedToLocal'));
      if (onSettingsChange) onSettingsChange();
    } catch (err: unknown) {
      toastStore.error(getErrorMessage(err, 'Failed to switch to local'), 5000);
    } finally {
      saving = false;
    }
  }

  async function testConfiguration(config: UserASRSettingsResponse) {
    testingConfigId = config.uuid;
    testing = true;
    try {
      const result = await ASRSettingsApi.testSavedConfig(config.uuid);
      // Refresh to pick up updated test_status
      await loadData();
      if (result.success) {
        toastStore.success(`${config.name}: ${result.message}`, 5000);
      } else {
        toastStore.error(`${config.name}: ${result.message}`, 8000);
      }
    } catch (err: unknown) {
      toastStore.error(`${config.name}: ${getErrorMessage(err, 'Test failed')}`, 8000);
    } finally {
      testing = false;
      testingConfigId = null;
    }
  }

  function openCreateModal() {
    editingConfiguration = null;
    showConfigModal = true;
  }

  function openEditModal(config: UserASRSettingsResponse) {
    editingConfiguration = config;
    showConfigModal = true;
  }

  function confirmDelete(config: UserASRSettingsResponse) {
    configToDelete = config;
    showDeleteModal = true;
  }

  async function deleteConfiguration() {
    if (!configToDelete) return;
    saving = true;
    try {
      const deletedName = configToDelete.name;
      await ASRSettingsApi.deleteConfig(configToDelete.uuid);
      configToDelete = null;
      showDeleteModal = false;
      // Re-fetch canonical state to pick up auto-promoted active config
      await loadData();
      toastStore.success(`"${deletedName}" deleted`);
      if (onSettingsChange) onSettingsChange();
    } catch (err: unknown) {
      toastStore.error(getErrorMessage(err, 'Failed to delete configuration'), 5000);
    } finally {
      saving = false;
    }
  }

  async function deleteAllConfigurations() {
    saving = true;
    try {
      await ASRSettingsApi.deleteAll();
      configurations = [];
      activeConfigurationId = null;
      usingLocalDefault = true;
      showDeleteAllModal = false;
      toastStore.success($t('settings.asrProvider.deletedAll'));
      if (onSettingsChange) onSettingsChange();
    } catch (err: unknown) {
      toastStore.error(getErrorMessage(err, 'Failed to delete configurations'), 5000);
    } finally {
      saving = false;
    }
  }

  async function handleConfigSaved(event: CustomEvent<UserASRSettingsResponse>) {
    // Re-fetch canonical state from backend to pick up auto-activation, etc.
    await loadData();
    if (onSettingsChange) onSettingsChange();
  }

  function getActiveConfig(): UserASRSettingsResponse | null {
    return configurations.find(c => c.uuid === activeConfigurationId)
      || sharedConfigurations.find(c => c.uuid === activeConfigurationId) || null;
  }

  async function handleShareToggle(config: UserASRSettingsResponse) {
    const newShared = !config.is_shared;
    const idx = configurations.findIndex(c => c.uuid === config.uuid);
    const prevSharedAt = config.shared_at;

    // Optimistic update — flip toggle immediately for responsive UI
    if (idx !== -1) {
      configurations[idx] = {
        ...configurations[idx],
        is_shared: newShared,
        shared_at: newShared ? new Date().toISOString() : undefined
      };
      configurations = configurations;
    }

    saving = true;
    try {
      await ASRSettingsApi.toggleShare(config.uuid, newShared);
      toastStore.success(
        newShared ? $t('settings.asrProvider.shareEnabled') : $t('settings.asrProvider.shareDisabled'),
        3000
      );
    } catch (err: unknown) {
      // Rollback on failure
      if (idx !== -1) {
        configurations[idx] = {
          ...configurations[idx],
          is_shared: !newShared,
          shared_at: prevSharedAt
        };
        configurations = configurations;
      }
      toastStore.error(getErrorMessage(err, 'Failed to toggle sharing'), 5000);
    } finally {
      saving = false;
    }
  }

  function getTestStatusIcon(config: UserASRSettingsResponse): string {
    if (config.test_status === 'success') return '✓';
    if (config.test_status === 'failed') return '✗';
    return '';
  }

  function getTestStatusClass(config: UserASRSettingsResponse): string {
    if (config.test_status === 'success') return 'test-success';
    if (config.test_status === 'failed') return 'test-failed';
    return '';
  }

  // Admin: set local model and restart worker
  async function handleSetLocalModel() {
    if (!selectedLocalModel || selectedLocalModel === activeLocalModel) return;
    modelChangeInProgress = true;
    try {
      await ASRSettingsApi.setLocalModel(selectedLocalModel);
      activeLocalModel = selectedLocalModel;
      activeLocalModelSource = 'database';
      toastStore.success(`Local model set to "${selectedLocalModel}". Restart GPU worker to apply.`);
    } catch (err: unknown) {
      toastStore.error(getErrorMessage(err, 'Failed to set local model'), 5000);
    } finally {
      modelChangeInProgress = false;
    }
  }

  async function handleRestartGpuWorker() {
    restartInProgress = true;
    try {
      const result = await ASRSettingsApi.restartGpuWorker();
      toastStore.success(result.message, 8000);
    } catch (err: unknown) {
      toastStore.error(getErrorMessage(err, 'Failed to restart GPU worker'), 5000);
    } finally {
      restartInProgress = false;
    }
  }

  $: localModelChanged = selectedLocalModel && selectedLocalModel !== activeLocalModel;

  let _asrModalWasOpen = false;
  $: {
    const _anyOpen = showConfigModal || showDeleteModal || showDeleteAllModal;
    if (_anyOpen !== _asrModalWasOpen) {
      _anyOpen ? lockScroll() : unlockScroll();
      _asrModalWasOpen = _anyOpen;
    }
  }
</script>

<div class="asr-settings">
  {#if loading}
    <div class="loading">{$t('settings.asrProvider.loading')}</div>
  {:else}
    <!-- Local GPU Model Info (all users) / Controls (admin only) -->
    {#if activeLocalModel}
      <div class="local-model-section" class:admin-mode={isAdmin}>
        <div class="admin-section-header">
          <h4>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <rect x="4" y="4" width="16" height="16" rx="2" ry="2"/><rect x="9" y="9" width="6" height="6"/>
              <line x1="9" y1="1" x2="9" y2="4"/><line x1="15" y1="1" x2="15" y2="4"/>
              <line x1="9" y1="20" x2="9" y2="23"/><line x1="15" y1="20" x2="15" y2="23"/>
              <line x1="20" y1="9" x2="23" y2="9"/><line x1="20" y1="14" x2="23" y2="14"/>
              <line x1="1" y1="9" x2="4" y2="9"/><line x1="1" y1="14" x2="4" y2="14"/>
            </svg>
            Local GPU Model
          </h4>
          {#if isAdmin}
            <span class="admin-badge-tag">Admin</span>
          {/if}
        </div>

        <!-- Model info — visible to everyone -->
        <div class="model-info-row">
          <div class="model-name-display">
            <span class="model-label">{localModelInfo?.display_name || activeLocalModel}</span>
            {#if localModelInfo?.description}
              <span class="model-desc">{localModelInfo.description}</span>
            {/if}
          </div>
          <div class="model-capabilities">
            {#if localModelInfo}
              <span class="cap-badge" class:cap-yes={localModelInfo.supports_diarization} class:cap-no={!localModelInfo.supports_diarization}>
                {localModelInfo.supports_diarization ? '✓' : '✗'} Diarization
              </span>
              <span class="cap-badge" class:cap-yes={localModelInfo.supports_translation} class:cap-no={!localModelInfo.supports_translation}>
                {localModelInfo.supports_translation ? '✓' : '✗'} Translation
              </span>
              <span class="cap-badge cap-neutral">
                {localModelInfo.language_support === 'english_optimized' ? 'English optimized' : 'Multilingual'}
              </span>
            {/if}
          </div>
        </div>

        {#if isAdmin}
          <!-- Admin controls -->
          <p class="admin-description">
            The local Whisper model is loaded into GPU VRAM at worker startup and shared by all users. Changing the model requires a GPU worker restart.
          </p>
          <div class="model-control-row">
            <div class="model-select-group">
              <label for="local-model-select">Active Model</label>
              <div class="model-select-with-source">
                <select id="local-model-select" bind:value={selectedLocalModel} disabled={modelChangeInProgress || restartInProgress} class="form-select">
                  {#if availableLocalModels.length > 0}
                    {#each availableLocalModels as model}
                      <option value={model.short_name}>{model.short_name}</option>
                    {/each}
                    {#if !availableLocalModels.find(m => m.short_name === activeLocalModel)}
                      <option value={activeLocalModel}>{activeLocalModel} (not downloaded)</option>
                    {/if}
                  {:else}
                    <option value={activeLocalModel}>{activeLocalModel}</option>
                  {/if}
                </select>
                <span class="model-source">Source: {activeLocalModelSource === 'database' ? 'Database' : 'Environment variable'}</span>
              </div>
            </div>
            <div class="model-actions">
              {#if localModelChanged}
                <button class="btn btn-primary" on:click={handleSetLocalModel} disabled={modelChangeInProgress}>
                  {#if modelChangeInProgress}
                    <Spinner size="small" /> Saving...
                  {:else}
                    Save Model
                  {/if}
                </button>
              {/if}
              <button class="btn btn-warning" on:click={handleRestartGpuWorker} disabled={restartInProgress || modelChangeInProgress}>
                {#if restartInProgress}
                  <Spinner size="small" /> Restarting...
                {:else}
                  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <polyline points="1 4 1 10 7 10"/><polyline points="23 20 23 14 17 14"/>
                    <path d="m20.49 9A9 9 0 0 0 5.64 5.64L1 10m22 4l-4.64 4.36A9 9 0 0 1 3.51 15"/>
                  </svg>
                  Restart GPU Worker
                {/if}
              </button>
            </div>
          </div>
          {#if availableLocalModels.length > 0}
            <div class="downloaded-count">{availableLocalModels.length} model{availableLocalModels.length !== 1 ? 's' : ''} downloaded locally</div>
          {/if}
        {/if}
      </div>
    {/if}

    <!-- Active provider status bar -->
    <div class="status-bar" class:local={usingLocalDefault}>
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <circle cx="12" cy="12" r="3"/><path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"/>
      </svg>
      {#if usingLocalDefault}
        {$t('settings.asrProvider.usingLocalDefault')}{#if activeLocalModel} — <strong>{activeLocalModel}</strong>{/if}
      {:else}
        <span class="status-text">
          {$t('settings.asrProvider.currentProvider')}: <strong>{getActiveConfig()?.name || ''}</strong>
          ({ASRSettingsApi.getProviderDisplayName(getActiveConfig()?.provider || '')} — {getActiveConfig()?.model_name || ''})
        </span>
        <button class="btn-use-local" on:click={switchToLocal} disabled={saving}>
          {$t('settings.asrProvider.useLocal')}
        </button>
      {/if}
    </div>

    <!-- Section header -->
    <div class="section-header">
      <h4>
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"/>
        </svg>
        {$t('settings.asrProvider.title')}
      </h4>
      {#if configurations.length > 0 || sharedConfigurations.length > 0}
        <button class="add-button" on:click={openCreateModal} disabled={saving}>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>
          </svg>
          {$t('settings.asrProvider.addConfig')}
        </button>
      {/if}
    </div>

    {#if configurations.length > 0}
      <div class="config-list">
        {#each configurations as config (config.uuid)}
          <div class="config-card" class:active={config.uuid === activeConfigurationId}>
            <div class="config-info">
              <div class="config-badges">
                <span class="badge provider-badge">{ASRSettingsApi.getProviderDisplayName(config.provider)}</span>
                {#if isProviderExperimental(config.provider)}
                  <span class="badge experimental-badge">{$t('settings.asrProvider.statusExperimental')}</span>
                {/if}
                {#if config.uuid === activeConfigurationId}
                  <span class="badge active-badge">{$t('settings.asrProvider.currentlyActive')}</span>
                {/if}
                {#if config.test_status && config.test_status !== 'untested'}
                  <span class="badge test-badge {getTestStatusClass(config)}">{getTestStatusIcon(config)} {$t(`settings.asrProvider.status.${config.test_status === 'success' ? 'connected' : 'failed'}`)}</span>
                {/if}
              </div>
              <div class="config-name">{config.name}
                {#if config.is_shared}<span class="share-badge">{$t('settings.asrProvider.shared')}</span>{/if}
              </div>
              <div class="config-model">{config.model_name}</div>
              <div class="share-toggle-row">
                <label class="toggle-label">
                  <input type="checkbox" class="toggle-input" checked={config.is_shared}
                    on:change={() => handleShareToggle(config)} disabled={saving} />
                  <span class="toggle-switch"></span>
                  <span class="toggle-text">{$t('settings.asrProvider.shareGlobally')}</span>
                </label>
              </div>
            </div>

            <div class="config-actions">
              {#if config.uuid !== activeConfigurationId}
                <button
                  class="btn-activate"
                  on:click={() => activateConfiguration(config.uuid)}
                  disabled={saving}
                  title={$t('settings.asrProvider.setActive')}
                >
                  {$t('settings.asrProvider.setActive')}
                </button>
              {/if}
              <button
                class="btn-icon btn-test"
                on:click={() => testConfiguration(config)}
                disabled={testing}
                title={$t('settings.asrProvider.testConnection')}
              >
                {#if testing && testingConfigId === config.uuid}
                  <Spinner size="small" />
                {:else}
                  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <polyline points="1 4 1 10 7 10"/><polyline points="23 20 23 14 17 14"/>
                    <path d="m20.49 9A9 9 0 0 0 5.64 5.64L1 10m22 4l-4.64 4.36A9 9 0 0 1 3.51 15"/>
                  </svg>
                {/if}
              </button>
              <button
                class="btn-icon btn-edit"
                on:click={() => openEditModal(config)}
                disabled={saving}
                title={$t('settings.asrProvider.editConfig')}
              >
                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>
                  <path d="m18.5 2.5 a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>
                </svg>
              </button>
              <button
                class="btn-icon btn-delete"
                on:click={() => confirmDelete(config)}
                disabled={saving}
                title={$t('settings.asrProvider.deleteConfirmTitle')}
              >
                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <polyline points="3,6 5,6 21,6"/>
                  <path d="m19,6v14a2,2 0 0,1 -2,2H7a2,2 0 0,1 -2,-2V6m3,0V4a2,2 0 0,1 2,-2h4a2,2 0 0,1 2,2v2"/>
                </svg>
              </button>
            </div>
          </div>
        {/each}
      </div>

      <div class="delete-all-section">
        <button class="btn-delete-all" on:click={() => showDeleteAllModal = true} disabled={saving}>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <polyline points="3,6 5,6 21,6"/>
            <path d="m19,6v14a2,2 0 0,1 -2,2H7a2,2 0 0,1 -2,-2V6m3,0V4a2,2 0 0,1 2,-2h4a2,2 0 0,1 2,2v2"/>
          </svg>
          {$t('settings.asrProvider.deleteAll')}
        </button>
      </div>
    {/if}

    <!-- Shared by Others -->
    {#if sharedConfigurations.length > 0}
      <div class="section-header shared-section-header">
        <h4>
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/>
            <path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/>
          </svg>
          {$t('settings.asrProvider.sharedByOthers')}
        </h4>
      </div>
      <div class="config-list">
        {#each sharedConfigurations as config (config.uuid)}
          <div class="config-card shared" class:active={config.uuid === activeConfigurationId}>
            <div class="config-info">
              <div class="config-badges">
                <span class="badge provider-badge">{ASRSettingsApi.getProviderDisplayName(config.provider)}</span>
                {#if config.uuid === activeConfigurationId}
                  <span class="badge active-badge">{$t('settings.asrProvider.currentlyActive')}</span>
                {/if}
                {#if config.owner_role === 'admin' || config.owner_role === 'super_admin'}
                  <span class="badge admin-badge">{$t('settings.sharing.adminBadge')}</span>
                {/if}
              </div>
              <div class="config-name">{config.name}</div>
              <div class="config-model">{config.model_name}</div>
              {#if config.owner_name}
                <div class="shared-by">{$t('settings.asrProvider.sharedBy', { name: config.owner_name })}</div>
              {/if}
            </div>
            <div class="config-actions">
              {#if config.uuid !== activeConfigurationId}
                <button class="btn-activate" on:click={() => activateConfiguration(config.uuid)} disabled={saving}>
                  {$t('settings.asrProvider.setActive')}
                </button>
              {/if}
              <button class="btn-icon btn-test" on:click={() => testConfiguration(config)} disabled={testing}>
                {#if testing && testingConfigId === config.uuid}
                  <Spinner size="small" />
                {:else}
                  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <polyline points="1 4 1 10 7 10"/><polyline points="23 20 23 14 17 14"/>
                    <path d="m20.49 9A9 9 0 0 0 5.64 5.64L1 10m22 4l-4.64 4.36A9 9 0 0 1 3.51 15"/>
                  </svg>
                {/if}
              </button>
            </div>
          </div>
        {/each}
      </div>
    {/if}

    {#if configurations.length === 0 && sharedConfigurations.length === 0}
      <div class="empty-state">
        <div class="empty-icon">
          <svg width="44" height="44" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
            <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/>
            <path d="M19 10v2a7 7 0 0 1-14 0v-2"/>
            <line x1="12" y1="19" x2="12" y2="23"/>
            <line x1="8" y1="23" x2="16" y2="23"/>
          </svg>
        </div>
        <h4>{$t('settings.asrProvider.emptyTitle')}</h4>
        <p>{$t('settings.asrProvider.emptyDescription')}</p>
        <button class="btn-add-first" on:click={openCreateModal}>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>
          </svg>
          {$t('settings.asrProvider.addConfig')}
        </button>
      </div>
    {/if}
  {/if}
</div>

<ASRConfigModal
  bind:show={showConfigModal}
  editingConfig={editingConfiguration}
  {providers}
  on:saved={handleConfigSaved}
  on:close={() => { showConfigModal = false; editingConfiguration = null; }}
/>

<ConfirmationModal
  bind:isOpen={showDeleteModal}
  title={$t('settings.asrProvider.deleteConfirmTitle')}
  message={configToDelete ? $t('settings.asrProvider.deleteConfirmMessage', { name: configToDelete.name }) : ''}
  confirmText="Delete"
  cancelText={$t('common.cancel')}
  confirmButtonClass="modal-delete-button"
  cancelButtonClass="modal-cancel-button"
  on:confirm={deleteConfiguration}
  on:cancel={() => { configToDelete = null; showDeleteModal = false; }}
  on:close={() => { configToDelete = null; showDeleteModal = false; }}
/>

<ConfirmationModal
  bind:isOpen={showDeleteAllModal}
  title={$t('settings.asrProvider.deleteAllConfirmTitle')}
  message={$t('settings.asrProvider.deleteAllConfirmMessage')}
  confirmText={$t('settings.asrProvider.deleteAll')}
  cancelText={$t('common.cancel')}
  confirmButtonClass="modal-delete-button"
  cancelButtonClass="modal-cancel-button"
  on:confirm={deleteAllConfigurations}
  on:cancel={() => showDeleteAllModal = false}
  on:close={() => showDeleteAllModal = false}
/>

<style>
  .asr-settings {
    max-width: 800px;
    margin: 0 auto;
  }

  .loading {
    text-align: center;
    padding: 3rem;
    color: var(--text-muted);
    font-size: 0.8125rem;
  }

  .status-bar {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.625rem 1rem;
    border-radius: 6px;
    margin-bottom: 1.25rem;
    font-size: 0.8125rem;
    background: var(--primary-bg, rgba(var(--primary-color-rgb), 0.08));
    color: var(--primary-color, #3b82f6);
    border: 1px solid var(--primary-border, rgba(var(--primary-color-rgb), 0.2));
  }

  .status-bar.local {
    background: var(--card-bg);
    color: var(--text-muted);
    border-color: var(--border-color);
  }

  .status-text { flex: 1; }

  .btn-use-local {
    margin-left: auto;
    padding: 0.25rem 0.75rem;
    font-size: 0.75rem;
    border-radius: 4px;
    border: 1px solid var(--border-color);
    background: var(--surface-color);
    color: var(--text-color);
    cursor: pointer;
    white-space: nowrap;
  }
  .btn-use-local:hover:not(:disabled) { background: var(--button-hover); }

  .section-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 1rem;
    padding-bottom: 0.5rem;
    border-bottom: 1px solid var(--border-color);
  }

  .section-header h4 {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    margin: 0;
    font-size: 1.125rem;
    font-weight: 500;
    color: var(--text-color);
  }

  .add-button {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    background: #3b82f6;
    color: white;
    border: none;
    padding: 0.5rem 1rem;
    border-radius: 8px;
    cursor: pointer;
    font-size: 0.8125rem;
    font-weight: 500;
    transition: background 0.2s;
  }

  .add-button:hover:not(:disabled) {
    background: #2563eb;
  }

  .config-list {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
  }

  .config-card {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0.875rem 1rem;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    background: var(--card-bg);
    transition: all 0.15s ease;
    gap: 1rem;
  }

  .config-card.active {
    border-color: var(--primary-color, #3b82f6);
    background: var(--primary-bg, rgba(var(--primary-color-rgb), 0.05));
  }

  .config-card:hover:not(.active) {
    border-color: var(--border-hover, #6b7280);
  }

  .config-info {
    flex: 1;
    min-width: 0;
  }

  .config-badges {
    display: flex;
    flex-wrap: wrap;
    gap: 0.375rem;
    margin-bottom: 0.375rem;
  }

  .badge {
    display: inline-flex;
    align-items: center;
    padding: 0.15rem 0.5rem;
    border-radius: 4px;
    font-size: 0.7rem;
    font-weight: 500;
  }

  .provider-badge {
    background: rgba(var(--primary-color-rgb), 0.12);
    color: var(--primary-color);
  }

  .experimental-badge {
    background: rgba(245, 158, 11, 0.12);
    color: var(--warning-color, #d97706);
  }

  .active-badge {
    background: var(--success-bg, rgba(16, 185, 129, 0.1));
    color: var(--success-color, #10b981);
    border: 1px solid var(--success-border, rgba(16, 185, 129, 0.2));
  }

  .test-badge { font-size: 0.7rem; }
  .test-badge.test-success { background: rgba(16, 185, 129, 0.1); color: #10b981; }
  .test-badge.test-failed { background: rgba(239, 68, 68, 0.1); color: #ef4444; }

  .config-name {
    font-weight: 500;
    font-size: 0.875rem;
    color: var(--text-color);
  }

  .config-model {
    font-size: 0.775rem;
    color: var(--text-muted);
    margin-top: 0.1rem;
  }

  .config-actions {
    display: flex;
    align-items: center;
    gap: 0.375rem;
    flex-shrink: 0;
  }

  .btn-activate {
    padding: 0.375rem 0.75rem;
    border-radius: 5px;
    font-size: 0.775rem;
    font-weight: 500;
    cursor: pointer;
    background: var(--success-color, #10b981);
    color: white;
    border: none;
    transition: background 0.15s;
    white-space: nowrap;
  }

  .btn-activate:hover:not(:disabled) { background: #059669; }

  .btn-icon {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 30px;
    height: 30px;
    padding: 0;
    border-radius: 5px;
    cursor: pointer;
    transition: all 0.15s;
    border: 1px solid;
    box-shadow: none;
  }

  .btn-test {
    background: transparent;
    border-color: var(--primary-color);
    color: var(--primary-color);
  }

  .btn-test:hover:not(:disabled) {
    background: #3b82f6;
    color: white;
  }

  .btn-edit {
    background: transparent;
    border-color: var(--border-color);
    color: var(--text-color);
  }

  .btn-edit:hover:not(:disabled) {
    background: #3b82f6;
    border-color: var(--primary-color);
    color: white;
  }

  .btn-delete {
    background: transparent;
    border-color: var(--error-color, #ef4444);
    color: var(--error-color, #ef4444);
  }

  .btn-delete:hover:not(:disabled) {
    background: var(--error-color, #ef4444);
    color: white;
  }

  .delete-all-section {
    margin-top: 1.5rem;
    padding-top: 1rem;
    border-top: 1px solid var(--border-color);
    text-align: center;
  }

  .btn-delete-all {
    display: inline-flex;
    align-items: center;
    gap: 0.5rem;
    background: #ef4444;
    color: white;
    border: none;
    padding: 0.5rem 1rem;
    border-radius: 8px;
    cursor: pointer;
    font-size: 0.8125rem;
    font-weight: 500;
    transition: background 0.2s;
  }

  .btn-delete-all:hover:not(:disabled) { background: #dc2626; }

  .empty-state {
    text-align: center;
    padding: 2.5rem 2rem;
    border: 2px dashed var(--border-color);
    border-radius: 8px;
    background: var(--card-bg);
  }

  .empty-icon {
    margin-bottom: 0.875rem;
    color: var(--text-muted);
    opacity: 0.6;
  }

  .empty-state h4 {
    margin: 0 0 0.5rem;
    font-size: 1rem;
    font-weight: 500;
    color: var(--text-color);
  }

  .empty-state p {
    margin: 0 0 1.25rem;
    font-size: 0.8125rem;
    color: var(--text-muted);
    line-height: 1.5;
  }

  .btn-add-first {
    display: inline-flex;
    align-items: center;
    gap: 0.5rem;
    background: #3b82f6;
    color: white;
    border: none;
    padding: 0.5rem 1.1rem;
    border-radius: 8px;
    cursor: pointer;
    font-size: 0.8125rem;
    font-weight: 500;
    transition: background 0.2s;
  }

  .btn-add-first:hover { background: #2563eb; }

  button:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .config-card.shared {
    border-left: 3px solid var(--info-color, #3b82f6);
    background: rgba(var(--primary-color-rgb), 0.04);
  }
  :global([data-theme='dark']) .config-card.shared { background: rgba(96, 165, 250, 0.06); }

  .shared-section-header { margin-top: 1.5rem; }

  .share-badge {
    display: inline-flex; align-items: center;
    padding: 1px 6px; border-radius: 10px;
    font-size: 0.625rem; font-weight: 600;
    text-transform: uppercase; letter-spacing: 0.5px;
    background: rgba(var(--primary-color-rgb), 0.12); color: var(--primary-color);
    margin-left: 0.5rem; vertical-align: middle;
  }
  :global([data-theme='dark']) .share-badge { background: rgba(var(--primary-color-rgb), 0.2); color: #60a5fa; }

  .admin-badge {
    background: rgba(245, 158, 11, 0.12); color: #d97706;
  }
  :global([data-theme='dark']) .admin-badge { background: rgba(245, 158, 11, 0.2); color: #fbbf24; }

  .shared-by {
    font-size: 0.725rem; color: var(--text-muted);
    font-style: italic; margin-top: 0.125rem;
  }

  .share-toggle-row {
    display: flex; align-items: center; gap: 0.75rem;
    margin-top: 0.5rem; padding-top: 0.5rem;
    border-top: 1px dashed var(--border-color);
  }

  .toggle-label {
    display: flex; align-items: center; gap: 0.5rem;
    cursor: pointer; font-size: 0.75rem; color: var(--text-muted);
  }

  .toggle-input { display: none; }

  .toggle-switch {
    position: relative; width: 28px; height: 16px;
    background: var(--border-color); border-radius: 8px;
    transition: background 0.2s; flex-shrink: 0;
  }
  .toggle-switch::after {
    content: ''; position: absolute; top: 2px; left: 2px;
    width: 12px; height: 12px; background: white;
    border-radius: 50%; transition: transform 0.2s;
  }
  .toggle-input:checked + .toggle-switch { background: #3b82f6; }
  .toggle-input:checked + .toggle-switch::after { transform: translateX(12px); }
  .toggle-text { user-select: none; }

  /* Local model section (all users) */
  .local-model-section {
    padding: 1rem;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    background: var(--card-bg);
    margin-bottom: 1.25rem;
  }

  .model-info-row {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 1rem;
    flex-wrap: wrap;
    margin-bottom: 0.25rem;
  }

  .local-model-section.admin-mode .model-info-row {
    margin-bottom: 0.75rem;
    padding-bottom: 0.75rem;
    border-bottom: 1px dashed var(--border-color);
  }

  .model-name-display {
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
  }

  .model-label {
    font-size: 0.9375rem;
    font-weight: 600;
    color: var(--text-color);
  }

  .model-desc {
    font-size: 0.75rem;
    color: var(--text-muted);
  }

  .model-capabilities {
    display: flex;
    align-items: center;
    gap: 0.375rem;
    flex-wrap: wrap;
  }

  .cap-badge {
    display: inline-flex;
    align-items: center;
    padding: 0.15rem 0.5rem;
    border-radius: 4px;
    font-size: 0.7rem;
    font-weight: 500;
    white-space: nowrap;
  }

  .cap-yes {
    background: var(--success-bg, rgba(16, 185, 129, 0.1));
    color: var(--success-color, #10b981);
  }

  .cap-no {
    background: rgba(239, 68, 68, 0.08);
    color: var(--error-color, #ef4444);
  }

  .cap-neutral {
    background: rgba(var(--primary-color-rgb), 0.08);
    color: var(--primary-color);
  }

  .admin-section-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 0.5rem;
  }

  .admin-section-header h4 {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    margin: 0;
    font-size: 0.9375rem;
    font-weight: 500;
    color: var(--text-color);
  }

  .admin-badge-tag {
    display: inline-flex;
    align-items: center;
    padding: 0.15rem 0.5rem;
    border-radius: 4px;
    font-size: 0.675rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    background: rgba(245, 158, 11, 0.12);
    color: #d97706;
  }
  :global([data-theme='dark']) .admin-badge-tag {
    background: rgba(245, 158, 11, 0.2);
    color: #fbbf24;
  }

  .admin-description {
    margin: 0 0 0.875rem;
    font-size: 0.775rem;
    color: var(--text-muted);
    line-height: 1.5;
  }

  .model-control-row {
    display: flex;
    align-items: flex-start;
    gap: 0.875rem;
    flex-wrap: wrap;
  }

  .model-select-group {
    flex: 1;
    min-width: 200px;
  }

  .model-select-group label {
    display: block;
    font-size: 0.75rem;
    font-weight: 500;
    color: var(--text-muted);
    margin-bottom: 0.375rem;
  }

  .model-select-with-source {
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
  }

  .model-select-group .form-select {
    width: 100%;
    padding: 0.5rem 0.75rem;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    background: var(--surface-color, var(--card-bg));
    color: var(--text-color);
    font-size: 0.8125rem;
    height: 36px;
  }

  .model-source {
    font-size: 0.675rem;
    color: var(--text-muted);
    font-style: italic;
  }

  .model-actions {
    display: flex;
    gap: 0.5rem;
    align-items: center;
    flex-shrink: 0;
    /* Align with the select element: skip past the label height */
    margin-top: 1.25rem;
  }

  .model-actions .btn {
    display: inline-flex;
    align-items: center;
    gap: 0.375rem;
    padding: 0.5rem 0.875rem;
    border-radius: 6px;
    font-size: 0.8125rem;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.15s;
    border: none;
    white-space: nowrap;
  }

  .model-actions .btn-primary {
    background: var(--primary-color, #3b82f6);
    color: white;
  }

  .model-actions .btn-primary:hover:not(:disabled) {
    background: var(--primary-hover, #2563eb);
  }

  .model-actions .btn-warning {
    background: var(--warning-color, #f59e0b);
    color: white;
  }

  .model-actions .btn-warning:hover:not(:disabled) {
    background: #d97706;
  }

  .model-actions .btn:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .downloaded-count {
    margin-top: 0.625rem;
    font-size: 0.725rem;
    color: var(--text-muted);
  }

  @media (max-width: 768px) {
    .status-bar {
      flex-wrap: wrap;
    }

    .config-card {
      flex-direction: column;
      align-items: flex-start;
    }

    .config-actions {
      width: 100%;
      justify-content: flex-start;
    }

    .model-control-row {
      flex-direction: column;
    }

    .model-actions {
      margin-top: 0;
      width: 100%;
    }

    .model-actions .btn {
      flex: 1;
      min-height: 44px;
    }

    .model-info-row {
      flex-direction: column;
    }

    .section-header {
      flex-wrap: wrap;
      gap: 0.5rem;
    }

    .config-actions .btn-icon,
    .config-actions .btn-activate {
      min-height: 44px;
    }
  }
</style>
