<script lang="ts">
  import { createEventDispatcher, onDestroy } from 'svelte';
  import { lockScroll, unlockScroll } from '$lib/scrollLock';
  import { LLMSettingsApi, type UserLLMSettings, type ProviderDefaults } from '../../lib/api/llmSettings';
  import { toastStore } from '../../stores/toast';
  import { t } from '$stores/locale';
  import { getErrorMessage } from '$lib/utils/apiError';
  import Spinner from '../ui/Spinner.svelte';
  import ConfirmationModal from '../ConfirmationModal.svelte';
  import BaseModal from '../ui/BaseModal.svelte';

  export let show = false;
  export let editingConfig: UserLLMSettings | null = null;
  export let supportedProviders: ProviderDefaults[] = [];

  const dispatch = createEventDispatcher();

  // Form data
  let formData = {
    name: '',
    provider: '' as any,
    model_name: '',
    api_key: '',
    base_url: '',
    max_tokens: 4096,
    temperature: '0.3',
    is_active: true,
    is_shared: false
  };

  // Form state
  let saving = false;
  let testing = false;
  let showApiKey = false;
  let isDirty = false;
  let originalFormData = {
    name: '',
    provider: '' as any,
    model_name: '',
    api_key: '',
    base_url: '',
    max_tokens: 4096,
    temperature: '0.3',
    is_active: true,
    is_shared: false
  };

  // Ollama model discovery
  let loadingOllamaModels = false;
  let ollamaModels: Array<{
    name: string;
    size: number;
    modified_at: string;
    digest: string;
    details: any;
    display_name: string;
  }> = [];
  let showModelSelector = false;

  // OpenAI-compatible model discovery
  let loadingOpenAIModels = false;
  let openaiCompatibleModels: Array<{
    name: string;
    id: string;
    owned_by: string;
    created: number;
  }> = [];
  let showOpenAIModelSelector = false;

  // Anthropic model discovery
  let loadingAnthropicModels = false;
  let anthropicModels: Array<{
    id: string;
    display_name: string;
    created_at: string;
    type: string;
  }> = [];
  let showAnthropicModelSelector = false;


  // Unsaved changes modal
  let showUnsavedChangesModal = false;
  let pendingCloseAction: (() => void) | null = null;

  // Track when the modal was last opened to prevent repeated form population
  let lastEditingConfigId: string | null = null;

  // Only populate form when modal opens or editingConfig changes
  $: if (show) {
    if (editingConfig && editingConfig.uuid !== lastEditingConfigId) {
      populateForm(editingConfig);
      lastEditingConfigId = editingConfig.uuid;
    } else if (!editingConfig && lastEditingConfigId !== null) {
      resetForm();
      lastEditingConfigId = null;
    }
  } else {
    // Reset tracking when modal closes
    lastEditingConfigId = null;
  }

  // Track form changes for dirty state
  $: isDirty = JSON.stringify(formData) !== JSON.stringify(originalFormData);

  // Form validation
  $: isFormValid = (() => {
    if (!formData.name.trim() || !formData.provider || !formData.model_name.trim()) {
      return false;
    }

    // Provider-specific validation
    const providerConfig = getProviderDefaults(formData.provider);
    if (!providerConfig) return false;

    // Check if API key is required (only for new configurations)
    if (providerConfig.requires_api_key && !formData.api_key.trim() && !editingConfig) {
      return false;
    }

    // Check base URL for providers that support custom URLs
    if (providerConfig.supports_custom_url && !formData.base_url.trim()) {
      return false;
    }

    return true;
  })();

  // Connection test validation
  $: isConnectionTestValid = (() => {
    if (!formData.provider || !formData.model_name) {
      return false;
    }
    if (formData.provider === 'ollama' && !formData.base_url) {
      return false;
    }
    // Check if API key is required and present (for new configs or if no stored key)
    const providerConfig = getProviderDefaults(formData.provider);
    if (providerConfig?.requires_api_key) {
      // In edit mode with stored key, allow test even without re-entering key
      if (editingConfig?.has_api_key) {
        return true;
      }
      // Otherwise require API key to be entered
      if (!formData.api_key?.trim()) {
        return false;
      }
    }
    return true;
  })();

  // Loading state for API key fetch
  let loadingApiKey = false;

  async function populateForm(config: UserLLMSettings) {
    formData = {
      name: config.name,
      provider: config.provider as any,
      model_name: config.model_name,
      api_key: '', // Will be populated below if exists
      base_url: config.base_url || '',
      max_tokens: config.max_tokens,
      temperature: config.temperature,
      is_active: config.is_active,
      is_shared: config.is_shared || false
    };

    // Fetch the actual API key if one is stored
    if (config.has_api_key && config.uuid) {
      loadingApiKey = true;
      try {
        const result = await LLMSettingsApi.getConfigApiKey(config.uuid);
        if (result.api_key) {
          formData.api_key = result.api_key;
          formData = { ...formData }; // Trigger reactivity
        }
      } catch (err) {
        console.error('Failed to fetch API key:', err);
        // Don't show error to user, just leave field empty
      } finally {
        loadingApiKey = false;
      }
    }

    originalFormData = { ...formData };
  }

  function resetForm() {
    formData = {
      name: '',
      provider: '' as any,
      model_name: '',
      api_key: '',
      base_url: '',
      max_tokens: 4096,
      temperature: '0.3',
      is_active: true,
      is_shared: false
    };
    originalFormData = { ...formData };
    ollamaModels = [];
    showModelSelector = false;
    openaiCompatibleModels = [];
    showOpenAIModelSelector = false;
    anthropicModels = [];
    showAnthropicModelSelector = false;
  }


  function getProviderDefaults(provider: any): ProviderDefaults | undefined {
    return supportedProviders.find(p => p.provider === provider);
  }

  // Function to position tooltip dynamically
  function positionTooltip(event: MouseEvent) {
    const target = event.target as Element | null;
    const tooltip = target?.closest('.info-tooltip');
    if (!(tooltip instanceof HTMLElement)) return;
    const rect = tooltip.getBoundingClientRect();

    tooltip.style.setProperty('--tooltip-left', `${rect.left + rect.width / 2}px`);
    tooltip.style.setProperty('--tooltip-top', `${rect.bottom}px`);
  }

  function closeModal(force: boolean = false) {
    if (!force && isDirty) {
      // Show professional confirmation modal instead of browser alert
      pendingCloseAction = () => executeCloseModal();
      showUnsavedChangesModal = true;
      return;
    }

    executeCloseModal();
  }

  function executeCloseModal() {
    show = false;
    dispatch('close');
    // Reset form state
    resetForm();
  }

  function handleUnsavedChangesConfirm() {
    if (pendingCloseAction) {
      pendingCloseAction();
      pendingCloseAction = null;
    }
    showUnsavedChangesModal = false;
  }

  function handleUnsavedChangesCancel() {
    showUnsavedChangesModal = false;
    pendingCloseAction = null;
  }

  async function saveConfiguration() {
    if (!isFormValid) return;

    saving = true;

    try {
      let savedConfig;
      if (editingConfig) {
        savedConfig = await LLMSettingsApi.updateSettings(editingConfig.uuid, formData);
        toastStore.success($t('llm.configUpdated'), 5000);
      } else {
        savedConfig = await LLMSettingsApi.createSettings(formData);
        toastStore.success($t('llm.configCreated'), 5000);
      }

      dispatch('saved', savedConfig);
      closeModal(true);
    } catch (err: unknown) {
      const errorMsg = getErrorMessage(err, $t('llm.configSaveFailed'));
      toastStore.error(errorMsg, 8000);
    } finally {
      saving = false;
    }
  }

  async function testConnection() {
    if (!isConnectionTestValid) return;

    testing = true;

    try {
      const result = await LLMSettingsApi.testConnection({
        provider: formData.provider,
        model_name: formData.model_name,
        api_key: formData.api_key?.trim() || undefined,
        base_url: formData.base_url || undefined,
        config_id: editingConfig?.uuid  // Pass config UUID for edit mode to use stored key
      });

      if (result.success) {
        toastStore.success(result.message, 5000);
      } else {
        toastStore.error(result.message, 8000);
      }
    } catch (err: unknown) {
      const errorMsg = getErrorMessage(err, $t('llm.connectionTestFailed'));
      toastStore.error(errorMsg, 8000);
    } finally {
      testing = false;
    }
  }

  async function loadOllamaModels() {
    if (!formData.base_url) {
      toastStore.error($t('llm.enterBaseUrlFirst'));
      return;
    }

    loadingOllamaModels = true;

    try {
      const result = await LLMSettingsApi.getOllamaModels(formData.base_url);
      if (result.success && result.models) {
        ollamaModels = result.models;
        showModelSelector = true;
      } else {
        toastStore.error(result.message);
      }
    } catch (err: unknown) {
      toastStore.error(getErrorMessage(err, $t('llm.ollamaLoadFailed')));
    } finally {
      loadingOllamaModels = false;
    }
  }

  function selectOllamaModel(modelName: string, model?: any) {
    formData.model_name = modelName;
    formData = { ...formData }; // Force reactivity update
    showModelSelector = false;
  }

  function formatModelSize(size: number): string {
    if (size === 0) return '';
    const gb = (size / (1024 ** 3)).toFixed(1);
    return `${gb}GB`;
  }

  async function loadOpenAICompatibleModels() {
    if (!formData.base_url) {
      toastStore.error($t('llm.enterBaseUrlFirst'));
      return;
    }

    // Check if API key is required for this provider
    const providerConfig = getProviderDefaults(formData.provider);
    const hasStoredKey = editingConfig?.has_api_key;
    if (providerConfig?.requires_api_key && !formData.api_key?.trim() && !hasStoredKey) {
      toastStore.error($t('llm.enterApiKeyFirst'));
      return;
    }

    loadingOpenAIModels = true;

    try {
      const result = await LLMSettingsApi.getOpenAICompatibleModels(
        formData.base_url,
        formData.api_key?.trim() || undefined,
        editingConfig?.uuid  // Pass config UUID for edit mode to use stored key
      );
      if (result.success && result.models) {
        openaiCompatibleModels = result.models;
        showOpenAIModelSelector = true;
      } else {
        toastStore.error(result.message);
      }
    } catch (err: unknown) {
      toastStore.error(getErrorMessage(err, $t('llm.modelsLoadFailed')));
    } finally {
      loadingOpenAIModels = false;
    }
  }

  function selectOpenAIModel(modelId: string) {
    formData.model_name = modelId;
    formData = { ...formData }; // Force reactivity update
    showOpenAIModelSelector = false;
  }

  async function loadAnthropicModels() {
    // In edit mode, we can use stored key if no new key is entered
    const hasKey = formData.api_key?.trim() || editingConfig?.has_api_key;
    if (!hasKey) {
      toastStore.error($t('llm.enterApiKeyFirst'));
      return;
    }

    loadingAnthropicModels = true;

    try {
      const result = await LLMSettingsApi.getAnthropicModels(
        formData.api_key?.trim() || undefined,  // Only send if user typed a new key
        editingConfig?.uuid  // Backend will use stored key if no api_key provided
      );
      if (result.success && result.models) {
        anthropicModels = result.models;
        showAnthropicModelSelector = true;
      } else {
        toastStore.error(result.message);
      }
    } catch (err: unknown) {
      toastStore.error(getErrorMessage(err, $t('llm.modelsLoadFailed')));
    } finally {
      loadingAnthropicModels = false;
    }
  }

  function selectAnthropicModel(modelId: string) {
    formData.model_name = modelId;
    formData = { ...formData }; // Force reactivity update
    showAnthropicModelSelector = false;
  }

  // Track provider changes to reset defaults
  let previousProvider: string = '';

  // Apply provider defaults when provider changes
  $: if (formData.provider) {
    const defaults = getProviderDefaults(formData.provider);
    if (defaults && !editingConfig) {
      // Detect if provider actually changed (not just initialization)
      const providerChanged = previousProvider !== '' && previousProvider !== formData.provider;

      if (providerChanged) {
        // Provider changed - reset and apply new defaults
        formData.base_url = defaults.default_base_url || '';
        formData.model_name = defaults.default_model || '';
        // Close any open model selectors
        showModelSelector = false;
        showOpenAIModelSelector = false;
        showAnthropicModelSelector = false;
      } else {
        // Initial load or same provider - only apply if empty
        if (!formData.base_url && defaults.default_base_url) {
          formData.base_url = defaults.default_base_url;
        }
        if (!formData.model_name && defaults.default_model) {
          formData.model_name = defaults.default_model;
        }
      }

      // Track current provider for next change detection
      previousProvider = formData.provider;
    }
  }

  let _llmConfigModalWasOpen = false;
  $: {
    const _anyOpen = show || showUnsavedChangesModal;
    if (_anyOpen !== _llmConfigModalWasOpen) {
      _anyOpen ? lockScroll() : unlockScroll();
      _llmConfigModalWasOpen = _anyOpen;
    }
  }

  onDestroy(() => {
    if (_llmConfigModalWasOpen) unlockScroll();
  });

  function getProviderDisplayName(provider: string): string {
    const displayNames: Record<string, string> = {
      openai: $t('llm.provider.openai'),
      vllm: $t('llm.provider.vllm'),
      ollama: $t('llm.provider.ollama'),
      claude: $t('llm.provider.claude'),
      anthropic: $t('llm.provider.anthropic'),
      openrouter: $t('llm.provider.openrouter'),
      bedrock: $t('llm.provider.bedrock'),
      custom: $t('llm.provider.custom')
    };
    return displayNames[provider] || provider;
  }
