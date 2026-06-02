<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { lockScroll, unlockScroll } from '$lib/scrollLock';
  import { LLMSettingsApi, type UserLLMSettings, type ProviderDefaults, type ConnectionTestResponse, type UserLLMConfigurationsList } from '../../lib/api/llmSettings';
  import ConfirmationModal from '../ConfirmationModal.svelte';
  import LLMConfigModal from './LLMConfigModal.svelte';
  import { toastStore } from '../../stores/toast';
  import { llmStatusStore } from '../../stores/llmStatus';
  import { t } from '$stores/locale';
  import axiosInstance from '$lib/axios';

  export let onSettingsChange: (() => void) | null = null;
  export let isAdmin: boolean = false;

  // State variables
  let loading = false;
  let saving = false;
  let testing = false;

  let currentSettings: UserLLMSettings | null = null;
  let supportedProviders: ProviderDefaults[] = [];
  let hasSettings = false;
  let savedConfigurations: UserLLMSettings[] = [];
  let sharedConfigurations: UserLLMSettings[] = [];
  let activeConfigurationId: string | null = null;
  let editingConfiguration: UserLLMSettings | null = null;

  // Modal state
  let showConfigModal = false;

  // Confirmation modals
  let showDeleteConfigModal = false;
  let configToDelete: UserLLMSettings | null = null;
  let showDeleteAllModal = false;

  // Auto-summary toggle state
  let autoSummaryEnabled = true;
  let autoSummaryLoading = false;
  let systemSummaryEnabled = true;
  let systemSummaryLoading = false;

  // Connection status for active configuration
  let connectionStatus: 'unknown' | 'connected' | 'disconnected' = 'unknown';
  let statusMessage = '';
  let statusLastChecked: Date | null = null;
  let checkingStatus = false;

  // Load initial data — parallelize independent calls
  onMount(async () => {
    const loads: Promise<void>[] = [loadData(), loadAutoSummarySetting()];
    if (isAdmin) {
      loads.push(loadSystemSummarySetting());
    }
    await Promise.allSettled(loads);
  });

  onDestroy(() => {
    if (_llmModalWasOpen) unlockScroll();
  });

  async function loadData() {
    loading = true;

    try {
      // Fetch providers and configurations in parallel
      const [providersResponse, configurationsResponse] = await Promise.all([
        LLMSettingsApi.getSupportedProviders(),
        LLMSettingsApi.getUserConfigurations().catch((err: any) => {
          if (err.response?.status === 404 || err.response?.status === 403) {
            return null; // No configs — not an error
          }
          throw err;
        }),
      ]);

      supportedProviders = providersResponse.providers;

      if (configurationsResponse) {
        savedConfigurations = configurationsResponse.configurations;
        sharedConfigurations = configurationsResponse.shared_configurations || [];
        activeConfigurationId = configurationsResponse.active_configuration_id || null;

        if (activeConfigurationId && (savedConfigurations.length > 0 || sharedConfigurations.length > 0)) {
          currentSettings = savedConfigurations.find(c => c.uuid === activeConfigurationId)
            || sharedConfigurations.find(c => c.uuid === activeConfigurationId) || null;
          hasSettings = true;

          // Refresh global store from cached backend status (no external API call)
          llmStatusStore.refreshStatus();

          // Use last test result from configurations data instead of re-testing
          if (currentSettings?.test_status === 'success') {
            connectionStatus = 'connected';
            statusMessage = currentSettings.test_message || '';
            statusLastChecked = currentSettings.last_tested ? new Date(currentSettings.last_tested) : null;
          } else if (currentSettings?.test_status === 'failed') {
            connectionStatus = 'disconnected';
            statusMessage = currentSettings.test_message || '';
            statusLastChecked = currentSettings.last_tested ? new Date(currentSettings.last_tested) : null;
          } else {
            connectionStatus = 'unknown';
          }
        } else {
          currentSettings = null;
          hasSettings = false;
          llmStatusStore.reset();
        }
      } else {
        currentSettings = null;
        hasSettings = false;
        llmStatusStore.reset();
      }
    } catch (err: any) {
      console.error('Error loading LLM providers:', err);
      const detail = err.response?.data?.detail;
      const detailStr = typeof detail === 'string' ? detail : '';
      if (!err.message?.includes('LLM') && !detailStr.includes('configuration')) {
        const errorMsg = detailStr || $t('settings.llmProvider.loadProvidersError');
        toastStore.error(errorMsg, 5000);
      }
    } finally {
      loading = false;
    }
  }

  async function loadAutoSummarySetting() {
    try {
      const res = await axiosInstance.get('/settings/ai-summary');
      autoSummaryEnabled = res.data.ai_summary_enabled;
    } catch (err) {
      console.warn('Failed to load auto-summary setting:', err);
    }
  }

  async function saveAutoSummary() {
    autoSummaryLoading = true;
    try {
      const res = await axiosInstance.put('/settings/ai-summary', { enabled: autoSummaryEnabled });
      autoSummaryEnabled = res.data.ai_summary_enabled;
      toastStore.success(res.data.message, 3000);
    } catch (err: any) {
      const detail = err.response?.data?.detail;
      toastStore.error(typeof detail === 'string' ? detail : $t('common.error'), 5000);
      autoSummaryEnabled = !autoSummaryEnabled; // rollback
    } finally {
      autoSummaryLoading = false;
    }
  }

  async function loadSystemSummarySetting() {
    try {
      const res = await axiosInstance.get('/admin/system/ai-summary');
      systemSummaryEnabled = res.data.ai_summary_enabled;
    } catch (err) {
      console.warn('Failed to load system summary setting:', err);
    }
  }

  async function saveSystemSummary() {
    systemSummaryLoading = true;
    try {
      const res = await axiosInstance.put('/admin/system/ai-summary', { enabled: systemSummaryEnabled });
      systemSummaryEnabled = res.data.ai_summary_enabled;
      const state = systemSummaryEnabled ? $t('common.enabled') : $t('common.disabled');
      toastStore.success(`${$t('settings.llm.systemSummary')}: ${state}`, 3000);
    } catch (err: any) {
      const detail = err.response?.data?.detail;
      toastStore.error(typeof detail === 'string' ? detail : $t('common.error'), 5000);
      systemSummaryEnabled = !systemSummaryEnabled; // rollback
    } finally {
      systemSummaryLoading = false;
    }
  }

  async function checkCurrentStatus() {
    if (!hasSettings || !activeConfigurationId) {
      connectionStatus = 'unknown';
      statusMessage = '';
      return;
    }

    checkingStatus = true;

    try {
      const result = await LLMSettingsApi.testCurrentSettings();
      connectionStatus = result.success ? 'connected' : 'disconnected';
      statusMessage = result.message;
      statusLastChecked = new Date();
    } catch (err: any) {
      connectionStatus = 'disconnected';
      const detail = err.response?.data?.detail;
      statusMessage = typeof detail === 'string' ? detail : $t('settings.llmProvider.testFailed');
      statusLastChecked = new Date();
    } finally {
      checkingStatus = false;
      // Refresh global store once after test completes
      llmStatusStore.refreshStatus();
    }
  }

  // Modal management functions
  function openCreateModal() {
    showConfigModal = true;
    editingConfiguration = null;
  }

  function openEditModal(config: UserLLMSettings) {
    showConfigModal = true;
    editingConfiguration = config;
  }

  function editConfiguration(config: UserLLMSettings) {
    openEditModal(config);
  }

  async function activateConfiguration(configId: string) {
    if (configId === activeConfigurationId) {
      toastStore.success($t('settings.llmProvider.configAlreadyActive'), 3000);
      return;
    }

    saving = true;

    try {
      await LLMSettingsApi.setActiveConfiguration(configId);

      // Update local state
      activeConfigurationId = configId;
      currentSettings = savedConfigurations.find(c => c.uuid === configId) || null;
      hasSettings = true;

      // Check status of newly activated configuration
      await checkCurrentStatus();

      toastStore.success($t('settings.llmProvider.configActivated'), 5000);

      // Trigger parent component update
      if (onSettingsChange) {
        onSettingsChange();
      }
    } catch (err: any) {
      const detail = err.response?.data?.detail;
      const errorMsg = typeof detail === 'string' ? detail : $t('settings.llmProvider.activateFailed');
      toastStore.error(errorMsg, 5000);
    } finally {
      saving = false;
    }
  }

  async function testSavedConfiguration(config: UserLLMSettings): Promise<void> {
    testing = true;

    try {
      const result = await LLMSettingsApi.testConfiguration(config.uuid);

      if (result.success) {
        toastStore.success(`${config.name}: ${result.message}`, 5000);
      } else {
        toastStore.error(`${config.name}: ${result.message}`, 8000);
      }
    } catch (err: any) {
      const detail = err.response?.data?.detail;
      const errorMsg = typeof detail === 'string' ? detail : $t('settings.llmProvider.testFailed');
      toastStore.error(`${config.name}: ${errorMsg}`, 8000);
    } finally {
      testing = false;
    }
  }

  function confirmDeleteConfiguration(config: UserLLMSettings) {
    configToDelete = config;
    showDeleteConfigModal = true;
  }

  async function deleteConfiguration() {
    if (!configToDelete) return;
    const config = configToDelete;

    saving = true;

    try {
      await LLMSettingsApi.deleteConfiguration(config.uuid);

      // Remove from saved configurations
      savedConfigurations = savedConfigurations.filter(c => c.uuid !== config.uuid);

      // If this was the active configuration, clear it
      if (config.uuid === activeConfigurationId) {
        activeConfigurationId = null;
        currentSettings = null;
        hasSettings = false;

        // Reset global LLM status store
        llmStatusStore.reset();
      }

      toastStore.success($t('settings.llmProvider.configDeleted', { name: config.name }), 5000);

      configToDelete = null;
      showDeleteConfigModal = false;

      // Trigger parent component update
      if (onSettingsChange) {
        onSettingsChange();
      }
    } catch (err: any) {
      const detail = err.response?.data?.detail;
      const errorMsg = typeof detail === 'string' ? detail : $t('settings.llmProvider.deleteFailed');
      toastStore.error(errorMsg, 5000);
    } finally {
      saving = false;
    }
  }

  function confirmDeleteAll() {
    showDeleteAllModal = true;
  }

  async function deleteAllSettings() {
    if (!hasSettings) {
      toastStore.error($t('settings.llmProvider.noSettingsToDelete'));
      return;
    }

    saving = true;

    try {
      await LLMSettingsApi.deleteSettings();

      // Reset all local state
      savedConfigurations = [];
      activeConfigurationId = null;
      currentSettings = null;
      hasSettings = false;
      connectionStatus = 'unknown';
      statusMessage = '';
      statusLastChecked = null;

      // Reset global LLM status store
      llmStatusStore.reset();

      toastStore.success($t('settings.llmProvider.allDeleted'), 5000);

      showDeleteAllModal = false;

      // Trigger parent component update
      if (onSettingsChange) {
        onSettingsChange();
      }
    } catch (err: any) {
      const detail = err.response?.data?.detail;
      const errorMsg = typeof detail === 'string' ? detail : $t('settings.llmProvider.deleteAllFailed');
      toastStore.error(errorMsg, 5000);
    } finally {
      saving = false;
    }
  }

  function handleConfigSaved(event: CustomEvent<UserLLMSettings>) {
    const savedConfig = event.detail;

    // Update the configurations list
    if (editingConfiguration) {
      // Update existing configuration
      const index = savedConfigurations.findIndex(c => c.uuid === savedConfig.uuid);
      if (index !== -1) {
        savedConfigurations[index] = savedConfig;
        savedConfigurations = [...savedConfigurations];
      }
    } else {
      // Add new configuration
      savedConfigurations = [...savedConfigurations, savedConfig];
    }

    // Update hasSettings flag
    hasSettings = savedConfigurations.length > 0 || sharedConfigurations.length > 0;

    // Trigger parent component update
    if (onSettingsChange) {
      onSettingsChange();
    }
  }

  async function handleShareToggle(config: UserLLMSettings) {
    const newShared = !config.is_shared;
    const idx = savedConfigurations.findIndex(c => c.uuid === config.uuid);
    const prevSharedAt = config.shared_at;

    // Optimistic update — flip toggle immediately for responsive UI
    if (idx !== -1) {
      savedConfigurations[idx] = {
        ...savedConfigurations[idx],
        is_shared: newShared,
        shared_at: newShared ? new Date().toISOString() : undefined
      };
      savedConfigurations = savedConfigurations;
    }

    saving = true;
    try {
      await LLMSettingsApi.toggleShare(config.uuid, newShared);
      toastStore.success(
        newShared ? $t('settings.llmProvider.shareEnabled') : $t('settings.llmProvider.shareDisabled'),
        3000
      );
    } catch (err: any) {
      // Rollback on failure
      if (idx !== -1) {
        savedConfigurations[idx] = {
          ...savedConfigurations[idx],
          is_shared: !newShared,
          shared_at: prevSharedAt
        };
        savedConfigurations = savedConfigurations;
      }
      const detail = err.response?.data?.detail;
      const errorMsg = typeof detail === 'string' ? detail : $t('settings.llmProvider.shareFailed');
      toastStore.error(errorMsg, 5000);
    } finally {
      saving = false;
    }
  }

  function getProviderDisplayName(provider: string): string {
    const displayNames: Record<string, string> = {
      openai: $t('llm.provider.openai'),
      vllm: $t('llm.provider.vllm'),
      ollama: $t('llm.provider.ollama'),
      claude: $t('llm.provider.claude'),
      anthropic: $t('llm.provider.anthropic'),
      openrouter: $t('llm.provider.openrouter'),
      custom: $t('llm.provider.custom')
    };
    return displayNames[provider] || provider;
  }

  let _llmModalWasOpen = false;
  $: {
    const _anyOpen = showConfigModal || showDeleteConfigModal || showDeleteAllModal;
    if (_anyOpen !== _llmModalWasOpen) {
      _anyOpen ? lockScroll() : unlockScroll();
      _llmModalWasOpen = _anyOpen;
    }
  }

  // Handle keyboard shortcuts for modals
  let keydownHandler: ((event: KeyboardEvent) => void) | null = null;

  $: {
    // Clean up previous listener if exists
    if (keydownHandler) {
      document.removeEventListener('keydown', keydownHandler);
      keydownHandler = null;
    }

    // Add new listener if modal is open
    if (showConfigModal) {
      keydownHandler = (event: KeyboardEvent) => {
        if (event.key === 'Escape') {
          showConfigModal = false;
        }
      };
      document.addEventListener('keydown', keydownHandler);
    }
  }
