<script lang="ts">
  import Toggle from '$components/ui/Toggle.svelte';
  import ActivateIcon from '$components/ui/ActivateIcon.svelte';
  import EyeIcon from '$components/ui/EyeIcon.svelte';
  import MenuIcon from '$components/ui/MenuIcon.svelte';
  import { createEventDispatcher, onDestroy, tick } from 'svelte';
  import { lockScroll, unlockScroll } from '$lib/scrollLock';
  import {
    LLMSettingsApi,
    llmProviderDisplayName,
    type UserLLMSettings,
    type LLMProvider,
    type ProviderDefaults,
    type LocalEndpointStatus
  } from '$lib/api/llmSettings';
  import { toastStore } from '$stores/toast';
  import { t } from '$stores/locale';
  import { getErrorMessage } from '$lib/utils/apiError';
  import {
    SCHEMA_DEFAULT_MAX_TOKENS,
    blankForm,
    formFromConfig,
    isFormValid as formIsValid,
    isConnectionTestValid as connectionTestIsValid,
    awsFields,
    buildPayload,
    formatModelSize,
    showsDiscovery,
    reconcileDiscovery,
    type LLMFormData
  } from '$lib/settings/llmConfigForm';
  import {
    discoverOllamaModels,
    discoverOpenAIModels,
    discoverAnthropicModels,
    type OllamaModel,
    type OpenAIModel,
    type AnthropicModel
  } from '$lib/settings/llmModelDiscovery';
  import { user } from '$stores/auth';
  import Spinner from '$components/ui/Spinner.svelte';
  import ConfirmationModal from '$components/ConfirmationModal.svelte';
  import BaseModal from '$components/ui/BaseModal.svelte';
  import LLMLocalDiscovery from './LLMLocalDiscovery.svelte';
  import LLMBedrockFields from './LLMBedrockFields.svelte';

  export let show = false;
  export let editingConfig: UserLLMSettings | null = null;
  export let supportedProviders: ProviderDefaults[] = [];

  const dispatch = createEventDispatcher();

  // Form data (defaults and validation live in $lib/settings/llmConfigForm)
  let formData: LLMFormData = blankForm();

  // Form state
  let saving = false;
  let testing = false;
  let showApiKey = false;
  let isDirty = false;
  let originalFormData: LLMFormData = blankForm();

  // Model discovery (Ollama / OpenAI-compatible / Anthropic)
  let loadingOllamaModels = false;
  let ollamaModels: OllamaModel[] = [];
  let showModelSelector = false;

  let loadingOpenAIModels = false;
  let openaiCompatibleModels: OpenAIModel[] = [];
  let showOpenAIModelSelector = false;

  let loadingAnthropicModels = false;
  let anthropicModels: AnthropicModel[] = [];
  let showAnthropicModelSelector = false;

  // Local endpoint discovery (issue #644) — a fixed-allowlist reachability probe over
  // this repo's own dev/test LLM overlay services, surfaced as one-click fill-ins.
  let localEndpoints: LocalEndpointStatus[] = [];
  // Defaults to `true` (no nag) until the first probe response actually says otherwise —
  // a deployment that never enabled any of these overlays must not see a warning about a
  // setting that is irrelevant to it.
  let localEndpointsPrivateAllowed = true;
  let loadingLocalEndpoints = false;

  async function loadLocalEndpoints() {
    loadingLocalEndpoints = true;
    try {
      const result = await LLMSettingsApi.getLocalEndpoints();
      localEndpoints = result.endpoints;
      localEndpointsPrivateAllowed = result.private_endpoints_allowed;
    } catch {
      // Best-effort discovery convenience, never a hard requirement of the form —
      // fail silently and leave the section empty rather than toasting an error over
      // a feature the user did not explicitly ask to run.
      localEndpoints = [];
    } finally {
      loadingLocalEndpoints = false;
    }
  }

  // Issue #644 — the "Use this" ordering trap. `formData.provider = X` reactively
  // triggers `$: if (formData.provider) {...}` below, whose `providerChanged` branch
  // OVERWRITES `formData.base_url`/`formData.model_name` from the provider catalog's
  // defaults. That reactive block does not run synchronously with this assignment —
  // Svelte batches it — so setting `base_url` immediately afterward in the same
  // function would still get silently wiped out once the batched update finally runs.
  // `await tick()` forces that reactive block to run and settle FIRST (which also
  // syncs its internal `previousProvider` tracker to the new provider), so the
  // base_url/model_name assignment below lands last and sticks.
  async function useDiscoveredEndpoint(endpoint: LocalEndpointStatus) {
    formData.provider = endpoint.provider;
    formData = { ...formData };
    await tick();
    formData.base_url = endpoint.base_url;
    if (endpoint.models.length > 0) {
      formData.model_name = endpoint.models[0];
    }
    formData = { ...formData };
  }

  $: if (show) {
    loadLocalEndpoints();
  }

  $: showDiscovery = showsDiscovery(formData.provider, loadingLocalEndpoints, localEndpoints);

  // Disclosure state: see reconcileDiscovery for the expand-by-default rule.
  let discoveryExpanded = false;
  let discoveryTouched = false;
  let discoverySeenProvider = '';
  function syncDiscovery(provider: string, endpoints: LocalEndpointStatus[]) {
    ({
      expanded: discoveryExpanded,
      touched: discoveryTouched,
      seenProvider: discoverySeenProvider
    } = reconcileDiscovery(
      { expanded: discoveryExpanded, touched: discoveryTouched, seenProvider: discoverySeenProvider },
      provider,
      endpoints
    ));
  }
  $: syncDiscovery(formData.provider, localEndpoints);

  // Bedrock account fields: any admin sees whether the server's own AWS credentials resolve.
  $: isAdmin = $user?.role === 'admin' || $user?.role === 'super_admin';
  const bedrockDefaults = getProviderDefaults('bedrock');

  // Unsaved changes modal
  let showUnsavedChangesModal = false;
  let pendingCloseAction: (() => void) | null = null;

  // Track when the modal was last opened to prevent repeated form population
  let lastEditingConfigId: string | null = null;

  // Only populate form when modal opens or editingConfig changes
  function syncEditingConfig(isShown: boolean, config: typeof editingConfig) {
    if (!isShown) {
      // Reset tracking when modal closes
      lastEditingConfigId = null;
    } else if (config && config.uuid !== lastEditingConfigId) {
      populateForm(config);
      lastEditingConfigId = config.uuid;
    } else if (!config && lastEditingConfigId !== null) {
      resetForm();
      lastEditingConfigId = null;
    }
  }
  $: syncEditingConfig(show, editingConfig);

  // Track form changes for dirty state
  $: isDirty = JSON.stringify(formData) !== JSON.stringify(originalFormData);

  $: isFormValid = formIsValid(formData, getProviderDefaults(formData.provider), Boolean(editingConfig));

  $: isConnectionTestValid = connectionTestIsValid(
    formData,
    getProviderDefaults(formData.provider),
    Boolean(editingConfig?.has_api_key)
  );

  // Loading state for API key fetch
  let loadingApiKey = false;

  async function populateForm(config: UserLLMSettings) {
    formData = formFromConfig(config);

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
    formData = blankForm();
    originalFormData = { ...formData };
    discoveryTouched = false;
    ollamaModels = [];
    showModelSelector = false;
    openaiCompatibleModels = [];
    showOpenAIModelSelector = false;
    anthropicModels = [];
    showAnthropicModelSelector = false;
    // Allow the provider-select handler (below) to re-apply the catalog
    // context window the next time this provider is picked in a fresh form —
    // otherwise the guard there would see the same provider value as last
    // time and skip re-populating `max_tokens` after the reset above.
    previousProvider = '';
    lastContextWindowProvider = '';
  }


  function getProviderDefaults(provider: string): ProviderDefaults | undefined {
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
        savedConfig = await LLMSettingsApi.updateSettings(editingConfig.uuid, buildPayload(formData, Boolean(editingConfig)));
        toastStore.success($t('llm.configUpdated'), 5000);
      } else {
        savedConfig = await LLMSettingsApi.createSettings(buildPayload(formData, Boolean(editingConfig)));
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
        provider: formData.provider as LLMProvider,
        model_name: formData.model_name,
        api_key: formData.api_key?.trim() || undefined,
        base_url: formData.base_url || undefined,
        config_id: editingConfig?.uuid, // Pass config UUID for edit mode to use stored key
        ...(formData.provider === 'bedrock'
          ? { aws_region: formData.aws_region || undefined, ...awsFields(formData) }
          : {})
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

  const discoveryCtx = (setLoading: (loading: boolean) => void) => ({ t: $t, setLoading });
  const editingKey = () =>
    editingConfig ? { uuid: editingConfig.uuid, hasApiKey: Boolean(editingConfig.has_api_key) } : null;

  async function loadOllamaModels() {
    const models = await discoverOllamaModels(
      discoveryCtx((v) => { loadingOllamaModels = v; }),
      formData.base_url
    );
    if (models) {
      ollamaModels = models;
      showModelSelector = true;
    }
  }

  function selectOllamaModel(modelName: string, _model?: OllamaModel) {
    formData.model_name = modelName;
    formData = { ...formData }; // Force reactivity update
    showModelSelector = false;
  }

  async function loadOpenAICompatibleModels() {
    const models = await discoverOpenAIModels(
      discoveryCtx((v) => { loadingOpenAIModels = v; }),
      {
        baseUrl: formData.base_url,
        apiKey: formData.api_key,
        provider: getProviderDefaults(formData.provider)
      },
      editingKey()
    );
    if (models) {
      openaiCompatibleModels = models;
      showOpenAIModelSelector = true;
    }
  }

  function selectOpenAIModel(modelId: string) {
    formData.model_name = modelId;
    formData = { ...formData }; // Force reactivity update
    showOpenAIModelSelector = false;
  }

  async function loadAnthropicModels() {
    const models = await discoverAnthropicModels(
      discoveryCtx((v) => { loadingAnthropicModels = v; }),
      formData.api_key,
      editingKey()
    );
    if (models) {
      anthropicModels = models;
      showAnthropicModelSelector = true;
    }
  }

  function selectAnthropicModel(modelId: string) {
    formData.model_name = modelId;
    formData = { ...formData }; // Force reactivity update
    showAnthropicModelSelector = false;
  }

  // Track provider changes to reset defaults
  let previousProvider: string = '';

  // Issue #873: tracks which provider's catalog context window has already
  // been applied to `formData.max_tokens`, separately from `previousProvider`
  // above. Svelte's two-way `bind:value={formData.max_tokens}` invalidates
  // the whole `formData` object on every keystroke, which re-runs this
  // reactive block even though `formData.provider` hasn't changed — an
  // unconditional assignment below would silently overwrite the token the
  // user just typed. Guarding on "have we already applied this provider's
  // catalog value" (rather than "did the provider just change") makes the
  // default apply exactly once per provider selection, on both the very
  // first pick and a later switch, and never again until the provider
  // changes again.
  let lastContextWindowProvider: string = '';

  // Apply provider defaults when provider changes
  function applyProviderDefaults(provider: string, isEditing: boolean) {
    if (!provider) return;
    const defaults = getProviderDefaults(provider);
    if (defaults && !isEditing) {
      // Detect if provider actually changed (not just initialization)
      const providerChanged = previousProvider !== '' && previousProvider !== provider;

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

      // Populate the context window from the server's provider catalog —
      // never leave it at a hardcoded literal. Falls back to the schema
      // default when the catalog has no entry for this provider (e.g.
      // Bedrock/custom, whose real maximum varies by model and is never
      // guessed). Still fully admin-editable afterward.
      if (lastContextWindowProvider !== provider) {
        formData.max_tokens = defaults.max_context_length ?? SCHEMA_DEFAULT_MAX_TOKENS;
        lastContextWindowProvider = provider;
      }

      // Track current provider for next change detection
      previousProvider = provider;
    }
  }
  $: applyProviderDefaults(formData.provider, Boolean(editingConfig));

let _llmConfigModalWasOpen = false;
  function syncScrollLock(anyOpen: boolean) {
    if (anyOpen === _llmConfigModalWasOpen) return;
    if (anyOpen) lockScroll();
    else unlockScroll();
    _llmConfigModalWasOpen = anyOpen;
  }
  $: syncScrollLock(show || showUnsavedChangesModal);

  onDestroy(() => {
    if (_llmConfigModalWasOpen) unlockScroll();
  });

</script>

<BaseModal isOpen={show} onClose={() => closeModal()} size="md">
  <svelte:fragment slot="header">
    <h2 class="modal-title">{editingConfig ? $t('llm.editConfiguration') : $t('llm.createConfiguration')}</h2>
    {#if isDirty}
      <span class="unsaved-dot" role="img" aria-label={$t('llm.unsavedChangesIndicator')} title={$t('llm.unsavedChangesIndicator')}><MenuIcon name="dot" size={10} /></span>
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
            {#each [...supportedProviders].sort((a, b) => llmProviderDisplayName(a.provider, $t).localeCompare(llmProviderDisplayName(b.provider, $t))) as provider (provider.provider)}
              <option value={provider.provider}>{llmProviderDisplayName(provider.provider, $t)}</option>
            {/each}
          </select>
        </div>

        <!-- Local endpoint discovery (issue #644). Deliberately OUTSIDE the provider block
             below: "Use this" SETS the provider, so a user who has not picked one yet is
             exactly who needs to see this first. It is a collapsed disclosure once a local
             provider is chosen, and absent for providers that never run locally. -->
        {#if showDiscovery}
          <LLMLocalDiscovery
            endpoints={localEndpoints}
            loading={loadingLocalEndpoints}
            privateAllowed={localEndpointsPrivateAllowed}
            disabled={saving}
            bind:expanded={discoveryExpanded}
            on:toggle={() => (discoveryTouched = true)}
            on:rescan={loadLocalEndpoints}
            on:use={(e) => useDiscoveredEndpoint(e.detail)}
          />
        {/if}

        {#if formData.provider}
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
              <input dir="ltr"
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
            <div class="form-label-row">
              <label for="model-name">{$t('llm.modelName')}</label>
              {#if formData.provider === 'ollama'}
                <button
                  type="button"
                  class="btn btn-primary btn-sm discover-models-btn"
                  on:click={loadOllamaModels}
                  disabled={loadingOllamaModels || saving || !formData.base_url}
                  title={$t('llm.discoverModelsTooltip')}
                >
                  {#if loadingOllamaModels}
                    <Spinner size="small" />
                  {:else}
                    <ActivateIcon size={12} />
                  {/if}
                  {$t('llm.discoverModels')}
                </button>
              {:else if formData.provider === 'openai' || formData.provider === 'vllm' || formData.provider === 'openrouter'}
                <button
                  type="button"
                  class="btn btn-primary btn-sm discover-models-btn"
                  on:click={loadOpenAICompatibleModels}
                  disabled={loadingOpenAIModels || saving || loadingApiKey || !formData.base_url || (getProviderDefaults(formData.provider)?.requires_api_key && !formData.api_key?.trim() && !editingConfig?.has_api_key)}
                  title={$t('llm.discoverModelsTooltip')}
                >
                  {#if loadingOpenAIModels}
                    <Spinner size="small" />
                  {:else}
                    <ActivateIcon size={12} />
                  {/if}
                  {$t('llm.discoverModels')}
                </button>
              {:else if formData.provider === 'anthropic' || formData.provider === 'claude'}
                <button
                  type="button"
                  class="btn btn-primary btn-sm discover-models-btn"
                  on:click={loadAnthropicModels}
                  disabled={loadingAnthropicModels || saving || loadingApiKey || (!formData.api_key?.trim() && !editingConfig?.has_api_key)}
                  title={$t('llm.discoverModelsTooltip')}
                >
                  {#if loadingAnthropicModels}
                    <Spinner size="small" />
                  {:else}
                    <ActivateIcon size={12} />
                  {/if}
                  {$t('llm.discoverModels')}
                </button>
              {/if}
            </div>
            <input
              type="text"
              id="model-name"
              bind:value={formData.model_name}
              disabled={saving}
              class="form-control"
              placeholder={getProviderDefaults(formData.provider)?.default_model || $t('llm.modelNamePlaceholder')}
              list={formData.provider === 'bedrock' ? 'bedrock-model-suggestions' : undefined}
              required
            />
            {#if formData.provider === 'bedrock'}
              <p class="field-desc">{$t('llm.bedrock.modelHelp')}</p>
            {/if}

            <!-- Ollama Model Selector -->
            {#if showModelSelector && ollamaModels.length > 0}
              <div class="model-selector">
                <h4>{$t('llm.availableModels')}</h4>
                <div class="model-list">
                  {#each ollamaModels as model (model.name)}
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
                  {#each openaiCompatibleModels as model (model.id)}
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
                  {#each anthropicModels as model (model.id)}
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

          {#if formData.provider === 'bedrock'}
            <LLMBedrockFields
              bind:region={formData.aws_region}
              bind:accessKeyId={formData.aws_access_key_id}
              bind:secretAccessKey={formData.aws_secret_access_key}
              bind:sessionToken={formData.aws_session_token}
              bind:clearCredentials={formData.clear_aws_credentials}
              hasSavedCredentials={Boolean(editingConfig?.has_aws_credentials)}
              regions={bedrockDefaults?.suggested_regions ?? []}
              models={bedrockDefaults?.suggested_models ?? []}
              {isAdmin}
              disabled={saving}
            />
          {/if}

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
                  <EyeIcon visible={showApiKey} />
                </button>
              </div>
              <input dir="ltr"
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
                  min="512"
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
              class="btn btn-primary btn-lg"
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
          <div class="toggle-label">
            <Toggle id="llmconfigmodal-toggle-1" checked={formData.is_shared} onchange={(v) => (formData.is_shared = v)} disabled={saving} />
            <label class="toggle-text" for="llmconfigmodal-toggle-1">{$t('settings.llmProvider.shareGlobally')}</label>
          </div>
        </div>

        <!-- Form Actions -->
        <div class="form-actions">
          <button type="button" class="btn btn-secondary btn-lg cancel-button" on:click={() => closeModal()} disabled={saving}>
            {$t('common.cancel')}
          </button>
          <button
            type="submit"
            class="btn btn-primary btn-lg save-button"
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
  variant="warning"
  on:confirm={handleUnsavedChangesConfirm}
  on:cancel={handleUnsavedChangesCancel}
  on:close={handleUnsavedChangesCancel}
/>

<style>
  .modal-title {
    margin: 0;
    font-size: var(--font-size-xl);
    font-weight: var(--font-weight-semibold);
    color: var(--text-color);
  }

  .field-desc {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
    margin: var(--space-1-5) 0 0 0;
    line-height: 1.5;
  }

  .unsaved-dot {
    display: inline-flex;
    align-items: center;
    color: var(--warning-color);
    flex-shrink: 0;
  }

  .config-form {
    padding: var(--space-6);
  }

  .form-group {
    margin-bottom: var(--space-6);
  }

  .form-group label {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: var(--space-2);
    font-weight: var(--font-weight-medium);
    color: var(--text-color);
  }

  .form-control {
    width: 100%;
  }

  .form-label-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: var(--space-2);
  }

  .form-label-row label {
    margin-bottom: 0;
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
    padding: var(--space-1);
    color: var(--text-secondary);
    display: flex;
    align-items: center;
    border-radius: var(--radius-sm);
    transition: background-color var(--duration-base), color var(--duration-base);
  }

  .toggle-password:hover {
    background-color: var(--background-color);
    color: var(--text-color);
  }

  .advanced-settings {
    margin: var(--space-6) 0;
    padding: var(--space-4);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-sm);
    background: var(--card-bg);
  }

  .advanced-settings h4 {
    margin: 0 0 var(--space-4);
    font-size: var(--font-size-base);
    font-weight: var(--font-weight-medium);
    color: var(--text-color);
  }

  .form-row {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
    gap: var(--space-4);
  }

  .model-selector {
    margin-top: var(--space-2);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-sm);
    background: var(--card-bg);
    padding: var(--space-4);
  }

  .model-selector h4 {
    margin: 0 0 var(--space-2);
    font-size: var(--font-size-md);
    font-weight: var(--font-weight-medium);
  }

  .model-list {
    max-height: 200px;
    overflow-y: auto;
    margin-bottom: var(--space-2);
  }

  .model-item {
    width: 100%;
    text-align: left;
    padding: var(--space-2);
    border: 1px solid var(--border-color);
    background: var(--input-bg);
    margin-bottom: var(--space-1);
    border-radius: var(--radius-sm);
    cursor: pointer;
    transition: all var(--duration-base) ease;
  }

  .model-item:hover {
    background: var(--hover-color);
    border-color: var(--primary-color);
  }

  .model-name {
    font-weight: var(--font-weight-medium);
    font-size: var(--font-size-md);
  }

  .model-details {
    font-size: var(--font-size-xs);
    color: var(--text-muted);
  }

  .close-selector {
    background: var(--secondary-color);
    color: var(--text-on-solid);
    border: none;
    padding: var(--space-2) var(--space-4);
    border-radius: var(--radius-sm);
    cursor: pointer;
    font-size: var(--font-size-md);
  }

  .test-section {
    margin: var(--space-6) 0;
    padding: var(--space-4);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-sm);
    background: var(--card-bg);
  }

  .share-toggle-row {
    display: flex;
    align-items: center;
    gap: var(--space-3);
    margin-top: var(--space-6);
    padding-top: var(--space-4);
    border-top: 1px dashed var(--border-color);
  }

  .toggle-label {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    cursor: pointer;
    font-size: var(--font-size-md);
    font-weight: var(--font-weight-regular);
    user-select: none;
  }

  .toggle-text {
    color: var(--text-color);
  }

  .form-actions {
    display: flex;
    justify-content: flex-end;
    gap: var(--space-4);
    margin-top: var(--space-8);
    padding-top: var(--space-4);
    border-top: 1px solid var(--border-color);
  }

  /* Tooltip styles */
  .info-tooltip {
    display: inline-flex;
    align-items: center;
    color: var(--text-muted);
    opacity: 0.6;
    cursor: help;
    transition: opacity var(--duration-base) ease;
    position: relative;
    margin-left: var(--space-2);
  }

  .info-tooltip:hover {
    opacity: 1;
    color: var(--primary-on-surface);
  }

  .info-tooltip[data-tooltip]:hover::after {
    content: attr(data-tooltip);
    position: fixed;
    left: var(--tooltip-left, 50%);
    top: var(--tooltip-top, 50%);
    background: #1a1a1a;
    color: var(--text-on-solid);
    padding: var(--space-2) var(--space-3);
    border-radius: var(--radius-md);
    font-size: var(--font-size-sm);
    font-weight: normal;
    max-width: 320px;
    white-space: normal;
    z-index: var(--z-toast);
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
    font-weight: var(--font-weight-medium);
    font-size: 0.9rem;
    color: var(--text-color);
    margin-bottom: var(--space-2);
  }

  .label-with-tooltip {
    display: flex;
    align-items: center;
    gap: var(--space-2);
  }

  @media (max-width: 768px) {
    .form-actions {
      flex-direction: column;
      gap: var(--space-3);
    }

    .cancel-button,
    .save-button {
      width: 100%;
      min-height: 44px;
      justify-content: center;
    }

    .form-control {
      min-height: 44px;
      font-size: var(--font-size-base);
    }
  }

</style>
