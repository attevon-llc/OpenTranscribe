<script lang="ts">
  import FormField from '$components/ui/FormField.svelte';
  import MenuIcon from '$components/ui/MenuIcon.svelte';
  import Toggle from '$components/ui/Toggle.svelte';
  import { createEventDispatcher } from 'svelte';
  import Spinner from '$components/ui/Spinner.svelte';
  import BaseModal from '$components/ui/BaseModal.svelte';
  import {
    ASRSettingsApi,
    type UserASRSettingsResponse,
    type UserASRSettingsCreate,
    type ASRConnectionTestRequest,
    type ASRProviderInfo,
    type ASRProvider,
  } from '$lib/api/asrSettings';
  import { toastStore } from '$stores/toast';
  import { t } from '$stores/locale';
  import { localWhisperDescription } from '$lib/i18n/backendLabels';
  import { getErrorMessage } from '$lib/utils/apiError';
  import type { DiarizationSource } from '$lib/api/transcriptionSettings';
  import { PIPELINE_ISSUE_KEYS } from '$lib/settings/pipelineKeys';
  import { modelCapability, optionWithFlags, providerCapability } from '$lib/settings/asrCapabilities';
  import { selectionNotices } from '$lib/settings/speakerSources';
  import { notAutoActivatedMessages } from '$lib/settings/asrOutcome';
  import { pipelineFailureKey } from '$lib/settings/pipelineIssues';
  import CapabilityBadges from './CapabilityBadges.svelte';
  import IssueRow from './IssueRow.svelte';

  export let show = false;
  export let editingConfig: UserASRSettingsResponse | null = null;
  export let providers: ASRProviderInfo[] = [];
  /** The user's speaker detection choice; decides which warnings the model earns. */
  export let diarizationSource: DiarizationSource | null = null;
  export let deploymentMode: 'full' | 'lite' = 'full';
  /** The user has no saved config yet, so a created one would be auto-activated (if runnable). */
  export let firstConfig = false;
  /** Create mode only: open with this provider chosen (the policy panel's "Add shared credentials"). */
  export let presetProvider: ASRProvider | '' = '';
  /** Create mode only: open with "share with all users" already on. */
  export let presetShared = false;

  const dispatch = createEventDispatcher();

  let saving = false;
  let testing = false;
  let testResult: { success: boolean; message: string } | null = null;
  let showApiKey = false;
  let isDirty: boolean;

  let formData = {
    name: '',
    provider: '' as ASRProvider | '',
    model_name: '',
    api_key: '',
    access_key_id: '',
    region: '',
    base_url: '',
    is_active: true,
    is_shared: false,
  };

  let originalFormData = { ...formData };
  let lastEditingConfigId: string | null = null;

  const AZURE_REGIONS = ['westus', 'eastus', 'eastus2', 'westeurope', 'northeurope', 'uksouth', 'australiaeast'];
  const AWS_REGIONS = ['us-east-1', 'us-west-2', 'eu-west-1', 'eu-central-1', 'ap-southeast-1', 'ap-northeast-1'];

  function syncEditingConfig(isShown: boolean, config: typeof editingConfig) {
    if (!isShown) {
      lastEditingConfigId = null;
      testResult = null;
    } else if (config && config.uuid !== lastEditingConfigId) {
      populateForm(config);
      lastEditingConfigId = config.uuid;
    } else if (!config && lastEditingConfigId !== null) {
      resetForm();
      lastEditingConfigId = null;
    }
  }
  $: syncEditingConfig(show, editingConfig);

  let presetApplied = false;
  function syncPreset(
    isShown: boolean,
    config: typeof editingConfig,
    provider: typeof presetProvider,
    shared: typeof presetShared
  ) {
    if (!isShown) {
      presetApplied = false;
    } else if (!config && !presetApplied && (provider || shared)) {
      presetApplied = true;
      applyPreset();
    }
  }
  $: syncPreset(show, editingConfig, presetProvider, presetShared);

  function applyPreset() {
    resetForm();
    formData.is_shared = presetShared;
    if (presetProvider) {
      formData.provider = presetProvider;
      onProviderChange();
    }
    originalFormData = { ...formData };
  }

  $: isDirty = JSON.stringify(formData) !== JSON.stringify(originalFormData);

  // Lite has no local transcription: that provider is not offered at all.
  $: selectableProviders = providers.filter((p) => !(deploymentMode === 'lite' && p.provider === 'local'));
  $: selectedProvider = providers.find(p => p.provider === formData.provider) || null;
  $: availableModels = selectedProvider?.models || [];

  $: selectedModel = availableModels.find(m => m.id === formData.model_name) || null;

  // What the chosen model returns (the provider's values until a model is picked), and what
  // that means for the user's current speaker detection choice.
  $: capability = formData.provider
    ? modelCapability(providers, formData.provider, formData.model_name)
    : null;
  $: modelNotices = capability
    ? selectionNotices(
        diarizationSource ?? 'off',
        {
          returnsSpeakers: capability.returnsSpeakers,
          timing: capability.timing,
          provider: formData.provider,
          model: formData.model_name
        },
        deploymentMode
      ).map((notice) => ({
        ...notice,
        messageKey: notice.code in PIPELINE_ISSUE_KEYS
          ? PIPELINE_ISSUE_KEYS[notice.code as keyof typeof PIPELINE_ISSUE_KEYS]
          : null
      }))
    : [];

  $: estimatedCostPerHour = selectedModel?.price_per_min_batch
    ? ASRSettingsApi.formatPricePerHour(selectedModel.price_per_min_batch)
    : null;

  $: isFormValid = !!(
    formData.name.trim() &&
    formData.provider &&
    formData.model_name.trim() &&
    (formData.provider === 'local' || !selectedProvider?.requires_api_key || formData.api_key.trim() || editingConfig?.has_api_key) &&
    (!selectedProvider?.requires_access_key_id || formData.access_key_id.trim() || editingConfig?.has_access_key_id)
  );

  $: needsRegion = formData.provider === 'azure' || formData.provider === 'aws';
  $: regionOptions = formData.provider === 'azure' ? AZURE_REGIONS : formData.provider === 'aws' ? AWS_REGIONS : [];

  function populateForm(config: UserASRSettingsResponse) {
    formData = {
      name: config.name,
      provider: config.provider,
      model_name: config.model_name,
      api_key: '',
      access_key_id: '',
      region: config.region || '',
      base_url: config.base_url || '',
      is_active: config.is_active,
      is_shared: config.is_shared || false,
    };
    originalFormData = { ...formData };
    testResult = null;
  }

  function resetForm() {
    formData = {
      name: '',
      provider: '' as ASRProvider | '',
      model_name: '',
      api_key: '',
      access_key_id: '',
      region: '',
      base_url: '',
      is_active: true,
      is_shared: false,
    };
    originalFormData = { ...formData };
    testResult = null;
    showApiKey = false;
  }

  function onProviderChange() {
    formData.model_name = '';
    formData.region = '';
    testResult = null;
    // Set default name if not set
    if (!formData.name && formData.provider) {
      formData.name = ASRSettingsApi.getProviderDisplayName(formData.provider);
    }
  }

  function onModelChange() {
    testResult = null;
  }

  async function handleSave() {
    if (!isFormValid || !formData.provider) return;
    saving = true;
    try {
      const payload: UserASRSettingsCreate = {
        name: formData.name.trim(),
        provider: formData.provider,
        model_name: formData.model_name.trim(),
        is_active: formData.is_active,
        is_shared: formData.is_shared,
      };
      if (formData.api_key.trim()) payload.api_key = formData.api_key.trim();
      if (formData.access_key_id.trim()) payload.access_key_id = formData.access_key_id.trim();
      if (formData.region.trim()) payload.region = formData.region.trim();
      if (formData.base_url.trim()) payload.base_url = formData.base_url.trim();

      let result: UserASRSettingsResponse;
      if (editingConfig) {
        result = await ASRSettingsApi.updateConfig(editingConfig.uuid, payload);
        toastStore.success(`"${result.name}" updated`);
      } else {
        const created = await ASRSettingsApi.createConfig(payload);
        result = created;
        toastStore.success(`"${created.name}" created`);
        // A first config is made active only when speaker detection keeps working with it.
        for (const m of notAutoActivatedMessages(created, firstConfig)) {
          const text = m.key ? $t(m.key) : (m.fallbackText ?? '');
          if (m.severity === 'error') toastStore.error(text, 8000);
          else toastStore.warning(text, 8000);
        }
      }
      dispatch('saved', result);
      handleClose();
    } catch (err: unknown) {
      const refusal = pipelineFailureKey(err, 'settings.asrProvider.saveFailed');
      toastStore.error(
        refusal ? $t(refusal) : getErrorMessage(err, $t('settings.asrProvider.saveFailed')),
        5000
      );
    } finally {
      saving = false;
    }
  }

  async function handleTest() {
    if (!formData.provider || !formData.model_name) return;
    testing = true;
    testResult = null;
    try {
      const params: ASRConnectionTestRequest = {
        provider: formData.provider,
        model_name: formData.model_name,
      };
      if (formData.api_key.trim()) params.api_key = formData.api_key.trim();
      else if (editingConfig?.has_api_key) params.config_id = editingConfig.uuid;
      if (formData.access_key_id.trim()) params.access_key_id = formData.access_key_id.trim();
      if (formData.region.trim()) params.region = formData.region.trim();
      if (formData.base_url.trim()) params.base_url = formData.base_url.trim();

      const result = await ASRSettingsApi.testConnection(params);
      testResult = { success: result.success, message: result.message };
    } catch (err: unknown) {
      testResult = { success: false, message: getErrorMessage(err, $t('settings.asrProvider.testFailed')) };
    } finally {
      testing = false;
    }
  }

  function handleClose() {
    show = false;
    dispatch('close');
    resetForm();
  }

