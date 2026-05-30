<script lang="ts">
  import { onMount } from 'svelte';
  import Spinner from '../ui/Spinner.svelte';
  import axiosInstance from '$lib/axios';
  import { toastStore } from '$stores/toast';
  import { t } from '$stores/locale';

  type SettingSource = 'db' | 'env' | 'default';

  interface EngineSettingValue<T> {
    value: T;
    source: SettingSource;
  }

  interface EngineSettingsResponse {
    transcriber_backend: EngineSettingValue<string>;
    diarizer_backend: EngineSettingValue<string>;
    gpu_split: EngineSettingValue<boolean>;
    precompute_vad: EngineSettingValue<boolean>;
    boundary_smoothing_enabled: EngineSettingValue<boolean>;
    shared_volume_path: EngineSettingValue<string>;
  }

  type EngineSettingKey = keyof EngineSettingsResponse;

  let loading = false;
  let saving = false;
  let resetInProgress: EngineSettingKey | null = null;

  // Current values from server
  let settings: EngineSettingsResponse | null = null;

  // Draft values (bound to form controls)
  let draftTranscriberBackend = 'faster_whisper';
  let draftDiarizerBackend = 'pyannote';
  let draftGpuSplit = false;
  let draftPrecomputeVad = false;
  let draftBoundarySmoothing = false;
  let draftSharedVolumePath = '/tmp/transcription';

  onMount(async () => {
    await loadData();
  });

  async function loadData() {
    loading = true;
    try {
      const res = await axiosInstance.get<EngineSettingsResponse>('/admin/engine-settings');
      settings = res.data;
      draftTranscriberBackend = settings.transcriber_backend.value;
      draftDiarizerBackend = settings.diarizer_backend.value;
      draftGpuSplit = settings.gpu_split.value;
      draftPrecomputeVad = settings.precompute_vad.value;
      draftBoundarySmoothing = settings.boundary_smoothing_enabled.value;
      draftSharedVolumePath = settings.shared_volume_path.value;
    } catch (err: any) {
      const detail = err?.response?.data?.detail;
      toastStore.error(typeof detail === 'string' ? detail : 'Failed to load engine settings', 5000);
    } finally {
      loading = false;
    }
  }

  async function save() {
    if (!settings) return;
    saving = true;

    // Only send keys where the draft differs from the current server value
    const payload: Partial<{
      transcriber_backend: string;
      diarizer_backend: string;
      gpu_split: boolean;
      precompute_vad: boolean;
      boundary_smoothing_enabled: boolean;
      shared_volume_path: string;
    }> = {};

    if (draftTranscriberBackend !== settings.transcriber_backend.value) {
      payload.transcriber_backend = draftTranscriberBackend;
    }
    if (draftDiarizerBackend !== settings.diarizer_backend.value) {
      payload.diarizer_backend = draftDiarizerBackend;
    }
    if (draftGpuSplit !== settings.gpu_split.value) {
      payload.gpu_split = draftGpuSplit;
    }
    if (draftPrecomputeVad !== settings.precompute_vad.value) {
      payload.precompute_vad = draftPrecomputeVad;
    }
    if (draftBoundarySmoothing !== settings.boundary_smoothing_enabled.value) {
      payload.boundary_smoothing_enabled = draftBoundarySmoothing;
    }
    if (draftSharedVolumePath !== settings.shared_volume_path.value) {
      payload.shared_volume_path = draftSharedVolumePath;
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
    } catch (err: any) {
      const detail = err?.response?.data?.detail;
      toastStore.error(typeof detail === 'string' ? detail : 'Failed to save engine settings', 5000);
    } finally {
      saving = false;
    }
  }

  async function resetKey(key: EngineSettingKey) {
    resetInProgress = key;
    try {
      await axiosInstance.delete(`/admin/engine-settings/${key}`);
      toastStore.success(`Reset ${key} to default`);
      await loadData();
    } catch (err: any) {
      const detail = err?.response?.data?.detail;
      toastStore.error(typeof detail === 'string' ? detail : `Failed to reset ${key}`, 5000);
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
    draftTranscriberBackend !== settings.transcriber_backend.value ||
    draftDiarizerBackend !== settings.diarizer_backend.value ||
    draftGpuSplit !== settings.gpu_split.value ||
    draftPrecomputeVad !== settings.precompute_vad.value ||
    draftBoundarySmoothing !== settings.boundary_smoothing_enabled.value ||
    draftSharedVolumePath !== settings.shared_volume_path.value
  );
</script>

<div class="engine-settings">
  {#if loading}
    <div class="loading">{$t('settings.asrProvider.loading')}</div>
  {:else if settings}
    <!-- Section header -->
    <div class="section-header">
      <h4>
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <rect x="2" y="3" width="20" height="14" rx="2" ry="2"/>
          <line x1="8" y1="21" x2="16" y2="21"/>
          <line x1="12" y1="17" x2="12" y2="21"/>
        </svg>
        {$t('settings.engineSettings.title')}
      </h4>
    </div>

    <div class="settings-form">

      <!-- Transcriber Backend -->
      <div class="form-row">
        <div class="form-field">
          <div class="field-label-row">
            <label for="transcriber-backend">{$t('settings.engineSettings.transcriberBackend')}</label>
            <span class="source-badge {sourceClass(settings.transcriber_backend.source)}">
              {sourceLabel(settings.transcriber_backend.source)}
            </span>
            {#if settings.transcriber_backend.source !== 'default'}
              <button
                class="reset-btn"
                on:click={() => resetKey('transcriber_backend')}
                disabled={resetInProgress === 'transcriber_backend' || saving}
                title={$t('settings.engineSettings.resetKey')}
              >
                {#if resetInProgress === 'transcriber_backend'}
                  <Spinner size="small" />
                {:else}
                  {$t('settings.engineSettings.resetKey')}
                {/if}
              </button>
            {/if}
          </div>
          <select
            id="transcriber-backend"
            bind:value={draftTranscriberBackend}
            class="form-select"
            disabled={saving || resetInProgress !== null}
          >
            <option value="faster_whisper">faster_whisper</option>
            <option value="whisperx">whisperx</option>
            <option value="cloud">cloud</option>
          </select>
        </div>
      </div>

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
            <option value="pyannote">pyannote</option>
          </select>
        </div>
      </div>

      <!-- GPU Split -->
      <div class="form-row">
        <div class="form-field">
          <div class="field-label-row">
            <span class="field-name">{$t('settings.engineSettings.gpuSplit')}</span>
            <span class="source-badge {sourceClass(settings.gpu_split.source)}">
              {sourceLabel(settings.gpu_split.source)}
            </span>
            {#if settings.gpu_split.source !== 'default'}
              <button
                class="reset-btn"
                on:click={() => resetKey('gpu_split')}
                disabled={resetInProgress === 'gpu_split' || saving}
                title={$t('settings.engineSettings.resetKey')}
              >
                {#if resetInProgress === 'gpu_split'}
                  <Spinner size="small" />
                {:else}
                  {$t('settings.engineSettings.resetKey')}
                {/if}
              </button>
            {/if}
          </div>
          <label class="toggle-label" for="gpu-split-input">
            <input
              id="gpu-split-input"
              type="checkbox"
              class="toggle-input"
              bind:checked={draftGpuSplit}
              disabled={saving || resetInProgress !== null}
            />
            <span class="toggle-switch"></span>
            <span class="toggle-text help-text">{$t('settings.engineSettings.gpuSplitHelp')}</span>
          </label>
        </div>
      </div>

      <!-- Precompute VAD -->
      <div class="form-row">
        <div class="form-field">
          <div class="field-label-row">
            <span class="field-name">{$t('settings.engineSettings.precomputeVad')}</span>
            <span class="source-badge {sourceClass(settings.precompute_vad.source)}">
              {sourceLabel(settings.precompute_vad.source)}
            </span>
            {#if settings.precompute_vad.source !== 'default'}
              <button
                class="reset-btn"
                on:click={() => resetKey('precompute_vad')}
                disabled={resetInProgress === 'precompute_vad' || saving}
                title={$t('settings.engineSettings.resetKey')}
              >
                {#if resetInProgress === 'precompute_vad'}
                  <Spinner size="small" />
                {:else}
                  {$t('settings.engineSettings.resetKey')}
                {/if}
              </button>
            {/if}
          </div>
          <label class="toggle-label" for="precompute-vad-input">
            <input
              id="precompute-vad-input"
              type="checkbox"
              class="toggle-input"
              bind:checked={draftPrecomputeVad}
              disabled={saving || resetInProgress !== null}
            />
            <span class="toggle-switch"></span>
            <span class="toggle-text help-text">{$t('settings.engineSettings.precomputeVadHelp')}</span>
          </label>
        </div>
      </div>

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

      <!-- Shared Volume Path -->
      <div class="form-row">
        <div class="form-field">
          <div class="field-label-row">
            <label for="shared-volume-path">{$t('settings.engineSettings.sharedVolumePath')}</label>
            <span class="source-badge {sourceClass(settings.shared_volume_path.source)}">
              {sourceLabel(settings.shared_volume_path.source)}
            </span>
            {#if settings.shared_volume_path.source !== 'default'}
              <button
                class="reset-btn"
                on:click={() => resetKey('shared_volume_path')}
                disabled={resetInProgress === 'shared_volume_path' || saving}
                title={$t('settings.engineSettings.resetKey')}
              >
                {#if resetInProgress === 'shared_volume_path'}
                  <Spinner size="small" />
                {:else}
                  {$t('settings.engineSettings.resetKey')}
                {/if}
              </button>
            {/if}
          </div>
          <input
            id="shared-volume-path"
            type="text"
            class="form-input"
            bind:value={draftSharedVolumePath}
            disabled={saving || resetInProgress !== null}
            placeholder="/tmp/transcription"
          />
          <p class="field-hint">{$t('settings.engineSettings.sharedVolumePathHelp')}</p>
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
  .engine-settings {
    max-width: 800px;
    margin: 0 auto;
  }

  .loading {
    text-align: center;
    padding: 3rem;
    color: var(--text-muted);
    font-size: 0.8125rem;
  }

  .section-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 1.25rem;
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
    color: var(--primary-color);
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
    background: var(--primary-color, #3b82f6);
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
    background: var(--primary-color, #3b82f6);
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
