<script lang="ts">
  import Toggle from '$components/ui/Toggle.svelte';
  import ActivateIcon from '$components/ui/ActivateIcon.svelte';
  import SettingsSkeleton from '$components/ui/SettingsSkeleton.svelte';
  import { onMount, onDestroy } from 'svelte';
  import { lockScroll, unlockScroll } from '$lib/scrollLock';
  import { LLMSettingsApi, llmProviderDisplayName, type UserLLMSettings, type ProviderDefaults } from '$lib/api/llmSettings';
  import ConfirmationModal from '$components/ConfirmationModal.svelte';
  import { createConfirmDialog } from '$lib/confirmDialog';
  import LLMConfigModal from './LLMConfigModal.svelte';
  import { toastStore } from '$stores/toast';
  import { llmStatusStore } from '$stores/llmStatus';
  import { t } from '$stores/locale';
  import { getErrorMessage } from '$lib/utils/apiError';
  import {
    connectionFromStoredTest,
    isNoConfigurationsAnswer,
    loadFailureMessage,
    resolveActiveConfiguration
  } from '$lib/settings/llmSettingsView';
  import { runConnectionTest, runContextWindowProbe, runReasoningProbe } from '$lib/settings/llmProbes';
  import { withShareState } from '$lib/settings/shareState';
  import { UserSettingsApi } from '$lib/api/userSettings';
  import { AdminSettingsApi } from '$lib/api/adminSettings';
  import EmptyState from '$components/ui/EmptyState.svelte';

  export let onSettingsChange: (() => void) | null = null;
  export let isAdmin: boolean = false;

  // State variables
  let loading = false;
  let saving = false;
  let testing = false;
  /** uuid of the config whose reasoning probe is in flight, else null. */
  let probingReasoning: string | null = null;
  let probingWindow: string | null = null;

  let currentSettings: UserLLMSettings | null = null;
  let supportedProviders: ProviderDefaults[] = [];
  let hasSettings = false;
  let savedConfigurations: UserLLMSettings[] = [];
  let sharedConfigurations: UserLLMSettings[] = [];
  let activeConfigurationId: string | null = null;
  let editingConfiguration: UserLLMSettings | null = null;

  // Modal state
  let showConfigModal = false;

  // Confirmation dialog
  const confirmDialog = createConfirmDialog();
  const {
    isOpen: confirmOpen,
    title: confirmTitle,
    message: confirmMessage,
    confirmText: confirmLabel,
    cancelText: confirmCancelLabel,
    variant: confirmVariant,
  } = confirmDialog;

  // Auto-summary toggle state
  let autoSummaryEnabled = true;
  let autoSummaryLoading = false;
  let systemSummaryEnabled = true;
  let systemSummaryLoading = false;

  // Connection status for active configuration
  let statusMessage = '';

  // Load initial data — parallelize independent calls
  onMount(async () => {
    await Promise.allSettled([loadData(), loadAutoSummarySetting()]);
  });

  onDestroy(() => {
    if (_llmModalWasOpen) unlockScroll();
    if (keydownHandler) {
      document.removeEventListener('keydown', keydownHandler);
      keydownHandler = null;
    }
  });

  /**
   * Read the configuration list. `silent` re-reads it behind what is shown (after a save or
   * delete, whose server side effects the local list cannot know: the first configuration is
   * auto-activated, deleting the active one promotes another).
   */
  async function loadData({ silent = false }: { silent?: boolean } = {}) {
    if (!silent) loading = true;

    try {
      // Fetch providers and configurations in parallel
      const [providersResponse, configurationsResponse] = await Promise.all([
        LLMSettingsApi.getSupportedProviders(),
        LLMSettingsApi.getUserConfigurations().catch((err: unknown) => {
          if (isNoConfigurationsAnswer(err)) {
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
          currentSettings = resolveActiveConfiguration(
            savedConfigurations,
            sharedConfigurations,
            activeConfigurationId
          );
          hasSettings = true;

          // Use last test result from configurations data instead of re-testing
          const stored = connectionFromStoredTest(currentSettings);
          if (stored.status !== 'unknown') {
            statusMessage = stored.message;
          }
        } else {
          currentSettings = null;
          hasSettings = false;
        }
      } else {
        currentSettings = null;
        hasSettings = false;
      }

      // Ask the server rather than inferring availability from this list: with no
      // configuration of the user's own, the system provider may still answer.
      void llmStatusStore.invalidate();
    } catch (err: unknown) {
      console.error('Error loading LLM providers:', err);
      const errorMsg = loadFailureMessage(err, $t('settings.llmProvider.loadProvidersError'));
      if (errorMsg !== null) {
        toastStore.error(errorMsg, 5000);
      }
    } finally {
      loading = false;
    }
  }

  async function loadAutoSummarySetting() {
    try {
      const res = await UserSettingsApi.getAiSummaryPreference();
      autoSummaryEnabled = res.ai_summary_enabled;
      // Every user gets the deployment switch here; the admin route is for writing it.
      systemSummaryEnabled = res.system_enabled ?? true;
    } catch (err) {
      console.warn('Failed to load auto-summary setting:', err);
    }
  }

  async function saveAutoSummary() {
    autoSummaryLoading = true;
    try {
      const res = await UserSettingsApi.setAiSummaryPreference(autoSummaryEnabled);
      autoSummaryEnabled = res.ai_summary_enabled;
      toastStore.success(res.message ?? '', 3000);
    } catch (err: unknown) {
      toastStore.error(getErrorMessage(err, $t('common.error')), 5000);
      autoSummaryEnabled = !autoSummaryEnabled; // rollback
    } finally {
      autoSummaryLoading = false;
    }
  }

  async function saveSystemSummary() {
    systemSummaryLoading = true;
    try {
      const res = await AdminSettingsApi.setSystemAiSummary(systemSummaryEnabled);
      systemSummaryEnabled = res.ai_summary_enabled;
      const state = systemSummaryEnabled ? $t('common.enabled') : $t('common.disabled');
      toastStore.success(`${$t('settings.llm.systemSummary')}: ${state}`, 3000);
    } catch (err: unknown) {
      toastStore.error(getErrorMessage(err, $t('common.error')), 5000);
      systemSummaryEnabled = !systemSummaryEnabled; // rollback
    } finally {
      systemSummaryLoading = false;
    }
  }

  async function checkCurrentStatus() {
    if (!hasSettings || !activeConfigurationId) {
      statusMessage = '';
      return;
    }

    try {
      const result = await LLMSettingsApi.testCurrentSettings();
      statusMessage = result.message;
    } catch (err: unknown) {
      statusMessage = getErrorMessage(err, $t('settings.llmProvider.testFailed'));
    } finally {
      void llmStatusStore.invalidate();
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
    } catch (err: unknown) {
      const errorMsg = getErrorMessage(err, $t('settings.llmProvider.activateFailed'));
      toastStore.error(errorMsg, 5000);
    } finally {
      saving = false;
    }
  }

  async function testSavedConfiguration(config: UserLLMSettings): Promise<void> {
    testing = true;
    try {
      await runConnectionTest(config, $t);
    } finally {
      testing = false;
      void llmStatusStore.invalidate();
    }
  }

  // Both probes are explicitly user-invoked, never automatic (they cost real generations or
  // calls against the configured endpoint); `$lib/settings/llmProbes` says why.
  async function probeReasoning(config: UserLLMSettings): Promise<void> {
    probingReasoning = config.uuid;
    try {
      await runReasoningProbe(config, $t);
    } finally {
      probingReasoning = null;
    }
  }

  async function probeContextWindow(config: UserLLMSettings): Promise<void> {
    probingWindow = config.uuid;
    try {
      await runContextWindowProbe(config, $t);
    } finally {
      probingWindow = null;
    }
  }

  function confirmDeleteConfiguration(config: UserLLMSettings) {
    confirmDialog.request(
      $t('settings.llmProvider.deleteConfigConfirmTitle'),
      $t('settings.llmProvider.deleteConfigConfirmMessage', { name: config.name }),
      () => deleteConfiguration(config),
      { confirmText: $t('common.delete'), variant: 'danger' }
    );
  }

  async function deleteConfiguration(config: UserLLMSettings) {
    saving = true;

    try {
      await LLMSettingsApi.deleteConfiguration(config.uuid);

      // Remove from saved configurations
      savedConfigurations = savedConfigurations.filter(c => c.uuid !== config.uuid);

      // If this was the active configuration, clear it until the re-read below says which
      // configuration (if any) the server promoted in its place.
      if (config.uuid === activeConfigurationId) {
        activeConfigurationId = null;
        currentSettings = null;
        hasSettings = false;
      }
      void loadData({ silent: true });

      toastStore.success($t('settings.llmProvider.configDeleted', { name: config.name }), 5000);

      // Trigger parent component update
      if (onSettingsChange) {
        onSettingsChange();
      }
    } catch (err: unknown) {
      const errorMsg = getErrorMessage(err, $t('settings.llmProvider.deleteFailed'));
      toastStore.error(errorMsg, 5000);
    } finally {
      saving = false;
    }
  }

  function confirmDeleteAll() {
    confirmDialog.request(
      $t('settings.llmProvider.deleteAllConfirmTitle'),
      $t('settings.llmProvider.deleteAllConfirmMessage'),
      deleteAllSettings,
      { confirmText: $t('settings.llmProvider.deleteAll'), variant: 'danger' }
    );
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
      statusMessage = '';

      void llmStatusStore.invalidate();

      toastStore.success($t('settings.llmProvider.allDeleted'), 5000);

      // Trigger parent component update
      if (onSettingsChange) {
        onSettingsChange();
      }
    } catch (err: unknown) {
      const errorMsg = getErrorMessage(err, $t('settings.llmProvider.deleteAllFailed'));
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

    // The server auto-activates a first configuration; re-read so the active marker and the
    // LLM status store (and every AI button reading it) follow without a page reload.
    void loadData({ silent: true });

    // Trigger parent component update
    if (onSettingsChange) {
      onSettingsChange();
    }
  }

  async function handleShareToggle(config: UserLLMSettings) {
    const newShared = !config.is_shared;
    const prevSharedAt = config.shared_at;

    // Optimistic update — flip toggle immediately for responsive UI
    savedConfigurations = withShareState(
      savedConfigurations,
      config.uuid,
      newShared,
      newShared ? new Date().toISOString() : undefined
    );

    saving = true;
    try {
      await LLMSettingsApi.toggleShare(config.uuid, newShared);
      toastStore.success(
        newShared ? $t('settings.llmProvider.shareEnabled') : $t('settings.llmProvider.shareDisabled'),
        3000
      );
    } catch (err: unknown) {
      // Rollback on failure
      savedConfigurations = withShareState(savedConfigurations, config.uuid, !newShared, prevSharedAt);
      const errorMsg = getErrorMessage(err, $t('settings.llmProvider.shareFailed'));
      toastStore.error(errorMsg, 5000);
    } finally {
      saving = false;
    }
  }

  let _llmModalWasOpen = false;
  $: {
    const _anyOpen = showConfigModal;
    if (_anyOpen !== _llmModalWasOpen) {
      if (_anyOpen) lockScroll();
      else unlockScroll();
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
    <SettingsSkeleton variant="dashboard" label={$t('common.loading')} />
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
          <button class="btn btn-primary create-config-button" on:click={openCreateModal} title={$t('settings.llmProvider.createConfigTooltip')}>
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
          {#each savedConfigurations as config (config.uuid)}
            <div class="config-item" class:active={config.uuid === activeConfigurationId}>
              <div class="config-info">
                <div class="config-name">
                  {config.name}
                  {#if config.is_shared}
                    <span class="share-badge">{$t('settings.llmProvider.shared')}</span>
                  {/if}
                </div>
                <div class="config-provider">{llmProviderDisplayName(config.provider, $t)} • {config.model_name}</div>
                {#if config.base_url}
                  <div class="config-url">{config.base_url}</div>
                {/if}
                <div class="share-toggle-row">
                  <div class="toggle-label">
                    <Toggle id="share-config-{config.uuid}" checked={!!config.is_shared} onchange={() => handleShareToggle(config)} disabled={saving} />
                    <label class="toggle-text" for="share-config-{config.uuid}">{$t('settings.llmProvider.shareGlobally')}</label>
                  </div>
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
                    class="btn btn-success activate-button"
                    on:click={() => activateConfiguration(config.uuid)}
                    disabled={saving}
                    title={$t('settings.llmProvider.activateTooltip')}
                  >
                    <ActivateIcon strokeLinecap="round" />
                    {$t('settings.llmProvider.activate')}
                  </button>
                {/if}

                <button
                  class="btn btn-secondary test-connection-button"
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
                  class="btn btn-secondary test-connection-button"
                  on:click={() => probeReasoning(config)}
                  disabled={probingReasoning !== null}
                  title={$t('settings.llmProvider.probeReasoning')}
                  data-testid="llm-probe-reasoning"
                >
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M9 18h6"/>
                    <path d="M10 22h4"/>
                    <path d="M12 2a7 7 0 0 0-4 12.7V18h8v-3.3A7 7 0 0 0 12 2z"/>
                  </svg>
                </button>

                <button
                  class="btn btn-secondary test-connection-button"
                  on:click={() => probeContextWindow(config)}
                  disabled={probingWindow !== null}
                  title={$t('settings.llmProvider.probeContextWindow')}
                  data-testid="llm-probe-context-window"
                >
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <polyline points="15 3 21 3 21 9"/>
                    <polyline points="9 21 3 21 3 15"/>
                    <line x1="21" y1="3" x2="14" y2="10"/>
                    <line x1="3" y1="21" x2="10" y2="14"/>
                  </svg>
                </button>

                <button
                  class="btn btn-secondary edit-button"
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
                  class="btn btn-danger-outline delete-config-button"
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
            class="btn btn-danger delete-all-button"
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
          {#each sharedConfigurations as config (config.uuid)}
            <div class="config-item shared" class:active={config.uuid === activeConfigurationId}>
              <div class="config-info">
                <div class="config-name">
                  {config.name}
                  {#if config.owner_role === 'admin' || config.owner_role === 'super_admin'}
                    <span class="admin-badge">{$t('settings.sharing.adminBadge')}</span>
                  {/if}
                </div>
                <div class="config-provider">{llmProviderDisplayName(config.provider, $t)} • {config.model_name}</div>
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
                    class="btn btn-success activate-button"
                    on:click={() => activateConfiguration(config.uuid)}
                    disabled={saving}
                    title={$t('settings.llmProvider.activateTooltip')}
                  >
                    <ActivateIcon strokeLinecap="round" />
                    {$t('settings.llmProvider.activate')}
                  </button>
                {/if}
                <button
                  class="btn btn-secondary test-connection-button"
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
        <EmptyState bordered titleSize="xl" padding="var(--space-12) clamp(var(--space-4), 5vw, var(--space-8))" title={$t('settings.llmProvider.noConfigs')} description={$t('settings.llmProvider.noConfigsDesc')}>
          <svg slot="icon" width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M14.828 14.828a4 4 0 0 1-5.656 0M9 10h.01M15 10h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 0 1-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z"/>
          </svg>
          <button class="btn btn-primary create-first-config-btn" on:click={openCreateModal}>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <line x1="12" y1="5" x2="12" y2="19"/>
              <line x1="5" y1="12" x2="19" y2="12"/>
            </svg>
            {$t('settings.llmProvider.createFirstConfig')}
          </button>
        </EmptyState>
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
            <span class="toggle-title" id="auto-summary-title">{$t('settings.llm.autoSummary')}</span>
            <span class="toggle-desc">{$t('settings.llm.autoSummaryDesc')}</span>
          </div>
          <div class="toggle-label">
            <Toggle id="auto-summary-toggle" labelledby="auto-summary-title" checked={autoSummaryEnabled} onchange={(v) => { autoSummaryEnabled = v; saveAutoSummary(); }} disabled={autoSummaryLoading || (!systemSummaryEnabled && !isAdmin)} />
          </div>
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
              <span class="toggle-title" id="system-summary-title">
                {$t('settings.llm.systemSummary')}
                <span class="admin-badge">{$t('settings.sharing.adminBadge')}</span>
              </span>
              <span class="toggle-desc">{$t('settings.llm.systemSummaryDesc')}</span>
            </div>
            <div class="toggle-label">
              <Toggle id="system-summary-toggle" labelledby="system-summary-title" checked={systemSummaryEnabled} onchange={(v) => { systemSummaryEnabled = v; saveSystemSummary(); }} disabled={systemSummaryLoading} />
            </div>
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

<!-- Confirmation dialog -->
<ConfirmationModal
  bind:isOpen={$confirmOpen}
  title={$confirmTitle}
  message={$confirmMessage}
  confirmText={$confirmLabel}
  cancelText={$confirmCancelLabel}
  variant={$confirmVariant}
  on:confirm={confirmDialog.confirm}
  on:cancel={confirmDialog.cancel}
  on:close={confirmDialog.cancel}
/>

<style>
  .llm-settings {
    max-width: 800px;
    margin: 0 auto;
  }


  .saved-configs-section {
    margin-bottom: var(--space-8);
  }

  .section-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: var(--space-4);
    padding-bottom: var(--space-2);
    border-bottom: 1px solid var(--border-color);
  }

  .section-header h4 {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    margin: 0;
    font-size: 1.125rem;
    font-weight: var(--font-weight-medium);
    color: var(--text-color);
  }

  .config-list {
    display: flex;
    flex-direction: column;
    gap: var(--space-4);
  }

  .config-item {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: var(--space-4);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-md);
    background: var(--card-background);
    transition: all var(--duration-base) ease;
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
    font-weight: var(--font-weight-medium);
    color: var(--text-color);
    font-size: var(--font-size-sm);
    margin-bottom: var(--space-1);
  }

  .config-provider {
    font-size: 0.8rem;
    color: var(--text-muted);
    margin-bottom: var(--space-1);
  }

  .config-url {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    font-family: var(--font-mono);
    opacity: 0.7;
  }

  .config-actions {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    flex-shrink: 0;
  }

  .config-status {
    display: inline-flex;
    align-items: center;
    gap: var(--space-2);
    padding: var(--space-1-5) var(--space-3);
    border-radius: var(--radius-sm);
    font-size: 0.8rem;
    font-weight: var(--font-weight-medium);
    width: fit-content;
  }

  .config-status.currently-active {
    background-color: var(--success-bg);
    color: var(--success-text);
    border: 1px solid var(--success-border);
  }

  .status-indicator {
    display: flex;
    align-items: center;
    justify-content: center;
  }

  .delete-all-section {
    margin-top: var(--space-8);
    padding-top: var(--space-4);
    border-top: 1px solid var(--border-color);
    text-align: center;
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
    margin-top: var(--space-6);
  }

  .share-badge {
    display: inline-flex;
    align-items: center;
    padding: 1px var(--space-1-5);
    border-radius: var(--radius-xl);
    font-size: 0.625rem;
    font-weight: var(--font-weight-semibold);
    text-transform: uppercase;
    letter-spacing: 0.5px;
    background: rgba(var(--accent-rgb), 0.12);
    color: var(--primary-on-surface);
    margin-left: var(--space-2);
    vertical-align: middle;
  }

  :global([data-theme='dark']) .share-badge {
    background: rgba(var(--primary-color-rgb), 0.2);
    color: #60a5fa;
  }

  .admin-badge {
    display: inline-flex;
    align-items: center;
    padding: 1px var(--space-1-5);
    border-radius: var(--radius-xl);
    font-size: 0.625rem;
    font-weight: var(--font-weight-semibold);
    text-transform: uppercase;
    letter-spacing: 0.5px;
    background: rgba(245, 158, 11, 0.12);
    color: #d97706;
    margin-left: var(--space-2);
    vertical-align: middle;
  }

  :global([data-theme='dark']) .admin-badge {
    background: rgba(245, 158, 11, 0.2);
    color: #fbbf24;
  }

  .shared-by {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    font-style: italic;
    margin-top: var(--space-0-5);
  }

  .share-toggle-row {
    display: flex;
    align-items: center;
    gap: var(--space-3);
    margin-top: var(--space-2);
    padding-top: var(--space-2);
    border-top: 1px dashed var(--border-color);
  }

  .toggle-label {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    cursor: pointer;
    font-size: var(--font-size-xs);
    color: var(--text-muted);
  }

  .toggle-text {
    user-select: none;
  }

  /* Auto-summary settings */
  .auto-summary-section {
    margin-top: var(--space-8);
    padding-top: var(--space-4);
  }

  .summary-toggle-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: var(--space-4);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-md);
    background: var(--card-background);
    margin-bottom: var(--space-2);
  }

  .summary-toggle-row.admin-toggle {
    border-color: var(--warning-color);
    background: rgba(245, 158, 11, 0.04);
    margin-top: var(--space-4);
  }

  :global([data-theme='dark']) .summary-toggle-row.admin-toggle {
    background: rgba(245, 158, 11, 0.08);
  }

  .toggle-info {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
    flex: 1;
    min-width: 0;
  }

  .toggle-title {
    font-weight: var(--font-weight-medium);
    font-size: var(--font-size-sm);
    color: var(--text-color);
  }

  .toggle-desc {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    line-height: 1.4;
  }

  .summary-hint {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    padding: var(--space-2) var(--space-4);
    background: var(--surface-color);
    border-radius: var(--radius-sm);
    margin-bottom: var(--space-2);
    line-height: 1.4;
  }

  .summary-hint.warning {
    color: var(--warning-text);
    background: rgba(245, 158, 11, 0.08);
    border-left: 3px solid var(--warning-color);
  }

  @media (max-width: 768px) {
    .llm-settings {
      max-width: 100%;
    }

    .section-header {
      flex-wrap: wrap;
      gap: var(--space-2);
    }

    .section-header h4 {
      font-size: var(--font-size-base);
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
      gap: var(--space-3);
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
      gap: var(--space-1-5);
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

  }
</style>