</script>

<BaseModal isOpen={show} onClose={() => closeModal()} maxWidth="600px">
  <svelte:fragment slot="header">
    <h2 class="modal-title">{editingConfig ? $t('llm.editConfiguration') : $t('llm.createConfiguration')}</h2>
    {#if isDirty}
      <span class="unsaved-dot" title={$t('llm.unsavedChangesIndicator')}>●</span>
    {/if}
  </svelte:fragment>

      <form on:submit|preventDefault={saveConfiguration} class="config-form">
        <!-- Configuration Name -->
        <div class="form-group">
          <label for="config-name">{$t('llm.configName')}</label>
          <input
            type="text"
            id="config-name"
            bind:value={formData.name}
            disabled={saving}
            class="form-control"
            placeholder={$t('llm.configNamePlaceholder')}
            required
          />
        </div>

        <!-- Provider Selection -->
        <div class="form-group">
          <label for="provider">{$t('llm.provider')}</label>
          <select
            id="provider"
            bind:value={formData.provider}
            disabled={saving}
            class="form-control"
            required
          >
            <option value="">{$t('llm.selectProvider')}</option>
            {#each supportedProviders.sort((a, b) => getProviderDisplayName(a.provider).localeCompare(getProviderDisplayName(b.provider))) as provider}
              <option value={provider.provider}>{getProviderDisplayName(provider.provider)}</option>
            {/each}
          </select>
        </div>

        {#if formData.provider}
          <!-- Bedrock: no base URL / API key to enter — credentials and region are a
               deployment-level setting (BEDROCK_REGION / AWS credential chain), not
               per-configuration. Say so instead of leaving the form looking incomplete. -->
          {#if formData.provider === 'bedrock'}
            <div class="form-group">
              <p class="field-desc">{$t('llm.bedrockInfo')}</p>
            </div>
          {/if}

          <!-- Base URL (if supported) -->
          {#if getProviderDefaults(formData.provider)?.supports_custom_url}
            <div class="form-group">
              <label for="base-url">
                <span class="label-with-tooltip">
                  {$t('llm.baseUrl')}
                  {#if formData.provider === 'vllm' || formData.provider === 'ollama'}
                    <span
                      class="info-tooltip"
                      role="tooltip"
                      data-tooltip={$t('llm.baseUrlTooltipLocal')}
                      on:mouseenter={positionTooltip}
                    >
                      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                        <circle cx="12" cy="12" r="10"/>
                        <path d="M12 16v-4"/>
                        <path d="M12 8h.01"/>
                      </svg>
                    </span>
                  {:else if formData.provider === 'openrouter'}
                    <span
                      class="info-tooltip"
                      role="tooltip"
                      data-tooltip={$t('llm.baseUrlTooltipCloud')}
                      on:mouseenter={positionTooltip}
                    >
                      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                        <circle cx="12" cy="12" r="10"/>
                        <path d="M12 16v-4"/>
                        <path d="M12 8h.01"/>
                      </svg>
                    </span>
                  {/if}
                </span>
              </label>
              <input
                type="url"
                id="base-url"
                bind:value={formData.base_url}
                disabled={saving}
                class="form-control"
                placeholder={getProviderDefaults(formData.provider)?.default_base_url || 'https://api.example.com/v1'}
                required
              />
            </div>
          {/if}

          <!-- Model Selection -->
          <div class="form-group">
            <label for="model-name">
              {$t('llm.modelName')}
              {#if formData.provider === 'ollama'}
                <button
                  type="button"
                  class="discover-models-btn"
                  on:click={loadOllamaModels}
                  disabled={loadingOllamaModels || saving || !formData.base_url}
                  title={$t('llm.discoverModelsTooltip')}
                >
                  {#if loadingOllamaModels}
                    <Spinner size="small" />
                  {:else}
                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
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
                  {/if}
                  {$t('llm.discoverModels')}
                </button>
              {:else if formData.provider === 'openai' || formData.provider === 'vllm' || formData.provider === 'openrouter'}
                <button
                  type="button"
                  class="discover-models-btn"
                  on:click={loadOpenAICompatibleModels}
                  disabled={loadingOpenAIModels || saving || loadingApiKey || !formData.base_url || (getProviderDefaults(formData.provider)?.requires_api_key && !formData.api_key?.trim() && !editingConfig?.has_api_key)}
                  title={$t('llm.discoverModelsTooltip')}
                >
                  {#if loadingOpenAIModels}
                    <Spinner size="small" />
                  {:else}
                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
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
                  {/if}
                  {$t('llm.discoverModels')}
                </button>
              {:else if formData.provider === 'anthropic' || formData.provider === 'claude'}
                <button
                  type="button"
                  class="discover-models-btn"
                  on:click={loadAnthropicModels}
                  disabled={loadingAnthropicModels || saving || loadingApiKey || (!formData.api_key?.trim() && !editingConfig?.has_api_key)}
                  title={$t('llm.discoverModelsTooltip')}
                >
                  {#if loadingAnthropicModels}
                    <Spinner size="small" />
                  {:else}
                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
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
                  {/if}
                  {$t('llm.discoverModels')}
                </button>
              {/if}
            </label>
            <input
              type="text"
              id="model-name"
              bind:value={formData.model_name}
              disabled={saving}
              class="form-control"
              placeholder={getProviderDefaults(formData.provider)?.default_model || $t('llm.modelNamePlaceholder')}
              required
            />

            <!-- Ollama Model Selector -->
            {#if showModelSelector && ollamaModels.length > 0}
              <div class="model-selector">
                <h4>{$t('llm.availableModels')}</h4>
                <div class="model-list">
                  {#each ollamaModels as model}
                    <button
                      type="button"
                      class="model-item"
                      on:click={() => selectOllamaModel(model.name, model)}
                    >
                      <div class="model-info">
                        <div class="model-name">{model.display_name}</div>
                        <div class="model-details">{formatModelSize(model.size)}</div>
                      </div>
                    </button>
                  {/each}
                </div>
                <button type="button" class="close-selector" on:click={() => showModelSelector = false}>
                  {$t('llm.closeSelector')}
                </button>
              </div>
            {/if}


            <!-- OpenAI-Compatible Model Selector -->
            {#if showOpenAIModelSelector && openaiCompatibleModels.length > 0}
              <div class="model-selector">
                <h4>{$t('llm.availableModels')}</h4>
                <div class="model-list">
                  {#each openaiCompatibleModels as model}
                    <button
                      type="button"
                      class="model-item"
                      on:click={() => selectOpenAIModel(model.id)}
                    >
                      <div class="model-info">
                        <div class="model-name">{model.id}</div>
                        {#if model.owned_by}
                          <div class="model-details">{model.owned_by}</div>
                        {/if}
                      </div>
                    </button>
                  {/each}
                </div>
                <button type="button" class="close-selector" on:click={() => showOpenAIModelSelector = false}>
                  {$t('llm.closeSelector')}
                </button>
              </div>
            {/if}

            <!-- Anthropic Model Selector -->
            {#if showAnthropicModelSelector && anthropicModels.length > 0}
              <div class="model-selector">
                <h4>{$t('llm.availableModels')}</h4>
                <div class="model-list">
                  {#each anthropicModels as model}
                    <button
                      type="button"
                      class="model-item"
                      on:click={() => selectAnthropicModel(model.id)}
                    >
                      <div class="model-info">
                        <div class="model-name">{model.display_name}</div>
                        <div class="model-details">{model.id}</div>
                      </div>
                    </button>
                  {/each}
                </div>
                <button type="button" class="close-selector" on:click={() => showAnthropicModelSelector = false}>
                  {$t('llm.closeSelector')}
                </button>
              </div>
            {/if}

          </div>

          <!-- API Key (if required) -->
          {#if getProviderDefaults(formData.provider)?.requires_api_key || (editingConfig && editingConfig.has_api_key)}
            <div class="form-group">
              <div class="api-key-header">
                <label for="api-key">
                  {$t('llm.apiKey')} {#if !editingConfig}*{/if}
                </label>
                <button
                  type="button"
                  class="toggle-password"
                  on:click={() => showApiKey = !showApiKey}
                  tabindex="-1"
                  aria-label={showApiKey ? $t('llm.hideApiKey') : $t('llm.showApiKey')}
                >
                  {#if showApiKey}
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                      <path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7Z"/>
                      <circle cx="12" cy="12" r="3"/>
                    </svg>
                  {:else}
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                      <path d="m15 18-.722-3.25"/>
                      <path d="m2 2 20 20"/>
                      <path d="m9 9-.637 3.181"/>
                      <path d="M12.5 5.5c2.13.13 4.16 1.11 5.5 3.5-.274.526-.568 1.016-.891 1.469"/>
                      <path d="M2 12s3-7 10-7c1.284 0 2.499.23 3.62.67"/>
                      <path d="m18.147 8.476.853 3.524"/>
                    </svg>
                  {/if}
                </button>
              </div>
              <input
                type={showApiKey ? 'text' : 'password'}
                id="api-key"
                bind:value={formData.api_key}
                disabled={saving}
                class="form-control"
                placeholder={$t('llm.apiKeyPlaceholder')}
                required={!editingConfig}
              />
            </div>
          {/if}

          <!-- Advanced Settings -->
          <div class="advanced-settings">
            <h4>{$t('llm.advancedSettings')}</h4>

            <div class="form-row">
              <div class="form-group">
                <label for="max-tokens">{$t('llm.maxTokens')}</label>
                <input
                  type="number"
                  id="max-tokens"
                  bind:value={formData.max_tokens}
                  disabled={saving}
                  class="form-control"
                  min="100"
                  max="2000000"
                />
                <p class="field-desc">{$t('llm.maxTokensHelp')}</p>
              </div>

              <div class="form-group">
                <label for="temperature">{$t('llm.temperature')}</label>
                <input
                  type="text"
                  id="temperature"
                  bind:value={formData.temperature}
                  disabled={saving}
                  class="form-control"
                  placeholder="0.3"
                />
              </div>

            </div>
          </div>

          <!-- Test Connection -->
          <div class="test-section">
            <button
              type="button"
              class="test-connection-btn"
              on:click={testConnection}
              disabled={testing || loadingApiKey || !isConnectionTestValid}
              title={$t('llm.testConnectionTooltip')}
            >
              {#if testing}
                <Spinner size="small" />
                {$t('llm.testing')}
              {:else}
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <polyline points="22,12 18,12 15,21 9,3 6,12 2,12"/>
                </svg>
                {$t('llm.testConnection')}
              {/if}
            </button>

          </div>
        {/if}

        <!-- Share with all users -->
        <div class="share-toggle-row">
          <label class="toggle-label">
            <input type="checkbox" class="toggle-input" bind:checked={formData.is_shared} disabled={saving} />
            <span class="toggle-switch"></span>
            <span class="toggle-text">{$t('settings.llmProvider.shareGlobally')}</span>
          </label>
        </div>

        <!-- Form Actions -->
        <div class="form-actions">
          <button type="button" class="cancel-button" on:click={() => closeModal()} disabled={saving}>
            {$t('common.cancel')}
          </button>
          <button
            type="submit"
            class="save-button primary"
            disabled={saving || !isFormValid}
          >
            {#if saving}
              <Spinner size="small" />
              {$t('llm.saving')}
            {:else}
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/>
                <polyline points="17,21 17,13 7,13 7,21"/>
                <polyline points="7,3 7,8 15,8"/>
              </svg>
              {editingConfig ? $t('llm.updateConfiguration') : $t('llm.saveConfiguration')}
            {/if}
          </button>
        </div>
      </form>
</BaseModal>

<!-- Unsaved Changes Modal -->
<ConfirmationModal
  bind:isOpen={showUnsavedChangesModal}
  title={$t('llm.unsavedChanges')}
  message={$t('llm.unsavedChangesMessage')}
  confirmText={$t('llm.discardChanges')}
  cancelText={$t('llm.keepEditing')}
  confirmButtonClass="modal-warning-button"
  cancelButtonClass="modal-primary-button"
  on:confirm={handleUnsavedChangesConfirm}
  on:cancel={handleUnsavedChangesCancel}
  on:close={handleUnsavedChangesCancel}
/>

<style>
  .modal-title {
    margin: 0;
    font-size: 1.25rem;
    font-weight: 600;
    color: var(--text-color);
  }

  .field-desc {
    font-size: 0.75rem;
    color: var(--text-muted);
    margin: 0.375rem 0 0 0;
    line-height: 1.5;
  }

  .unsaved-dot {
    color: var(--warning-color);
    font-size: 0.9rem;
    line-height: 1;
    flex-shrink: 0;
  }

  .config-form {
    padding: 1.5rem;
  }

  .form-group {
    margin-bottom: 1.5rem;
  }

  .form-group label {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 0.5rem;
    font-weight: 500;
    color: var(--text-color);
  }

  .form-control {
    width: 100%;
    padding: 0.75rem;
    border: 1px solid var(--border-color);
    border-radius: 4px;
    background-color: var(--input-bg);
    color: var(--text-color);
    font-size: 0.875rem;
    transition: border-color 0.2s ease;
  }

  .form-control:focus {
    outline: none;
    border-color: var(--primary-color);
    box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.1);
  }

  .discover-models-btn {
    display: flex;
    align-items: center;
    gap: 0.25rem;
    background: #3b82f6;
    color: white;
    border: none;
    padding: 0.25rem 0.5rem;
    border-radius: 4px;
    font-size: 0.75rem;
    cursor: pointer;
    transition: background-color 0.2s ease;
  }

  .discover-models-btn:hover:not(:disabled) {
    background: #2563eb;
  }

  .discover-models-btn:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .api-key-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
  }

  .toggle-password {
    background: none;
    border: none;
    cursor: pointer;
    padding: 4px;
    color: var(--text-secondary, var(--text-muted));
    display: flex;
    align-items: center;
    border-radius: 4px;
    transition: background-color 0.2s, color 0.2s;
  }

  .toggle-password:hover {
    background-color: var(--background-color, var(--hover-color));
    color: var(--text-color);
  }

  .advanced-settings {
    margin: 1.5rem 0;
    padding: 1rem;
    border: 1px solid var(--border-color);
    border-radius: 4px;
    background: var(--card-bg);
  }

  .advanced-settings h4 {
    margin: 0 0 1rem;
    font-size: 1rem;
    font-weight: 500;
    color: var(--text-color);
  }

  .form-row {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
    gap: 1rem;
  }

  .model-selector {
    margin-top: 0.5rem;
    border: 1px solid var(--border-color);
    border-radius: 4px;
    background: var(--card-bg);
    padding: 1rem;
  }

  .model-selector h4 {
    margin: 0 0 0.5rem;
    font-size: 0.875rem;
    font-weight: 500;
  }

  .model-list {
    max-height: 200px;
    overflow-y: auto;
    margin-bottom: 0.5rem;
  }

  .model-item {
    width: 100%;
    text-align: left;
    padding: 0.5rem;
    border: 1px solid var(--border-color);
    background: var(--input-bg);
    margin-bottom: 0.25rem;
    border-radius: 4px;
    cursor: pointer;
    transition: all 0.2s ease;
  }

  .model-item:hover {
    background: var(--hover-color);
    border-color: var(--primary-color);
  }

  .model-name {
    font-weight: 500;
    font-size: 0.875rem;
  }

  .model-details {
    font-size: 0.75rem;
    color: var(--text-muted);
  }

  .close-selector {
    background: var(--secondary-color);
    color: white;
    border: none;
    padding: 0.5rem 1rem;
    border-radius: 4px;
    cursor: pointer;
    font-size: 0.875rem;
  }

  .test-section {
    margin: 1.5rem 0;
    padding: 1rem;
    border: 1px solid var(--border-color);
    border-radius: 4px;
    background: var(--card-bg);
  }

  .test-connection-btn {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    background: #3b82f6;
    color: white;
    border: none;
    padding: 0.6rem 1.2rem;
    border-radius: 10px;
    cursor: pointer;
    font-size: 0.95rem;
    font-weight: 500;
    transition: all 0.2s ease;
    box-shadow: 0 2px 4px rgba(var(--primary-color-rgb), 0.2);
  }

  .test-connection-btn:hover:not(:disabled) {
    background: #2563eb;
    transform: scale(1.02);
    box-shadow: 0 4px 8px rgba(var(--primary-color-rgb), 0.25);
  }

  .test-connection-btn:active:not(:disabled) {
    transform: scale(1);
  }

  .test-connection-btn:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .share-toggle-row {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    margin-top: 1.5rem;
    padding-top: 1rem;
    border-top: 1px dashed var(--border-color);
  }

  .toggle-label {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    cursor: pointer;
    font-size: 0.875rem;
    font-weight: 400;
    user-select: none;
  }

  .toggle-input {
    display: none;
  }

  .toggle-switch {
    position: relative;
    width: 36px;
    height: 20px;
    background: var(--border-color);
    border-radius: 10px;
    transition: background-color 0.2s;
    flex-shrink: 0;
  }

  .toggle-switch::after {
    content: '';
    position: absolute;
    top: 2px;
    left: 2px;
    width: 16px;
    height: 16px;
    background: white;
    border-radius: 50%;
    transition: transform 0.2s;
  }

  .toggle-input:checked + .toggle-switch {
    background: #3b82f6;
  }

  .toggle-input:checked + .toggle-switch::after {
    transform: translateX(16px);
  }

  .toggle-text {
    color: var(--text-color);
  }

  .form-actions {
    display: flex;
    justify-content: flex-end;
    gap: 1rem;
    margin-top: 2rem;
    padding-top: 1rem;
    border-top: 1px solid var(--border-color);
  }

  .cancel-button {
    background: var(--surface-color);
    color: var(--text-color);
    border: 1px solid var(--border-color);
    padding: 0.6rem 1.2rem;
    border-radius: 10px;
    cursor: pointer;
    font-size: 0.95rem;
    font-weight: 500;
    transition: all 0.2s ease;
    box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1);
  }

  .cancel-button:hover:not(:disabled) {
    background: var(--button-hover);
    transform: scale(1.02);
    box-shadow: 0 4px 8px rgba(0, 0, 0, 0.1);
  }

  .cancel-button:active {
    transform: scale(1);
  }

  .save-button {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    background: #3b82f6;
    color: white;
    border: none;
    padding: 0.6rem 1.2rem;
    border-radius: 10px;
    cursor: pointer;
    font-size: 0.95rem;
    font-weight: 500;
    transition: all 0.2s ease;
    box-shadow: 0 2px 4px rgba(var(--primary-color-rgb), 0.2);
  }

  .save-button:hover:not(:disabled) {
    background: #2563eb;
    transform: scale(1.02);
    box-shadow: 0 4px 8px rgba(var(--primary-color-rgb), 0.25);
  }

  .save-button:active:not(:disabled) {
    transform: scale(1);
  }

  .save-button:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  /* Tooltip styles */
  .info-tooltip {
    display: inline-flex;
    align-items: center;
    color: var(--text-muted);
    opacity: 0.6;
    cursor: help;
    transition: opacity 0.2s ease;
    position: relative;
    margin-left: 0.5rem;
  }

  .info-tooltip:hover {
    opacity: 1;
    color: var(--primary-color);
  }

  .info-tooltip[data-tooltip]:hover::after {
    content: attr(data-tooltip);
    position: fixed;
    left: var(--tooltip-left, 50%);
    top: var(--tooltip-top, 50%);
    background: #1a1a1a;
    color: white;
    padding: 8px 12px;
    border-radius: 6px;
    font-size: 13px;
    font-weight: normal;
    max-width: 320px;
    white-space: normal;
    z-index: 9999;
    line-height: 1.4;
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.2);
    text-align: left;
    pointer-events: none;
    transform: translate(-50%, 8px);
  }

  label {
    display: flex;
    align-items: center;
    justify-content: space-between;
    font-weight: 500;
    font-size: 0.9rem;
    color: var(--text-color);
    margin-bottom: 0.5rem;
  }

  .label-with-tooltip {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }

  @media (max-width: 768px) {
    .form-actions {
      flex-direction: column;
      gap: 0.75rem;
    }

    .cancel-button,
    .save-button {
      width: 100%;
      min-height: 44px;
      justify-content: center;
    }

    .form-control {
      min-height: 44px;
      font-size: 1rem;
    }
  }

</style>