</script>

<div class="llm-settings">
  {#if loading}
    <div class="llm-skeleton">
      <div class="skeleton-header">
        <div class="skeleton-line title"></div>
        <div class="skeleton-chip"></div>
      </div>
      <div class="skeleton-card"></div>
      <div class="skeleton-card short"></div>
      <div class="skeleton-section">
        <div class="skeleton-line title"></div>
        <div class="skeleton-line full"></div>
        <div class="skeleton-line mid"></div>
      </div>
    </div>
  {:else}
    <!-- Saved Configurations -->
    <div class="saved-configs-section">
      <div class="section-header">
        <h4>
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"/>
          </svg>
          {$t('settings.llmProvider.savedConfigs')}
        </h4>
        {#if savedConfigurations.length > 0 || sharedConfigurations.length > 0}
          <button class="create-config-button" on:click={openCreateModal} title={$t('settings.llmProvider.createConfigTooltip')}>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <line x1="12" y1="5" x2="12" y2="19"/>
              <line x1="5" y1="12" x2="19" y2="12"/>
            </svg>
            {$t('settings.llmProvider.createConfig')}
          </button>
        {/if}
      </div>

      {#if savedConfigurations.length > 0}
        <div class="config-list">
          {#each savedConfigurations as config}
            <div class="config-item" class:active={config.uuid === activeConfigurationId}>
              <div class="config-info">
                <div class="config-name">
                  {config.name}
                  {#if config.is_shared}
                    <span class="share-badge">{$t('settings.llmProvider.shared')}</span>
                  {/if}
                </div>
                <div class="config-provider">{getProviderDisplayName(config.provider)} • {config.model_name}</div>
                {#if config.base_url}
                  <div class="config-url">{config.base_url}</div>
                {/if}
                <div class="share-toggle-row">
                  <label class="toggle-label">
                    <input type="checkbox" class="toggle-input" checked={config.is_shared}
                      on:change={() => handleShareToggle(config)} disabled={saving} />
                    <span class="toggle-switch"></span>
                    <span class="toggle-text">{$t('settings.llmProvider.shareGlobally')}</span>
                  </label>
                </div>
              </div>

              <div class="config-actions">
                {#if config.uuid === activeConfigurationId}
                  <div class="config-status currently-active" title={statusMessage}>
                    <div class="status-indicator">
                      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                        <polyline points="20,6 9,17 4,12"/>
                      </svg>
                    </div>
                    <span class="status-text">{$t('settings.llmProvider.currentlyActive')}</span>
                  </div>
                {:else}
                  <button
                    class="activate-button"
                    on:click={() => activateConfiguration(config.uuid)}
                    disabled={saving}
                    title={$t('settings.llmProvider.activateTooltip')}
                  >
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                      <circle cx="12" cy="12" r="3"/>
                      <circle cx="12" cy="1" r="1"/>
                      <circle cx="12" cy="23" r="1"/>
                      <circle cx="4.22" cy="4.22" r="1"/>
                      <circle cx="19.78" cy="19.78" r="1"/>
                      <circle cx="1" cy="12" r="1"/>
                      <circle cx="23" cy="12" r="1"/>
                      <circle cx="4.22" cy="19.78" r="1"/>
                      <circle cx="19.78" cy="4.22" r="1"/>
                    </svg>
                    {$t('settings.llmProvider.activate')}
                  </button>
                {/if}

                <button
                  class="test-connection-button"
                  on:click={() => testSavedConfiguration(config)}
                  disabled={testing}
                  title={$t('settings.llmProvider.testConnection') + ' ' + config.name}
                >
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <polyline points="1 4 1 10 7 10"/>
                    <polyline points="23 20 23 14 17 14"/>
                    <path d="m20.49 9A9 9 0 0 0 5.64 5.64L1 10m22 4l-4.64 4.36A9 9 0 0 1 3.51 15"/>
                  </svg>
                </button>

                <button
                  class="edit-button"
                  on:click={() => editConfiguration(config)}
                  disabled={saving}
                  title={$t('settings.llmProvider.editConfig')}
                >
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>
                    <path d="m18.5 2.5 a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>
                  </svg>
                </button>

                <button
                  class="delete-config-button"
                  on:click={() => confirmDeleteConfiguration(config)}
                  disabled={saving}
                  title={$t('settings.llmProvider.deleteConfig') + ' ' + config.name}
                >
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <polyline points="3,6 5,6 21,6"/>
                    <path d="m19,6v14a2,2 0 0,1 -2,2H7a2,2 0 0,1 -2,-2V6m3,0V4a2,2 0 0,1 2,-2h4a2,2 0 0,1 2,2v2"/>
                    <line x1="10" y1="11" x2="10" y2="17"/>
                    <line x1="14" y1="11" x2="14" y2="17"/>
                  </svg>
                </button>
              </div>
            </div>
          {/each}
        </div>

        <!-- Delete All Button -->
        <div class="delete-all-section">
          <button
            class="delete-all-button"
            on:click={confirmDeleteAll}
            disabled={saving}
            title={$t('settings.llmProvider.deleteAllTooltip')}
          >
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <polyline points="3,6 5,6 21,6"/>
              <path d="m19,6v14a2,2 0 0,1 -2,2H7a2,2 0 0,1 -2,-2V6m3,0V4a2,2 0 0,1 2,-2h4a2,2 0 0,1 2,2v2"/>
              <line x1="10" y1="11" x2="10" y2="17"/>
              <line x1="14" y1="11" x2="14" y2="17"/>
            </svg>
            {$t('settings.llmProvider.deleteAllConfigs')}
          </button>
        </div>
      {/if}

      <!-- Shared by Others -->
      {#if sharedConfigurations.length > 0}
        <div class="section-header shared-section-header">
          <h4>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <circle cx="12" cy="12" r="10"/>
              <line x1="2" y1="12" x2="22" y2="12"/>
              <path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/>
            </svg>
            {$t('settings.llmProvider.sharedByOthers')}
          </h4>
        </div>
        <div class="config-list">
          {#each sharedConfigurations as config}
            <div class="config-item shared" class:active={config.uuid === activeConfigurationId}>
              <div class="config-info">
                <div class="config-name">
                  {config.name}
                  {#if config.owner_role === 'admin' || config.owner_role === 'super_admin'}
                    <span class="admin-badge">{$t('settings.sharing.adminBadge')}</span>
                  {/if}
                </div>
                <div class="config-provider">{getProviderDisplayName(config.provider)} • {config.model_name}</div>
                {#if config.owner_name}
                  <div class="shared-by">{$t('settings.llmProvider.sharedBy', { name: config.owner_name })}</div>
                {/if}
              </div>
              <div class="config-actions">
                {#if config.uuid === activeConfigurationId}
                  <div class="config-status currently-active">
                    <div class="status-indicator">
                      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                        <polyline points="20,6 9,17 4,12"/>
                      </svg>
                    </div>
                    <span class="status-text">{$t('settings.llmProvider.currentlyActive')}</span>
                  </div>
                {:else}
                  <button
                    class="activate-button"
                    on:click={() => activateConfiguration(config.uuid)}
                    disabled={saving}
                    title={$t('settings.llmProvider.activateTooltip')}
                  >
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                      <circle cx="12" cy="12" r="3"/>
                    </svg>
                    {$t('settings.llmProvider.activate')}
                  </button>
                {/if}
                <button
                  class="test-connection-button"
                  on:click={() => testSavedConfiguration(config)}
                  disabled={testing}
                  title={$t('settings.llmProvider.testConnection') + ' ' + config.name}
                >
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <polyline points="1 4 1 10 7 10"/>
                    <polyline points="23 20 23 14 17 14"/>
                    <path d="m20.49 9A9 9 0 0 0 5.64 5.64L1 10m22 4l-4.64 4.36A9 9 0 0 1 3.51 15"/>
                  </svg>
                </button>
              </div>
            </div>
          {/each}
        </div>
      {/if}

      {#if savedConfigurations.length === 0 && sharedConfigurations.length === 0}
        <div class="empty-state">
          <div class="empty-icon">
            <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M14.828 14.828a4 4 0 0 1-5.656 0M9 10h.01M15 10h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 0 1-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z"/>
            </svg>
          </div>
          <h4>{$t('settings.llmProvider.noConfigs')}</h4>
          <p>{$t('settings.llmProvider.noConfigsDesc')}</p>
          <button class="create-first-config-btn" on:click={openCreateModal}>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <line x1="12" y1="5" x2="12" y2="19"/>
              <line x1="5" y1="12" x2="19" y2="12"/>
            </svg>
            {$t('settings.llmProvider.createFirstConfig')}
          </button>
        </div>
      {/if}
    </div>

    <!-- Auto-Summary Settings -->
    {#if hasSettings || sharedConfigurations.length > 0}
      <div class="auto-summary-section">
        <div class="section-header">
          <h4>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
              <polyline points="14 2 14 8 20 8"/>
              <line x1="16" y1="13" x2="8" y2="13"/>
              <line x1="16" y1="17" x2="8" y2="17"/>
            </svg>
            {$t('settings.llm.autoSummarySection')}
          </h4>
        </div>

        <div class="summary-toggle-row">
          <div class="toggle-info">
            <span class="toggle-title">{$t('settings.llm.autoSummary')}</span>
            <span class="toggle-desc">{$t('settings.llm.autoSummaryDesc')}</span>
          </div>
          <label class="toggle-label">
            <input type="checkbox" class="toggle-input"
              bind:checked={autoSummaryEnabled}
              on:change={saveAutoSummary}
              disabled={autoSummaryLoading || (!systemSummaryEnabled && !isAdmin)} />
            <span class="toggle-switch"></span>
          </label>
        </div>

        {#if !autoSummaryEnabled}
          <div class="summary-hint">
            {$t('settings.llm.autoSummaryDisabledHint')}
          </div>
        {/if}

        {#if !systemSummaryEnabled && !isAdmin}
          <div class="summary-hint warning">
            {$t('settings.llm.systemSummaryDisabledHint')}
          </div>
        {/if}

        {#if isAdmin}
          <div class="summary-toggle-row admin-toggle">
            <div class="toggle-info">
              <span class="toggle-title">
                {$t('settings.llm.systemSummary')}
                <span class="admin-badge">{$t('settings.sharing.adminBadge')}</span>
              </span>
              <span class="toggle-desc">{$t('settings.llm.systemSummaryDesc')}</span>
            </div>
            <label class="toggle-label">
              <input type="checkbox" class="toggle-input"
                bind:checked={systemSummaryEnabled}
                on:change={saveSystemSummary}
                disabled={systemSummaryLoading} />
              <span class="toggle-switch"></span>
            </label>
          </div>

          {#if !systemSummaryEnabled}
            <div class="summary-hint warning">
              {$t('settings.llm.systemSummaryDisabledWarning')}
            </div>
          {/if}
        {/if}
      </div>
    {/if}
  {/if}
</div>

<!-- LLM Configuration Modal -->
<LLMConfigModal
  bind:show={showConfigModal}
  editingConfig={editingConfiguration}
  {supportedProviders}
  on:saved={handleConfigSaved}
  on:close={() => {
    showConfigModal = false;
    editingConfiguration = null;
  }}
/>

<!-- Confirmation Modals -->
<ConfirmationModal
  bind:isOpen={showDeleteConfigModal}
  title={$t('settings.llmProvider.deleteConfigConfirmTitle')}
  message={configToDelete ? $t('settings.llmProvider.deleteConfigConfirmMessage', { name: configToDelete.name }) : ''}
  confirmText={$t('common.delete')}
  cancelText={$t('common.cancel')}
  confirmButtonClass="modal-delete-button"
  cancelButtonClass="modal-cancel-button"
  on:confirm={deleteConfiguration}
  on:cancel={() => { configToDelete = null; showDeleteConfigModal = false; }}
  on:close={() => { configToDelete = null; showDeleteConfigModal = false; }}
/>

<ConfirmationModal
  bind:isOpen={showDeleteAllModal}
  title={$t('settings.llmProvider.deleteAllConfirmTitle')}
  message={$t('settings.llmProvider.deleteAllConfirmMessage')}
  confirmText={$t('settings.llmProvider.deleteAll')}
  cancelText={$t('common.cancel')}
  confirmButtonClass="modal-delete-button"
  cancelButtonClass="modal-cancel-button"
  on:confirm={deleteAllSettings}
  on:cancel={() => showDeleteAllModal = false}
  on:close={() => showDeleteAllModal = false}
/>

<style>
  .llm-settings {
    max-width: 800px;
    margin: 0 auto;
  }

  .llm-skeleton {
    display: flex;
    flex-direction: column;
    gap: 1rem;
    padding: 0.5rem 0;
  }

  .skeleton-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
  }

  .skeleton-chip {
    width: 80px;
    height: 28px;
    border-radius: 6px;
    background: var(--border-color);
    animation: skeleton-pulse 1.5s ease-in-out infinite;
  }

  .skeleton-card {
    height: 72px;
    border-radius: 8px;
    background: var(--border-color);
    animation: skeleton-pulse 1.5s ease-in-out infinite;
  }

  .skeleton-card.short {
    height: 48px;
  }

  .skeleton-section {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
    margin-top: 0.5rem;
  }

  .skeleton-line {
    height: 12px;
    border-radius: 4px;
    background: var(--border-color);
    animation: skeleton-pulse 1.5s ease-in-out infinite;
  }

  .skeleton-line.title {
    width: 40%;
    height: 16px;
  }

  .skeleton-line.full {
    width: 100%;
  }

  .skeleton-line.mid {
    width: 65%;
  }

  .saved-configs-section {
    margin-bottom: 2rem;
  }

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

  .create-config-button {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    background: #3b82f6;
    color: white;
    border: none;
    padding: 0.6rem 1.2rem;
    border-radius: 10px;
    cursor: pointer;
    font-size: 0.8125rem;
    font-weight: 500;
    transition: all 0.2s ease;
    box-shadow: 0 2px 4px rgba(var(--primary-color-rgb), 0.2);
  }

  .create-config-button:hover {
    background: #2563eb;
    transform: scale(1.02);
    box-shadow: 0 4px 8px rgba(var(--primary-color-rgb), 0.25);
  }

  .create-config-button:active {
    transform: scale(1);
  }

  .config-list {
    display: flex;
    flex-direction: column;
    gap: 1rem;
  }

  .config-item {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 1rem;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    background: var(--card-background);
    transition: all 0.2s ease;
  }

  .config-item.active {
    border-color: var(--primary-color);
    background: var(--primary-bg);
  }

  .config-item:hover:not(.active) {
    border-color: var(--border-hover);
    background: var(--hover-color);
  }

  .config-info {
    flex: 1;
    min-width: 0;
  }

  .config-name {
    font-weight: 500;
    color: var(--text-color);
    font-size: 0.8125rem;
    margin-bottom: 0.25rem;
  }

  .config-provider {
    font-size: 0.8rem;
    color: var(--text-muted);
    margin-bottom: 0.25rem;
  }

  .config-url {
    font-size: 0.75rem;
    color: var(--text-muted);
    font-family: var(--font-mono);
    opacity: 0.7;
  }

  .config-actions {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex-shrink: 0;
  }

  .config-status {
    display: inline-flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.375rem 0.75rem;
    border-radius: 4px;
    font-size: 0.8rem;
    font-weight: 500;
    width: fit-content;
  }

  .config-status.currently-active {
    background-color: var(--success-bg);
    color: var(--success-color);
    border: 1px solid var(--success-border);
  }

  .status-indicator {
    display: flex;
    align-items: center;
    justify-content: center;
  }

  .activate-button, .edit-button, .delete-config-button, .test-connection-button {
    display: flex;
    align-items: center;
    gap: 0.25rem;
    padding: 0.5rem 0.75rem;
    border-radius: 4px;
    font-size: 0.8rem;
    cursor: pointer;
    transition: all 0.2s ease;
    border: 1px solid;
    height: 32px;
    box-sizing: border-box;
  }

  .activate-button {
    background-color: var(--success-color);
    border-color: var(--success-color);
    color: white;
    padding: 0.5rem 0.5rem;
  }

  .activate-button:hover:not(:disabled) {
    background-color: #059669;
    border-color: #059669;
  }

  .test-connection-button {
    background-color: transparent;
    border-color: var(--primary-color);
    color: var(--primary-color);
  }

  .test-connection-button:hover:not(:disabled) {
    background-color: #3b82f6;
    border-color: var(--primary-color);
    color: white;
  }

  .edit-button {
    background-color: transparent;
    border-color: var(--border-color);
    color: var(--text-color);
  }

  .edit-button:hover:not(:disabled) {
    background-color: #3b82f6;
    border-color: var(--primary-color);
    color: white;
  }

  .delete-config-button {
    background-color: transparent;
    border-color: var(--error-color);
    color: var(--error-color);
  }

  .delete-config-button:hover:not(:disabled) {
    background-color: var(--error-color);
    border-color: var(--error-color);
    color: white;
    transform: scale(1.02);
    box-shadow: 0 4px 8px rgba(239, 68, 68, 0.25);
  }

  .delete-config-button:active:not(:disabled) {
    transform: scale(1);
    box-shadow: 0 2px 4px rgba(239, 68, 68, 0.2);
  }

  .delete-all-section {
    margin-top: 2rem;
    padding-top: 1rem;
    border-top: 1px solid var(--border-color);
    text-align: center;
  }

  .delete-all-button {
    display: inline-flex;
    align-items: center;
    gap: 0.5rem;
    background: #ef4444;
    color: white;
    border: none;
    padding: 0.6rem 1.2rem;
    border-radius: 10px;
    cursor: pointer;
    font-size: 0.8125rem;
    font-weight: 500;
    transition: all 0.2s ease;
    box-shadow: 0 2px 4px rgba(239, 68, 68, 0.2);
  }

  .delete-all-button:hover:not(:disabled) {
    background: #dc2626;
    transform: scale(1.02);
    box-shadow: 0 4px 8px rgba(239, 68, 68, 0.25);
  }

  .delete-all-button:active:not(:disabled) {
    transform: scale(1);
    box-shadow: 0 2px 4px rgba(239, 68, 68, 0.2);
  }

  .delete-all-button:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .empty-state {
    text-align: center;
    padding: 3rem 2rem;
    border: 2px dashed var(--border-color);
    border-radius: 8px;
    background: var(--card-background);
  }

  .empty-icon {
    margin-bottom: 1rem;
    color: var(--text-muted);
    opacity: 0.6;
  }

  .empty-state h4 {
    margin: 0 0 0.5rem;
    color: var(--text-color);
    font-size: 1.125rem;
    font-weight: 500;
  }

  .empty-state p {
    margin: 0 0 1.5rem;
    color: var(--text-muted);
    font-size: 0.8125rem;
    line-height: 1.5;
  }

  .create-first-config-btn {
    display: inline-flex;
    align-items: center;
    gap: 0.5rem;
    background: #3b82f6;
    color: white;
    border: none;
    padding: 0.6rem 1.2rem;
    border-radius: 10px;
    cursor: pointer;
    font-size: 0.8125rem;
    font-weight: 500;
    transition: all 0.2s ease;
    box-shadow: 0 2px 4px rgba(var(--primary-color-rgb), 0.2);
  }

  .create-first-config-btn:hover {
    background: #2563eb;
    transform: scale(1.02);
    box-shadow: 0 4px 8px rgba(var(--primary-color-rgb), 0.25);
  }

  .create-first-config-btn:active {
    transform: scale(1);
  }

  button:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  /* Shared config styling */
  .config-item.shared {
    border-left: 3px solid var(--primary-color);
    background: rgba(var(--primary-color-rgb), 0.04);
  }

  .shared-section-header {
    margin-top: 1.5rem;
  }

  .share-badge {
    display: inline-flex;
    align-items: center;
    padding: 1px 6px;
    border-radius: 10px;
    font-size: 0.625rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    background: rgba(59, 130, 246, 0.12);
    color: var(--primary-color);
    margin-left: 0.5rem;
    vertical-align: middle;
  }

  :global([data-theme='dark']) .share-badge {
    background: rgba(var(--primary-color-rgb), 0.2);
    color: #60a5fa;
  }

  .admin-badge {
    display: inline-flex;
    align-items: center;
    padding: 1px 6px;
    border-radius: 10px;
    font-size: 0.625rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    background: rgba(245, 158, 11, 0.12);
    color: #d97706;
    margin-left: 0.5rem;
    vertical-align: middle;
  }

  :global([data-theme='dark']) .admin-badge {
    background: rgba(245, 158, 11, 0.2);
    color: #fbbf24;
  }

  .shared-by {
    font-size: 0.75rem;
    color: var(--text-muted);
    font-style: italic;
    margin-top: 0.125rem;
  }

  .share-toggle-row {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    margin-top: 0.5rem;
    padding-top: 0.5rem;
    border-top: 1px dashed var(--border-color);
  }

  .toggle-label {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    cursor: pointer;
    font-size: 0.75rem;
    color: var(--text-muted);
  }

  .toggle-input {
    display: none;
  }

  .toggle-switch {
    position: relative;
    width: 28px;
    height: 16px;
    background: var(--border-color);
    border-radius: 8px;
    transition: background 0.2s;
    flex-shrink: 0;
  }

  .toggle-switch::after {
    content: '';
    position: absolute;
    top: 2px;
    left: 2px;
    width: 12px;
    height: 12px;
    background: white;
    border-radius: 50%;
    transition: transform 0.2s;
  }

  .toggle-input:checked + .toggle-switch {
    background: #3b82f6;
  }

  .toggle-input:checked + .toggle-switch::after {
    transform: translateX(12px);
  }

  .toggle-text {
    user-select: none;
  }

  /* Auto-summary settings */
  .auto-summary-section {
    margin-top: 2rem;
    padding-top: 1rem;
  }

  .summary-toggle-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 1rem;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    background: var(--card-background);
    margin-bottom: 0.5rem;
  }

  .summary-toggle-row.admin-toggle {
    border-color: var(--warning-color);
    background: rgba(245, 158, 11, 0.04);
    margin-top: 1rem;
  }

  :global([data-theme='dark']) .summary-toggle-row.admin-toggle {
    background: rgba(245, 158, 11, 0.08);
  }

  .toggle-info {
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
    flex: 1;
    min-width: 0;
  }

  .toggle-title {
    font-weight: 500;
    font-size: 0.8125rem;
    color: var(--text-color);
  }

  .toggle-desc {
    font-size: 0.75rem;
    color: var(--text-muted);
    line-height: 1.4;
  }

  .summary-hint {
    font-size: 0.75rem;
    color: var(--text-muted);
    padding: 0.5rem 1rem;
    background: var(--surface-color);
    border-radius: 4px;
    margin-bottom: 0.5rem;
    line-height: 1.4;
  }

  .summary-hint.warning {
    color: var(--warning-color);
    background: rgba(245, 158, 11, 0.08);
    border-left: 3px solid var(--warning-color);
  }

  @media (max-width: 768px) {
    .llm-settings {
      max-width: 100%;
    }

    .section-header {
      flex-wrap: wrap;
      gap: 0.5rem;
    }

    .section-header h4 {
      font-size: 1rem;
    }

    .create-config-button,
    .create-first-config-btn {
      width: 100%;
      justify-content: center;
      min-height: 44px;
    }

    .config-item {
      flex-direction: column;
      align-items: stretch;
      gap: 0.75rem;
    }

    .config-info {
      min-width: 0;
    }

    .config-name {
      word-break: break-word;
    }

    .config-url {
      word-break: break-all;
    }

    .config-actions {
      flex-wrap: wrap;
      gap: 0.375rem;
    }

    .activate-button,
    .edit-button,
    .delete-config-button,
    .test-connection-button {
      flex: 1;
      justify-content: center;
      min-height: 44px;
      height: auto;
    }

    .config-status {
      width: 100%;
      justify-content: center;
    }

    .delete-all-button {
      width: 100%;
      justify-content: center;
      min-height: 44px;
    }

    .empty-state {
      padding: 2rem 1rem;
    }

  }
</style>
