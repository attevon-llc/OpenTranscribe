<script lang="ts">
  import { onMount, createEventDispatcher } from 'svelte';
  import Spinner from '../ui/Spinner.svelte';
  import axiosInstance from '$lib/axios';
  import { toastStore } from '$stores/toast';
  import { t } from '$stores/locale';
  import { getErrorMessage } from '$lib/utils/apiError';

  type SettingSource = 'db' | 'env' | 'default';

  interface EngineSettingValue<T> {
    value: T;
    source: SettingSource;
  }

  interface EngineSettingsResponse {
    diarizer_backend: EngineSettingValue<string>;
    diarizer_require_sidecar: EngineSettingValue<boolean>;
    boundary_smoothing_enabled: EngineSettingValue<boolean>;
    boundary_acoustic_recheck_enabled: EngineSettingValue<boolean>;
    boundary_acoustic_cosine_margin: EngineSettingValue<number>;
    boundary_acoustic_max_word_dur: EngineSettingValue<number>;
  }

  type EngineSettingKey = keyof EngineSettingsResponse;

  const dispatch = createEventDispatcher<{ change: { hasChanges: boolean } }>();

  let loading = false;
  let saving = false;
  let resetInProgress: EngineSettingKey | null = null;

  // Current values from server
  let settings: EngineSettingsResponse | null = null;

  // Draft values (bound to form controls)
  let draftDiarizerBackend = 'native';
  let draftDiarizerRequireSidecar = false;
  let draftBoundarySmoothing = false;
  let draftAcousticRecheck = false;
  let draftAcousticCosineMargin = 0.05;
  let draftAcousticMaxWordDur = 1.0;

  onMount(async () => {
    await loadData();
  });

  async function loadData() {
    loading = true;
    try {
      const res = await axiosInstance.get<EngineSettingsResponse>('/admin/engine-settings');
      settings = res.data;
      draftDiarizerBackend = settings.diarizer_backend.value;
      draftDiarizerRequireSidecar = settings.diarizer_require_sidecar.value;
      draftBoundarySmoothing = settings.boundary_smoothing_enabled.value;
      draftAcousticRecheck = settings.boundary_acoustic_recheck_enabled.value;
      draftAcousticCosineMargin = settings.boundary_acoustic_cosine_margin.value;
      draftAcousticMaxWordDur = settings.boundary_acoustic_max_word_dur.value;
    } catch (err: unknown) {
      toastStore.error(getErrorMessage(err, $t('settings.engineSettings.loadFailed')), 5000);
    } finally {
      loading = false;
    }
  }

  async function save() {
    if (!settings) return;
    saving = true;

    // Only send keys where the draft differs from the current server value
    const payload: Partial<{
      diarizer_backend: string;
      diarizer_require_sidecar: boolean;
      boundary_smoothing_enabled: boolean;
      boundary_acoustic_recheck_enabled: boolean;
      boundary_acoustic_cosine_margin: number;
      boundary_acoustic_max_word_dur: number;
    }> = {};

    if (draftDiarizerBackend !== settings.diarizer_backend.value) {
      payload.diarizer_backend = draftDiarizerBackend;
    }
    if (draftDiarizerRequireSidecar !== settings.diarizer_require_sidecar.value) {
      payload.diarizer_require_sidecar = draftDiarizerRequireSidecar;
    }
    if (draftBoundarySmoothing !== settings.boundary_smoothing_enabled.value) {
      payload.boundary_smoothing_enabled = draftBoundarySmoothing;
    }
    if (draftAcousticRecheck !== settings.boundary_acoustic_recheck_enabled.value) {
      payload.boundary_acoustic_recheck_enabled = draftAcousticRecheck;
    }
    if (Number(draftAcousticCosineMargin) !== settings.boundary_acoustic_cosine_margin.value) {
      payload.boundary_acoustic_cosine_margin = Number(draftAcousticCosineMargin);
    }
    if (Number(draftAcousticMaxWordDur) !== settings.boundary_acoustic_max_word_dur.value) {
      payload.boundary_acoustic_max_word_dur = Number(draftAcousticMaxWordDur);
    }

    if (Object.keys(payload).length === 0) {
      toastStore.info($t('settings.engineSettings.saved'));
      saving = false;
      return;
    }

    try {
      await axiosInstance.post('/admin/engine-settings/update', payload);
      toastStore.success($t('settings.engineSettings.saved'));
      await loadData();
    } catch (err: unknown) {
      toastStore.error(getErrorMessage(err, $t('settings.engineSettings.saveFailed')), 5000);
    } finally {
      saving = false;
    }
  }

  async function resetKey(key: EngineSettingKey) {
    resetInProgress = key;
    try {
      await axiosInstance.delete(`/admin/engine-settings/${key}`);
      toastStore.success($t('settings.engineSettings.resetToDefault', { key }));
      await loadData();
    } catch (err: unknown) {
      toastStore.error(getErrorMessage(err, $t('settings.engineSettings.resetFailed', { key })), 5000);
    } finally {
      resetInProgress = null;
    }
  }

  function sourceLabel(source: SettingSource): string {
    if (source === 'db') return $t('settings.engineSettings.sourceDb');
    if (source === 'env') return $t('settings.engineSettings.sourceEnv');
    return $t('settings.engineSettings.sourceDefault');
  }

  function sourceClass(source: SettingSource): string {
    if (source === 'db') return 'source-db';
    if (source === 'env') return 'source-env';
    return 'source-default';
  }

  $: isDirty = settings !== null && (
    draftDiarizerBackend !== settings.diarizer_backend.value ||
    draftDiarizerRequireSidecar !== settings.diarizer_require_sidecar.value ||
    draftBoundarySmoothing !== settings.boundary_smoothing_enabled.value ||
    draftAcousticRecheck !== settings.boundary_acoustic_recheck_enabled.value ||
    Number(draftAcousticCosineMargin) !== settings.boundary_acoustic_cosine_margin.value ||
    Number(draftAcousticMaxWordDur) !== settings.boundary_acoustic_max_word_dur.value
  );

  $: dispatch('change', { hasChanges: isDirty });
