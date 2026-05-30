<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import Spinner from '../ui/Spinner.svelte';
  import BaseModal from '../ui/BaseModal.svelte';
  import {
    ASRSettingsApi,
    type UserASRSettingsResponse,
    type ASRProviderInfo,
    type ASRModelInfo,
    type ASRProvider,
  } from '../../lib/api/asrSettings';
  import { toastStore } from '../../stores/toast';
  import { t } from '$stores/locale';

  export let show = false;
  export let editingConfig: UserASRSettingsResponse | null = null;
  export let providers: ASRProviderInfo[] = [];

  const dispatch = createEventDispatcher();

  let saving = false;
  let testing = false;
  let testResult: { success: boolean; message: string } | null = null;
  let showApiKey = false;
  let isDirty = false;

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

  $: if (show) {
    if (editingConfig && editingConfig.uuid !== lastEditingConfigId) {
      populateForm(editingConfig);
      lastEditingConfigId = editingConfig.uuid;
    } else if (!editingConfig && lastEditingConfigId !== null) {
      resetForm();
      lastEditingConfigId = null;
    }
  } else {
    lastEditingConfigId = null;
    testResult = null;
  }

  $: isDirty = JSON.stringify(formData) !== JSON.stringify(originalFormData);

  $: selectedProvider = providers.find(p => p.provider === formData.provider) || null;
  $: availableModels = selectedProvider?.models || [];

  $: selectedModel = availableModels.find(m => m.id === formData.model_name) || null;

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
    if (!isFormValid) return;
    saving = true;
    try {
      const payload: any = {
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
        result = await ASRSettingsApi.createConfig(payload);
        toastStore.success(`"${result.name}" created`);
      }
      dispatch('saved', result);
      handleClose();
    } catch (err: any) {
      const detail = err.response?.data?.detail;
      toastStore.error(typeof detail === 'string' ? detail : 'Failed to save ASR configuration', 5000);
    } finally {
      saving = false;
    }
  }

  async function handleTest() {
    if (!formData.provider || !formData.model_name) return;
    testing = true;
    testResult = null;
    try {
      const params: any = {
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
    } catch (err: any) {
      const detail = err.response?.data?.detail;
      testResult = { success: false, message: typeof detail === 'string' ? detail : 'Connection test failed' };
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

<BaseModal isOpen={show} onClose={handleClose} maxWidth="520px">
  <svelte:fragment slot="header">
    <h2 class="modal-title">{editingConfig ? $t('settings.asrProvider.editConfig') : $t('settings.asrProvider.addConfig')}</h2>
    {#if isDirty}
      <span class="unsaved-dot" title={$t('settings.asrProvider.unsavedChanges')}>●</span>
    {/if}
  </svelte:fragment>
        <!-- Name -->
        <div class="form-group">
          <label for="asr-name">{$t('settings.asrProvider.fields.name')}</label>
          <input
            id="asr-name"
            type="text"
            bind:value={formData.name}
            placeholder="e.g., Deepgram Production"
            class="form-input"
          />
        </div>

        <!-- Provider -->
        <div class="form-group">
          <label for="asr-provider">{$t('settings.asrProvider.fields.provider')}</label>
          <select
            id="asr-provider"
            bind:value={formData.provider}
            on:change={onProviderChange}
            class="form-select"
            disabled={!!editingConfig}
            title={editingConfig ? $t('settings.asrProvider.providerLockedOnEdit') : undefined}
          >
            <option value="">Select provider...</option>
            {#each providers as p}
              <option value={p.provider}>
                {ASRSettingsApi.getProviderDisplayName(p.provider)}
                {#if !p.sdk_available} ({$t('settings.asrProvider.missingSDK')}){/if}
              </option>
            {/each}
          </select>
          {#if editingConfig}
            <p class="field-hint">{$t('settings.asrProvider.providerLockedOnEdit')}</p>
          {/if}
        </div>

        {#if selectedProvider}
          <!-- Provider capabilities -->
          <div class="capabilities">
            {#if selectedProvider.supports_diarization}
              <span class="cap-badge">Diarization</span>
            {/if}
            {#if selectedProvider.supports_vocabulary}
              <span class="cap-badge">Vocabulary Boosting</span>
            {/if}
            {#if selectedProvider.supports_translation}
              <span class="cap-badge">Translation</span>
            {/if}
          </div>

          {#if selectedProvider.status === 'experimental'}
            <div class="experimental-warning">
              <span class="warning-icon">&#9888;</span>
              <span>{selectedProvider.status_note || $t('settings.asrProvider.experimentalWarning')}</span>
            </div>
          {/if}

          {#if selectedProvider.diarization_quality}
            <p class="diarization-quality-note">
              <strong>{$t('settings.asrProvider.diarizationQuality')}:</strong> {selectedProvider.diarization_quality}
            </p>
          {/if}

          <!-- Model -->
          <div class="form-group">
            <label for="asr-model">{$t('settings.asrProvider.fields.model')}</label>
            {#if availableModels.length > 0}
              <select id="asr-model" bind:value={formData.model_name} on:change={onModelChange} class="form-select">
                <option value="">Select model...</option>
                {#each availableModels as m}
                  <option value={m.id}>
                    {m.name}{m.price_per_min_batch ? ` — $${m.price_per_min_batch}/min` : ''}{m.description ? ` — ${m.description}` : ''}
                  </option>
                {/each}
              </select>
            {:else}
              <input
                id="asr-model"
                type="text"
                bind:value={formData.model_name}
                placeholder="Model name"
                class="form-input"
              />
            {/if}
            {#if estimatedCostPerHour}
              <p class="cost-hint">{$t('settings.asrProvider.estimatedCost')}: <strong>{estimatedCostPerHour}</strong></p>
            {/if}
          </div>

          <!-- Access Key ID (AWS) -->
          {#if selectedProvider.requires_access_key_id}
            <div class="form-group">
              <label for="asr-access-key-id">{$t('settings.asrProvider.fields.accessKeyId')}</label>
              <input
                id="asr-access-key-id"
                type="text"
                bind:value={formData.access_key_id}
                placeholder={editingConfig?.has_access_key_id ? '(stored — leave blank to keep)' : 'Enter Access Key ID'}
                class="form-input"
                autocomplete="off"
              />
            </div>
          {/if}

          <!-- API Key / Secret Access Key -->
          {#if selectedProvider.requires_api_key}
            <div class="form-group">
              <label for="asr-apikey">{selectedProvider.requires_access_key_id ? $t('settings.asrProvider.fields.secretAccessKey') : $t('settings.asrProvider.fields.apiKey')}</label>
              <div class="input-with-toggle">
                <input
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
            </div>
          {/if}

          <!-- Region (Azure / AWS) -->
          {#if needsRegion}
            <div class="form-group">
              <label for="asr-region">{$t('settings.asrProvider.fields.region')}</label>
              <select id="asr-region" bind:value={formData.region} class="form-select">
                <option value="">Select region...</option>
                {#each regionOptions as r}
                  <option value={r}>{r}</option>
                {/each}
              </select>
            </div>
          {/if}

          <!-- Base URL -->
          {#if selectedProvider.supports_base_url}
            <div class="form-group">
              <label for="asr-baseurl">{$t('settings.asrProvider.fields.baseUrl')}</label>
              <input
                id="asr-baseurl"
                type="url"
                bind:value={formData.base_url}
                placeholder="https://..."
                class="form-input"
              />
            </div>
          {/if}
        {/if}

        <!-- Test result — aria-live so screen readers announce it after test completes -->
        <div role="status" aria-live="polite" aria-atomic="true">
          {#if testResult}
            <div class="test-result" class:success={testResult.success} class:failure={!testResult.success}>
              {testResult.success ? '✓' : '✗'} {testResult.message}
            </div>
          {/if}
        </div>

        <!-- Pricing disclaimer -->
        {#if estimatedCostPerHour}
          <p class="pricing-disclaimer">{$t('settings.asrProvider.pricingDisclaimer')}</p>
        {/if}

        <!-- Share with all users -->
        <div class="share-toggle-row">
          <label class="toggle-label">
            <input type="checkbox" class="toggle-input" bind:checked={formData.is_shared} disabled={saving} />
            <span class="toggle-switch"></span>
            <span class="toggle-text">{$t('settings.asrProvider.shareGlobally')}</span>
          </label>
        </div>

  <svelte:fragment slot="footer">
    <div class="footer-row">
        <button
          class="btn-test-conn"
          on:click={handleTest}
          disabled={testing || !formData.provider || !formData.model_name}
        >
          {#if testing}
            <Spinner size="small" /> Testing...
          {:else}
            {$t('settings.asrProvider.testConnection')}
          {/if}
        </button>
        <div class="footer-actions">
          <button class="btn-cancel" on:click={handleClose}>{$t('common.cancel')}</button>
          <button class="btn-save" on:click={handleSave} disabled={saving || !isFormValid}>
            {saving ? 'Saving...' : (editingConfig ? 'Update' : 'Save')}
          </button>
        </div>
    </div>
  </svelte:fragment>
</BaseModal>

<style>
  .modal-title {
    margin: 0;
    font-size: 1.25rem;
    font-weight: 600;
    color: var(--text-color);
  }

  .unsaved-dot {
    color: var(--warning-color);
    font-size: 0.9rem;
    line-height: 1;
    flex-shrink: 0;
  }

  .form-group {
    display: flex;
    flex-direction: column;
    gap: 0.375rem;
  }

  .form-group label {
    font-size: 0.8125rem;
    font-weight: 500;
    color: var(--text-color);
  }

  .form-input, .form-select {
    padding: 0.5rem 0.75rem;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    background: var(--input-background);
    color: var(--text-color);
    font-size: 0.8125rem;
    outline: none;
    transition: border-color 0.15s;
  }

  .form-select {
    appearance: none;
    -webkit-appearance: none;
    padding-right: 2.5rem;
    cursor: pointer;
  }

  .form-input:focus, .form-select:focus {
    border-color: var(--primary-color);
  }

  .input-with-toggle {
    display: flex;
    gap: 0.5rem;
  }

  .input-with-toggle .form-input {
    flex: 1;
  }

  .toggle-visibility {
    padding: 0.5rem 0.75rem;
    background: var(--card-bg);
    border: 1px solid var(--border-color);
    border-radius: 6px;
    cursor: pointer;
    font-size: 0.75rem;
    color: var(--text-muted);
    white-space: nowrap;
  }

  .toggle-visibility:hover { color: var(--text-color); }

  .capabilities {
    display: flex;
    flex-wrap: wrap;
    gap: 0.375rem;
  }

  .cap-badge {
    padding: 0.2rem 0.5rem;
    background: rgba(59, 130, 246, 0.1);
    color: var(--primary-color);
    border-radius: 4px;
    font-size: 0.7rem;
    font-weight: 500;
  }

  .experimental-warning {
    display: flex;
    align-items: flex-start;
    gap: 0.5rem;
    padding: 0.6rem 0.8rem;
    margin: 0.5rem 0;
    background: rgba(245, 158, 11, 0.1);
    border: 1px solid rgba(245, 158, 11, 0.3);
    border-radius: 6px;
    font-size: 0.8rem;
    color: var(--warning-color, #d97706);
    line-height: 1.4;
  }

  .experimental-warning .warning-icon {
    font-size: 1rem;
    flex-shrink: 0;
  }

  .diarization-quality-note {
    margin: 0.4rem 0;
    font-size: 0.8rem;
    color: var(--text-secondary);
    line-height: 1.4;
  }

  .cost-hint {
    margin: 0.25rem 0 0;
    font-size: 0.775rem;
    color: var(--text-muted);
  }

  .field-hint {
    margin: 0.25rem 0 0;
    font-size: 0.75rem;
    color: var(--text-muted);
    font-style: italic;
  }

  .pricing-disclaimer {
    margin: 0;
    font-size: 0.75rem;
    color: var(--text-muted);
    font-style: italic;
  }

  .test-result {
    padding: 0.625rem 0.875rem;
    border-radius: 6px;
    font-size: 0.8125rem;
  }

  .test-result.success {
    background: rgba(16, 185, 129, 0.1);
    color: #10b981;
    border: 1px solid rgba(16, 185, 129, 0.2);
  }

  .test-result.failure {
    background: rgba(239, 68, 68, 0.1);
    color: #ef4444;
    border: 1px solid rgba(239, 68, 68, 0.2);
  }

  .share-toggle-row {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    margin-top: 1rem;
    padding-top: 0.75rem;
    border-top: 1px dashed var(--border-color);
  }

  .toggle-label {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    cursor: pointer;
    font-size: 0.8125rem;
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

  .footer-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    width: 100%;
    gap: 0.75rem;
  }

  .footer-actions {
    display: flex;
    gap: 0.75rem;
  }

  .btn-test-conn {
    display: inline-flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.5rem 0.875rem;
    border: none;
    color: white;
    background: #3b82f6;
    border-radius: 6px;
    cursor: pointer;
    font-size: 0.8125rem;
    font-weight: 500;
    transition: all 0.2s ease;
    box-shadow: 0 2px 4px rgba(var(--primary-color-rgb), 0.2);
  }

  .btn-test-conn:hover:not(:disabled) {
    background: #2563eb;
    transform: scale(1.02);
    box-shadow: 0 4px 8px rgba(var(--primary-color-rgb), 0.25);
  }

  .btn-test-conn:active:not(:disabled) {
    transform: scale(1);
  }

  .btn-cancel {
    padding: 0.5rem 1rem;
    border: 1px solid var(--border-color);
    background-color: var(--surface-color);
    color: var(--text-color);
    border-radius: 6px;
    cursor: pointer;
    font-size: 0.8125rem;
    transition: all 0.15s;
    box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1);
  }

  .btn-cancel:hover:not(:disabled) {
    background-color: var(--button-hover);
    border-color: var(--border-color);
    transform: scale(1.02);
    box-shadow: 0 4px 8px rgba(0, 0, 0, 0.15);
  }

  .btn-cancel:active:not(:disabled) {
    transform: scale(1);
  }

  .btn-save {
    padding: 0.5rem 1.25rem;
    background: #3b82f6;
    color: white;
    border: none;
    border-radius: 6px;
    cursor: pointer;
    font-size: 0.8125rem;
    font-weight: 500;
    transition: background 0.15s;
    box-shadow: 0 2px 4px rgba(var(--primary-color-rgb), 0.2);
  }

  .btn-save:hover:not(:disabled) { background: #2563eb; }

  button:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  @media (max-width: 768px) {
    .footer-row {
      flex-direction: column;
      gap: 0.75rem;
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

    .form-group input,
    .form-group select {
      min-height: 44px;
      font-size: 1rem;
    }
  }

</style>
