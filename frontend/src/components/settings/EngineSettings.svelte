<script lang="ts">
  import SettingsSkeleton from '$components/ui/SettingsSkeleton.svelte';
  import { onMount, createEventDispatcher } from 'svelte';
  import Spinner from '../ui/Spinner.svelte';
  import axiosInstance from '$lib/axios';
  import { toastStore } from '$stores/toast';
  import { t } from '$stores/locale';
  import { getErrorMessage } from '$lib/utils/apiError';
  import { getPipelineView } from '$lib/api/pipeline';
  import { PIPELINE_ISSUE_KEYS } from '$lib/settings/pipelineKeys';
  import { pipelineFailureKey } from '$lib/settings/pipelineIssues';
  import EngineIntro, { type SidecarStatus } from '$components/settings/EngineIntro.svelte';
  import EngineSettingRow from '$components/settings/EngineSettingRow.svelte';
  import ExpandableSection from '$components/ui/ExpandableSection.svelte';

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
    /** Read-only probe result; not a setting, so it has no source badge or reset. */
    diarizer_sidecar_status?: SidecarStatus;
  }

  type EngineSettingKey = Exclude<keyof EngineSettingsResponse, 'diarizer_sidecar_status'>;

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

  // Lite has no local speaker detection engine to choose. The plan says so; asked once here
  // and never fatal, because the panel works without it.
  let isLite = false;
  // The re-check tuning (cosine margin, max word length) is detail most admins never open.
  let advancedOpen = false;

  onMount(async () => {
    await Promise.all([loadData(), loadMode()]);
  });

  async function loadMode() {
    try {
      isLite = (await getPipelineView()).deployment_mode === 'lite';
    } catch {
      isLite = false;
    }
  }

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
      const refusal = pipelineFailureKey(err, 'settings.engineSettings.saveFailed');
      toastStore.error(
        refusal ? $t(refusal) : getErrorMessage(err, $t('settings.engineSettings.saveFailed')),
        5000
      );
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
    <SettingsSkeleton variant="form" count={4} label={$t('settings.asrProvider.loading')} />
  {:else if settings}
    <div class="settings-form">
      <EngineIntro sidecarStatus={settings.diarizer_sidecar_status} />

      <h3 class="section-title">{$t('settings.engineSettings.diarizerHeading')}</h3>

      {#if isLite}
        <!-- Lite is cloud-only: the server refuses these two settings (local_diarization_unavailable). -->
        <p class="lite-note" data-testid="engine-lite-note">
          {$t(PIPELINE_ISSUE_KEYS.local_diarization_unavailable)}
        </p>
      {:else}

        <EngineSettingRow
          label={$t('settings.engineSettings.diarizerBackend')}
          labelFor="diarizer-backend"
          source={settings.diarizer_backend.source}
          resetting={resetInProgress === 'diarizer_backend'}
          disabled={saving}
          hint={$t('settings.engineSettings.diarizerBackendHint')}
          on:click={() => resetKey('diarizer_backend')}
        >
          <select
            id="diarizer-backend"
            bind:value={draftDiarizerBackend}
            class="form-select"
            disabled={saving || resetInProgress !== null}
          >
            <option value="native">{$t('settings.engineSettings.backendNative')}</option>
            <option value="pyannote">{$t('settings.engineSettings.backendPyannote')}</option>
          </select>
        </EngineSettingRow>

        <EngineSettingRow
          label={$t('settings.engineSettings.diarizerRequireSidecar')}
          source={settings.diarizer_require_sidecar.source}
          resetting={resetInProgress === 'diarizer_require_sidecar'}
          disabled={saving}
          on:click={() => resetKey('diarizer_require_sidecar')}
        >
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
        </EngineSettingRow>
      {/if}

      <h3 class="section-title boundary-heading">{$t('settings.engineSettings.boundaryHeading')}</h3>

      <EngineSettingRow
        label={$t('settings.engineSettings.boundarySmoothing')}
        source={settings.boundary_smoothing_enabled.source}
        resetting={resetInProgress === 'boundary_smoothing_enabled'}
        disabled={saving}
        on:click={() => resetKey('boundary_smoothing_enabled')}
      >
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
      </EngineSettingRow>

      <EngineSettingRow
        label={$t('settings.engineSettings.boundaryAcousticRecheck')}
        source={settings.boundary_acoustic_recheck_enabled.source}
        resetting={resetInProgress === 'boundary_acoustic_recheck_enabled'}
        disabled={saving}
        on:click={() => resetKey('boundary_acoustic_recheck_enabled')}
      >
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
        <svelte:fragment slot="after">
          <ExpandableSection title={$t('settings.engineSettings.advancedToggle')} bind:expanded={advancedOpen}>
            <div class="advanced-fields">
              <EngineSettingRow
                label={$t('settings.engineSettings.boundaryAcousticCosineMargin')}
                labelFor="boundary-acoustic-cosine-margin"
                source={settings.boundary_acoustic_cosine_margin.source}
                resetting={resetInProgress === 'boundary_acoustic_cosine_margin'}
                disabled={saving}
                hint={$t('settings.engineSettings.boundaryAcousticCosineMarginHelp')}
                on:click={() => resetKey('boundary_acoustic_cosine_margin')}
              >
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
              </EngineSettingRow>

              <EngineSettingRow
                label={$t('settings.engineSettings.boundaryAcousticMaxWordDur')}
                labelFor="boundary-acoustic-max-word-dur"
                source={settings.boundary_acoustic_max_word_dur.source}
                resetting={resetInProgress === 'boundary_acoustic_max_word_dur'}
                disabled={saving}
                hint={$t('settings.engineSettings.boundaryAcousticMaxWordDurHelp')}
                on:click={() => resetKey('boundary_acoustic_max_word_dur')}
              >
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
              </EngineSettingRow>
            </div>
          </ExpandableSection>
        </svelte:fragment>
      </EngineSettingRow>

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
  .lite-note {
    margin: 0 0 1rem;
    padding: 0.75rem 1rem;
    border-radius: 8px;
    border: 1px solid rgba(var(--primary-color-rgb), 0.3);
    background: rgba(var(--primary-color-rgb), 0.08);
    color: var(--text-color);
    font-size: 0.85rem;
    line-height: 1.45;
  }

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


  .settings-form {
    display: flex;
    flex-direction: column;
    gap: 1.25rem;
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


  .advanced-fields {
    display: flex;
    flex-direction: column;
    gap: 0.75rem;
    padding-top: 0.25rem;
  }

  /* The tuning rows sit inside the re-check card; a second border box nested in it is noise. */
  .advanced-fields :global(.form-field) {
    padding: 0.5rem 0;
    border: none;
    background: transparent;
  }

  .scope-note {
    margin: 0;
    font-size: 0.75rem;
    color: var(--text-muted);
    line-height: 1.4;
  }
</style>