</script>

<BaseModal isOpen={show} onClose={handleClose} size="sm">
  <svelte:fragment slot="header">
    <h2 class="modal-title">{editingConfig ? $t('settings.asrProvider.editConfig') : $t('settings.asrProvider.addConfig')}</h2>
    {#if isDirty}
      <span class="unsaved-dot" role="img" aria-label={$t('settings.asrProvider.unsavedChanges')} title={$t('settings.asrProvider.unsavedChanges')}><MenuIcon name="dot" size={10} /></span>
    {/if}
  </svelte:fragment>
        <!-- Name -->
        <FormField id="asr-name" label={$t('settings.asrProvider.fields.name')}>
          <input
            id="asr-name"
            type="text"
            bind:value={formData.name}
            placeholder="e.g., Deepgram Production"
            class="form-input"
          />
        </FormField>

        <!-- Provider -->
        <FormField id="asr-provider" label={$t('settings.asrProvider.fields.provider')}>
          <select
            id="asr-provider"
            bind:value={formData.provider}
            on:change={onProviderChange}
            class="form-select"
            disabled={!!editingConfig}
            title={editingConfig ? $t('settings.asrProvider.providerLockedOnEdit') : undefined}
          >
            <option value="">{$t('settings.asrProvider.selectProvider')}</option>
            {#each selectableProviders as p (p.provider)}
              <option value={p.provider}>
                {optionWithFlags(
                  ASRSettingsApi.getProviderDisplayName(p.provider),
                  providerCapability(providers, p.provider),
                  $t
                )}
                {#if p.status === 'experimental'} ({$t('settings.asrProvider.experimentalShort')}){/if}
                {#if !p.sdk_available} ({$t('settings.asrProvider.missingSDK')}){/if}
              </option>
            {/each}
          </select>
          {#if editingConfig}
            <p class="field-hint">{$t('settings.asrProvider.providerLockedOnEdit')}</p>
          {/if}
        </FormField>

        {#if selectedProvider}
          <!-- Provider capabilities -->
          <div class="capabilities">
            {#if selectedProvider.supports_vocabulary}
              <span class="cap-badge">{$t('settings.asrProvider.capVocabulary')}</span>
            {/if}
            {#if selectedProvider.supports_translation}
              <span class="cap-badge">{$t('settings.asrProvider.capTranslation')}</span>
            {/if}
          </div>

          {#if selectedProvider.status === 'experimental'}
            <div class="experimental-warning">
              <span class="warning-icon" aria-hidden="true"><MenuIcon name="warning" size="md" /></span>
              <span>{selectedProvider.status_note || $t('settings.asrProvider.experimentalWarning')}</span>
            </div>
          {/if}

          {#if selectedProvider.diarization_quality}
            <p class="diarization-quality-note">
              <strong>{$t('settings.asrProvider.diarizationQuality')}:</strong> {selectedProvider.diarization_quality}
            </p>
          {/if}

          <!-- Model -->
          <FormField id="asr-model" label={$t('settings.asrProvider.fields.model')}>
            {#if availableModels.length > 0}
              <select id="asr-model" bind:value={formData.model_name} on:change={onModelChange} class="form-select">
                <option value="">{$t('settings.asrProvider.selectModel')}</option>
                {#each availableModels as m (m.id)}
                  <option value={m.id}>
                    {optionWithFlags(m.name, modelCapability(providers, formData.provider, m.id), $t)}{m.price_per_min_batch ? ` — $${m.price_per_min_batch}/min` : ''}{localWhisperDescription($t, m) ? ` — ${localWhisperDescription($t, m)}` : ''}
                  </option>
                {/each}
              </select>
            {:else}
              <input dir="ltr"
                id="asr-model"
                type="text"
                bind:value={formData.model_name}
                placeholder={$t('settings.asrProvider.modelNamePlaceholder')}
                class="form-input"
              />
            {/if}
            {#if capability}
              <div class="model-capabilities" data-testid="model-capabilities">
                <CapabilityBadges
                  returnsSpeakers={capability.returnsSpeakers}
                  timing={capability.timing}
                  local={formData.provider === 'local'}
                  lite={deploymentMode === 'lite'}
                  explain
                />
              </div>
              {#if modelNotices.length > 0}
                <ul class="model-notices" data-testid="model-notices">
                  {#each modelNotices as notice (notice.code)}
                    <IssueRow
                      code={notice.code}
                      severity={notice.severity}
                      messageKey={notice.messageKey}
                      rowTestId="model-notice"
                    />
                  {/each}
                </ul>
              {/if}
            {/if}
            {#if estimatedCostPerHour}
              <p class="cost-hint">{$t('settings.asrProvider.estimatedCost')}: <strong>{estimatedCostPerHour}</strong></p>
            {/if}
          </FormField>

          <!-- Access Key ID (AWS) -->
          {#if selectedProvider.requires_access_key_id}
            <FormField id="asr-access-key-id" label={$t('settings.asrProvider.fields.accessKeyId')}>
              <input dir="ltr"
                id="asr-access-key-id"
                type="text"
                bind:value={formData.access_key_id}
                placeholder={editingConfig?.has_access_key_id ? '(stored — leave blank to keep)' : 'Enter Access Key ID'}
                class="form-input"
                autocomplete="off"
              />
            </FormField>
          {/if}

          <!-- API Key / Secret Access Key -->
          {#if selectedProvider.requires_api_key}
            <FormField id="asr-apikey" label={selectedProvider.requires_access_key_id ? $t('settings.asrProvider.fields.secretAccessKey') : $t('settings.asrProvider.fields.apiKey')}>
              <div class="input-with-toggle">
                <input dir="ltr"
                  id="asr-apikey"
                  type={showApiKey ? 'text' : 'password'}
                  bind:value={formData.api_key}
                  placeholder={editingConfig?.has_api_key ? '(stored — leave blank to keep)' : 'Enter API key'}
                  class="form-input"
                  autocomplete="off"
                />
                <button type="button" class="toggle-visibility" on:click={() => showApiKey = !showApiKey}>
                  {showApiKey ? 'Hide' : 'Show'}
                </button>
              </div>
            </FormField>
          {/if}

          <!-- Region (Azure / AWS) -->
          {#if needsRegion}
            <FormField id="asr-region" label={$t('settings.asrProvider.fields.region')}>
              <select id="asr-region" bind:value={formData.region} class="form-select">
                <option value="">{$t('settings.asrProvider.selectRegion')}</option>
                {#each regionOptions as r (r)}
                  <option value={r}>{r}</option>
                {/each}
              </select>
            </FormField>
          {/if}

          <!-- Base URL -->
          {#if selectedProvider.supports_base_url}
            <FormField id="asr-baseurl" label={$t('settings.asrProvider.fields.baseUrl')}>
              <input dir="ltr"
                id="asr-baseurl"
                type="url"
                bind:value={formData.base_url}
                placeholder="https://..."
                class="form-input"
              />
            </FormField>
          {/if}
        {/if}

        <!-- Test result — aria-live so screen readers announce it after test completes -->
        <div role="status" aria-live="polite" aria-atomic="true">
          {#if testResult}
            <div class="test-result" class:success={testResult.success} class:failure={!testResult.success}>
              <MenuIcon name={testResult.success ? 'status-success' : 'status-error'} size="md" />
              <span>{testResult.message}</span>
            </div>
          {/if}
        </div>

        <!-- Pricing disclaimer -->
        {#if estimatedCostPerHour}
          <p class="pricing-disclaimer">{$t('settings.asrProvider.pricingDisclaimer')}</p>
        {/if}

        <!-- Share with all users -->
        <div class="share-toggle-row">
          <Toggle
            checked={formData.is_shared}
            onchange={(next) => (formData.is_shared = next)}
            disabled={saving}
            id="asr-share-globally-label"
          />
          <label class="toggle-text" for="asr-share-globally-label">{$t('settings.asrProvider.shareGlobally')}</label>
        </div>

  <svelte:fragment slot="footer">
    <div class="footer-row">
        <button
          class="btn btn-primary btn-test-conn"
          on:click={handleTest}
          disabled={testing || !formData.provider || !formData.model_name}
        >
          {#if testing}
            <Spinner size="small" /> {$t('common.testing')}
          {:else}
            {$t('settings.asrProvider.testConnection')}
          {/if}
        </button>
        <div class="footer-actions">
          <button class="btn btn-secondary btn-cancel" on:click={handleClose}>{$t('common.cancel')}</button>
          <button class="btn btn-primary btn-save" on:click={handleSave} disabled={saving || !isFormValid}>
            {saving ? 'Saving...' : (editingConfig ? 'Update' : 'Save')}
          </button>
        </div>
    </div>
  </svelte:fragment>
</BaseModal>

<style>
  .model-capabilities {
    margin-top: var(--space-2);
  }

  .model-notices {
    list-style: none;
    margin: var(--space-2) 0 var(--space-4);
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
  }

  .modal-title {
    margin: 0;
    font-size: var(--font-size-xl);
    font-weight: var(--font-weight-semibold);
    color: var(--text-color);
  }

  .unsaved-dot {
    display: inline-flex;
    align-items: center;
    color: var(--warning-text);
    flex-shrink: 0;
  }

  .form-input, .form-select {
    padding: var(--space-2) var(--space-3);
    font-size: var(--font-size-sm);
  }

  .form-select {
    padding-inline-end: var(--space-10);
    cursor: pointer;
  }

  .input-with-toggle {
    display: flex;
    gap: var(--space-2);
  }

  .input-with-toggle .form-input {
    flex: 1;
  }

  .toggle-visibility {
    padding: var(--space-2) var(--space-3);
    background: var(--card-bg);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-md);
    cursor: pointer;
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
    white-space: nowrap;
  }

  .toggle-visibility:hover { color: var(--text-color); }

  .capabilities {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-1-5);
  }

  .cap-badge {
    padding: var(--space-1) var(--space-2);
    background: rgba(var(--accent-rgb), 0.1);
    color: var(--primary-on-surface);
    border-radius: var(--radius-sm);
    font-size: var(--font-size-xs);
    font-weight: var(--font-weight-medium);
  }

  .experimental-warning {
    display: flex;
    align-items: flex-start;
    gap: var(--space-2);
    padding: var(--space-2-5) var(--space-3);
    margin: var(--space-2) 0;
    background: rgba(var(--warning-color-rgb), 0.1);
    border: 1px solid rgba(var(--warning-color-rgb), 0.3);
    border-radius: var(--radius-md);
    font-size: var(--font-size-sm);
    color: var(--warning-text);
    line-height: 1.4;
  }

  .experimental-warning .warning-icon {
    display: inline-flex;
    flex-shrink: 0;
  }

  .diarization-quality-note {
    margin: var(--space-1-5) 0;
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
    line-height: 1.4;
  }

  .cost-hint {
    margin: var(--space-1) 0 0;
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
  }

  .field-hint {
    margin: var(--space-1) 0 0;
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
    font-style: italic;
  }

  .pricing-disclaimer {
    margin: 0;
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
    font-style: italic;
  }

  .test-result {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    padding: var(--space-2-5) var(--space-3-5);
    border-radius: var(--radius-md);
    font-size: var(--font-size-sm);
  }

  .test-result.success {
    background: rgba(var(--success-color-rgb), 0.1);
    color: var(--success-text);
    border: 1px solid rgba(var(--success-color-rgb), 0.2);
  }

  .test-result.failure {
    background: rgba(var(--error-color-rgb), 0.1);
    color: var(--error-text);
    border: 1px solid rgba(var(--error-color-rgb), 0.2);
  }

  .share-toggle-row {
    display: flex;
    align-items: center;
    gap: var(--space-3);
    margin-top: var(--space-4);
    padding-top: var(--space-3);
    border-top: 1px dashed var(--border-color);
  }

  .toggle-text {
    cursor: pointer;
    color: var(--text-color);
    font-size: var(--font-size-sm);
    user-select: none;
  }

  .footer-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    width: 100%;
    gap: var(--space-3);
  }

  .footer-actions {
    display: flex;
    gap: var(--space-3);
  }

  button:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  @media (max-width: 768px) {
    .footer-row {
      flex-direction: column;
      gap: var(--space-3);
    }

    .footer-actions {
      width: 100%;
      flex-direction: column;
    }

    .btn-test-conn,
    .btn-cancel,
    .btn-save {
      width: 100%;
      min-height: 44px;
      justify-content: center;
    }

    :global(.form-field) input,
    :global(.form-field) select {
      min-height: 44px;
      font-size: var(--font-size-base);
    }
  }

</style>