</script>

<div class="engine-settings">
  {#if loading}
    <div class="loading">{$t('settings.asrProvider.loading')}</div>
  {:else if settings}
    <div class="settings-form">
      <h3 class="section-title">{$t('settings.engineSettings.diarizerHeading')}</h3>

      <!-- Diarizer Backend -->
      <div class="form-row">
        <div class="form-field">
          <div class="field-label-row">
            <label for="diarizer-backend">{$t('settings.engineSettings.diarizerBackend')}</label>
            <span class="source-badge {sourceClass(settings.diarizer_backend.source)}">
              {sourceLabel(settings.diarizer_backend.source)}
            </span>
            {#if settings.diarizer_backend.source !== 'default'}
              <button
                class="reset-btn"
                on:click={() => resetKey('diarizer_backend')}
                disabled={resetInProgress === 'diarizer_backend' || saving}
                title={$t('settings.engineSettings.resetKey')}
              >
                {#if resetInProgress === 'diarizer_backend'}
                  <Spinner size="small" />
                {:else}
                  {$t('settings.engineSettings.resetKey')}
                {/if}
              </button>
            {/if}
          </div>
          <select
            id="diarizer-backend"
            bind:value={draftDiarizerBackend}
            class="form-select"
            disabled={saving || resetInProgress !== null}
          >
            <option value="native">{$t('settings.engineSettings.backendNative')}</option>
            <option value="pyannote">{$t('settings.engineSettings.backendPyannote')}</option>
          </select>
        </div>
      </div>

      <!-- Require Diarization Sidecar -->
      <div class="form-row">
        <div class="form-field">
          <div class="field-label-row">
            <span class="field-name">{$t('settings.engineSettings.diarizerRequireSidecar')}</span>
            <span class="source-badge {sourceClass(settings.diarizer_require_sidecar.source)}">
              {sourceLabel(settings.diarizer_require_sidecar.source)}
            </span>
            {#if settings.diarizer_require_sidecar.source !== 'default'}
              <button
                class="reset-btn"
                on:click={() => resetKey('diarizer_require_sidecar')}
                disabled={resetInProgress === 'diarizer_require_sidecar' || saving}
                title={$t('settings.engineSettings.resetKey')}
              >
                {#if resetInProgress === 'diarizer_require_sidecar'}
                  <Spinner size="small" />
                {:else}
                  {$t('settings.engineSettings.resetKey')}
                {/if}
              </button>
            {/if}
          </div>
          <label class="toggle-label" for="diarizer-require-sidecar-input">
            <input
              id="diarizer-require-sidecar-input"
              type="checkbox"
              class="toggle-input"
              bind:checked={draftDiarizerRequireSidecar}
              disabled={saving || resetInProgress !== null}
            />
            <span class="toggle-switch"></span>
            <span class="toggle-text help-text">{$t('settings.engineSettings.diarizerRequireSidecarHelp')}</span>
          </label>
        </div>
      </div>

      <h3 class="section-title boundary-heading">{$t('settings.engineSettings.boundaryHeading')}</h3>

      <!-- Boundary Smoothing -->
      <div class="form-row">
        <div class="form-field">
          <div class="field-label-row">
            <span class="field-name">{$t('settings.engineSettings.boundarySmoothing')}</span>
            <span class="source-badge {sourceClass(settings.boundary_smoothing_enabled.source)}">
              {sourceLabel(settings.boundary_smoothing_enabled.source)}
            </span>
            {#if settings.boundary_smoothing_enabled.source !== 'default'}
              <button
                class="reset-btn"
                on:click={() => resetKey('boundary_smoothing_enabled')}
                disabled={resetInProgress === 'boundary_smoothing_enabled' || saving}
                title={$t('settings.engineSettings.resetKey')}
              >
                {#if resetInProgress === 'boundary_smoothing_enabled'}
                  <Spinner size="small" />
                {:else}
                  {$t('settings.engineSettings.resetKey')}
                {/if}
              </button>
            {/if}
          </div>
          <label class="toggle-label" for="boundary-smoothing-input">
            <input
              id="boundary-smoothing-input"
              type="checkbox"
              class="toggle-input"
              bind:checked={draftBoundarySmoothing}
              disabled={saving || resetInProgress !== null}
            />
            <span class="toggle-switch"></span>
            <span class="toggle-text help-text">{$t('settings.engineSettings.boundarySmoothingHelp')}</span>
          </label>
        </div>
      </div>

      <!-- Acoustic Backchannel Re-check -->
      <div class="form-row">
        <div class="form-field">
          <div class="field-label-row">
            <span class="field-name">{$t('settings.engineSettings.boundaryAcousticRecheck')}</span>
            <span class="source-badge {sourceClass(settings.boundary_acoustic_recheck_enabled.source)}">
              {sourceLabel(settings.boundary_acoustic_recheck_enabled.source)}
            </span>
            {#if settings.boundary_acoustic_recheck_enabled.source !== 'default'}
              <button
                class="reset-btn"
                on:click={() => resetKey('boundary_acoustic_recheck_enabled')}
                disabled={resetInProgress === 'boundary_acoustic_recheck_enabled' || saving}
                title={$t('settings.engineSettings.resetKey')}
              >
                {#if resetInProgress === 'boundary_acoustic_recheck_enabled'}
                  <Spinner size="small" />
                {:else}
                  {$t('settings.engineSettings.resetKey')}
                {/if}
              </button>
            {/if}
          </div>
          <label class="toggle-label" for="boundary-acoustic-recheck-input">
            <input
              id="boundary-acoustic-recheck-input"
              type="checkbox"
              class="toggle-input"
              bind:checked={draftAcousticRecheck}
              disabled={saving || resetInProgress !== null}
            />
            <span class="toggle-switch"></span>
            <span class="toggle-text help-text">{$t('settings.engineSettings.boundaryAcousticRecheckHelp')}</span>
          </label>
          <p class="field-hint scope-note" data-testid="recheck-scope">
            {$t('settings.engineSettings.boundaryAcousticScope')}
          </p>
        </div>
      </div>

      <!-- Re-check Cosine Margin -->
      <div class="form-row">
        <div class="form-field">
          <div class="field-label-row">
            <label for="boundary-acoustic-cosine-margin">{$t('settings.engineSettings.boundaryAcousticCosineMargin')}</label>
            <span class="source-badge {sourceClass(settings.boundary_acoustic_cosine_margin.source)}">
              {sourceLabel(settings.boundary_acoustic_cosine_margin.source)}
            </span>
            {#if settings.boundary_acoustic_cosine_margin.source !== 'default'}
              <button
                class="reset-btn"
                on:click={() => resetKey('boundary_acoustic_cosine_margin')}
                disabled={resetInProgress === 'boundary_acoustic_cosine_margin' || saving}
                title={$t('settings.engineSettings.resetKey')}
              >
                {#if resetInProgress === 'boundary_acoustic_cosine_margin'}
                  <Spinner size="small" />
                {:else}
                  {$t('settings.engineSettings.resetKey')}
                {/if}
              </button>
            {/if}
          </div>
          <input
            id="boundary-acoustic-cosine-margin"
            type="number"
            step="0.01"
            min="0"
            max="1"
            class="form-input"
            bind:value={draftAcousticCosineMargin}
            disabled={saving || resetInProgress !== null || !draftAcousticRecheck}
          />
          <p class="field-hint">{$t('settings.engineSettings.boundaryAcousticCosineMarginHelp')}</p>
          <p class="field-hint scope-note" data-testid="recheck-scope">
            {$t('settings.engineSettings.boundaryAcousticScope')}
          </p>
        </div>
      </div>

      <!-- Re-check Max Word Duration -->
      <div class="form-row">
        <div class="form-field">
          <div class="field-label-row">
            <label for="boundary-acoustic-max-word-dur">{$t('settings.engineSettings.boundaryAcousticMaxWordDur')}</label>
            <span class="source-badge {sourceClass(settings.boundary_acoustic_max_word_dur.source)}">
              {sourceLabel(settings.boundary_acoustic_max_word_dur.source)}
            </span>
            {#if settings.boundary_acoustic_max_word_dur.source !== 'default'}
              <button
                class="reset-btn"
                on:click={() => resetKey('boundary_acoustic_max_word_dur')}
                disabled={resetInProgress === 'boundary_acoustic_max_word_dur' || saving}
                title={$t('settings.engineSettings.resetKey')}
              >
                {#if resetInProgress === 'boundary_acoustic_max_word_dur'}
                  <Spinner size="small" />
                {:else}
                  {$t('settings.engineSettings.resetKey')}
                {/if}
              </button>
            {/if}
          </div>
          <input
            id="boundary-acoustic-max-word-dur"
            type="number"
            step="0.1"
            min="0.1"
            max="5"
            class="form-input"
            bind:value={draftAcousticMaxWordDur}
            disabled={saving || resetInProgress !== null || !draftAcousticRecheck}
          />
          <p class="field-hint">{$t('settings.engineSettings.boundaryAcousticMaxWordDurHelp')}</p>
          <p class="field-hint scope-note" data-testid="recheck-scope">
            {$t('settings.engineSettings.boundaryAcousticScope')}
          </p>
        </div>
      </div>

      <!-- Save button -->
      <div class="form-actions">
        <button
          class="btn btn-primary"
          on:click={save}
          disabled={saving || resetInProgress !== null || !isDirty}
        >
          {#if saving}
            <Spinner size="small" />
            {$t('settings.engineSettings.save')}...
          {:else}
            {$t('settings.engineSettings.save')}
          {/if}
        </button>
      </div>
    </div>
  {/if}
</div>

<style>
  /* Left-aligned: these panels sit under a tab strip, and centring a narrower column
     beneath left-aligned tabs reads as misaligned. */
  .engine-settings {
    max-width: 800px;
  }

  .section-title {
    font-size: 1rem;
    font-weight: 600;
    color: var(--text-color);
    margin: 0;
  }

  .boundary-heading {
    margin-top: 0.5rem;
  }

  .loading {
    text-align: center;
    padding: 3rem;
    color: var(--text-muted);
    font-size: 0.8125rem;
  }

  .settings-form {
    display: flex;
    flex-direction: column;
    gap: 1.25rem;
  }

  .form-row {
    display: flex;
    flex-direction: column;
  }

  .form-field {
    display: flex;
    flex-direction: column;
    gap: 0.375rem;
    padding: 0.875rem 1rem;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    background: var(--card-bg);
  }

  .field-label-row {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex-wrap: wrap;
  }

  .field-label-row label,
  .field-label-row .field-name {
    font-size: 0.875rem;
    font-weight: 500;
    color: var(--text-color);
    flex: 1;
    min-width: 0;
  }

  .source-badge {
    display: inline-flex;
    align-items: center;
    padding: 0.15rem 0.5rem;
    border-radius: 4px;
    font-size: 0.675rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.4px;
    white-space: nowrap;
    flex-shrink: 0;
  }

  .source-db {
    background: rgba(var(--primary-color-rgb), 0.12);
    color: var(--primary-on-surface);
  }
  :global([data-theme='dark']) .source-db {
    background: rgba(var(--primary-color-rgb), 0.2);
    color: #60a5fa;
  }

  .source-env {
    background: rgba(245, 158, 11, 0.12);
    color: #d97706;
  }
  :global([data-theme='dark']) .source-env {
    background: rgba(245, 158, 11, 0.2);
    color: #fbbf24;
  }

  .source-default {
    background: var(--card-bg);
    color: var(--text-muted);
    border: 1px solid var(--border-color);
  }

  .reset-btn {
    display: inline-flex;
    align-items: center;
    gap: 0.25rem;
    padding: 0.2rem 0.6rem;
    border-radius: 4px;
    font-size: 0.7rem;
    font-weight: 500;
    cursor: pointer;
    background: var(--surface-color);
    color: var(--text-muted);
    border: 1px solid var(--border-color);
    transition: all 0.15s;
    white-space: nowrap;
    flex-shrink: 0;
  }

  .reset-btn:hover:not(:disabled) {
    background: var(--button-hover);
    color: var(--text-color);
  }

  .reset-btn:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .form-select {
    width: 100%;
    padding: 0.5rem 0.75rem;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    background: var(--surface-color, var(--card-bg));
    color: var(--text-color);
    font-size: 0.8125rem;
    height: 36px;
    cursor: pointer;
  }

  .form-select:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .form-input {
    width: 100%;
    padding: 0.5rem 0.75rem;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    background: var(--surface-color, var(--card-bg));
    color: var(--text-color);
    font-size: 0.8125rem;
    box-sizing: border-box;
  }

  .form-input:focus {
    outline: none;
    border-color: var(--primary-color);
    box-shadow: 0 0 0 2px rgba(var(--primary-color-rgb), 0.15);
  }

  .form-input:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .field-hint {
    margin: 0;
    font-size: 0.75rem;
    color: var(--text-muted);
    line-height: 1.4;
  }

  /* Toggle switch */
  .toggle-label {
    display: flex;
    align-items: flex-start;
    gap: 0.5rem;
    cursor: pointer;
    padding-top: 0.125rem;
  }

  .toggle-input {
    display: none;
  }

  .toggle-switch {
    position: relative;
    width: 32px;
    height: 18px;
    background: var(--border-color);
    border-radius: 9px;
    transition: background 0.2s;
    flex-shrink: 0;
    margin-top: 0.1rem;
  }

  .toggle-switch::after {
    content: '';
    position: absolute;
    top: 3px;
    left: 3px;
    width: 12px;
    height: 12px;
    background: white;
    border-radius: 50%;
    transition: transform 0.2s;
  }

  .toggle-input:checked + .toggle-switch {
    background: var(--primary-color, var(--primary-color));
  }

  .toggle-input:checked + .toggle-switch::after {
    transform: translateX(14px);
  }

  .toggle-input:disabled + .toggle-switch {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .toggle-text {
    user-select: none;
  }

  .help-text {
    font-size: 0.775rem;
    color: var(--text-muted);
    line-height: 1.4;
    font-weight: 400;
  }

  .form-actions {
    display: flex;
    justify-content: flex-end;
    padding-top: 0.5rem;
    border-top: 1px solid var(--border-color);
  }

  .btn {
    display: inline-flex;
    align-items: center;
    gap: 0.375rem;
    padding: 0.5rem 1.25rem;
    border-radius: 6px;
    font-size: 0.8125rem;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.15s;
    border: none;
    white-space: nowrap;
  }

  .btn-primary {
    background: var(--primary-color, var(--primary-color));
    color: white;
    box-shadow: 0 1px 4px rgba(var(--primary-color-rgb), 0.2);
  }

  .btn-primary:hover:not(:disabled) {
    background: var(--primary-hover, #2563eb);
    transform: translateY(-1px);
  }

  .btn-primary:disabled {
    opacity: 0.6;
    cursor: not-allowed;
    transform: none;
  }

  @media (max-width: 768px) {
    .field-label-row {
      gap: 0.375rem;
    }

    .form-field {
      padding: 0.75rem;
    }
  }
</style>
