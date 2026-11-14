<script lang="ts">
  import InfoIcon from '$components/ui/InfoIcon.svelte';
  import { createEventDispatcher, tick } from 'svelte';
  import { fly } from 'svelte/transition';
  import { cubicOut } from 'svelte/easing';
  import { t } from '$stores/locale';
  import { isLLMAvailable, llmStatusStore } from '$stores/llmStatus';
  import { toastStore } from '$stores/toast';
  import Spinner from './ui/Spinner.svelte';
  import BaseModal from './ui/BaseModal.svelte';
  import PerFileModelSelect from './PerFileModelSelect.svelte';
  import AudioTrackSelect from './AudioTrackSelect.svelte';
  import ForcedLanguageSelect from './ForcedLanguageSelect.svelte';
  import { ASRSettingsApi } from '$lib/api/asrSettings';
  import { reprocessFile, reprocessFiles } from '$lib/api/reprocess';
  import { capabilities, isCapabilityEnabled } from '$stores/capabilities';
  import { getErrorMessage } from '$lib/utils/apiError';
  import {
    getTranscriptionSettings,
    getTranscriptionSystemDefaults,
    speakerPrefill,
    type TranscriptionSettings,
    type TranscriptionSystemDefaults
  } from '$lib/api/transcriptionSettings';
  import {
    buildReprocessBody,
    buildStageGroups,
    countBulkResults,
    isReprocessValid,
    requiresSettingsStep,
    selectableStageIds,
    selectionHasDiarizationDisabled,
    selectionHasTranscript,
    showsSpeakerSettings,
    toggleStage,
    wizardStepLabels,
    type ReprocessFile
  } from '$lib/fileDetail/reprocessStages';

  export let showModal: boolean = false;
  export let file: ReprocessFile & { uuid?: string } | null = null;
  export let reprocessing: boolean = false;
  export let bulkMode: boolean = false;
  export let bulkFiles: (ReprocessFile & { uuid: string })[] = [];
  /** Stages ticked when the modal opens; the wizard then skips ahead to the settings step. */
  export let initialStages: string[] = [];

  const dispatch = createEventDispatcher();

  // Wizard state
  let currentStep = 1;
  let direction = 1; // 1 = forward, -1 = backward

  // Stage selection state
  let selectedStages = new Set<string>();

  // Speaker settings
  let minSpeakers: number | null = null;
  let maxSpeakers: number | null = null;
  let numSpeakers: number | null = null;
  // The user's saved speaker behaviour, so a re-run starts from their range (and a blank field
  // means what they chose) instead of from nothing.
  let transcriptionSettings: TranscriptionSettings | null = null;
  let transcriptionSystemDefaults: TranscriptionSystemDefaults | null = null;

  // ASR provider info
  let isCloudASR = false;
  let activeASRProvider = 'local';
  let activeASRModel = '';

  // Audio track of a multi-track file (#1263); single-file mode only.
  let selectedAudioTrack: number | null = null;

  // Language forced for this run (#1263); single-file mode only, null = the saved setting.
  let selectedLanguage: string | null = null;

  // Model selection for reprocessing
  let selectedReprocessModel: string | null = null;
  // Off = the deployment owns the model; the server ignores a requested one.
  $: modelChoiceEnabled = isCapabilityEnabled($capabilities, 'transcription.model_choice');
  let adminDefaultModel = '';

  // Computed state
  $: hasTranscript = selectionHasTranscript(file, bulkMode, bulkFiles);
  // A completed file's transcript always carries word timestamps, so re-diarize needs no more.
  $: hasWordTimestamps = hasTranscript;
  // A cloud ASR provider returns its own speakers on a transcription re-run, so the range is
  // moot there; a re-diarize re-detects with the user's current speaker-detection choice
  // whichever provider transcribed the file, so its range always applies.
  $: showSpeakerSettings = showsSpeakerSettings(selectedStages, isCloudASR);
  $: isValid = isReprocessValid(selectedStages, showSpeakerSettings, minSpeakers, maxSpeakers);

  // Step navigation — show settings step for speaker config OR model selection
  $: needsSettingsStep = requiresSettingsStep(selectedStages, showSpeakerSettings);
  $: stepLabels = wizardStepLabels(needsSettingsStep);
  $: totalSteps = stepLabels.length;
  $: isLastStep = currentStep === totalSteps;
  $: canGoNext = currentStep === 1 ? selectedStages.size > 0 : true;

  // Validation guards
  $: if (minSpeakers !== null && minSpeakers < 1) minSpeakers = 1;
  $: if (maxSpeakers !== null && maxSpeakers < 1) maxSpeakers = 1;
  $: if (numSpeakers !== null && numSpeakers < 1) numSpeakers = 1;

  $: stageGroups = buildStageGroups(
    { hasTranscript, hasWordTimestamps, llmAvailable: $isLLMAvailable },
    $t
  );
  $: allStages = stageGroups.flatMap((group) => group.stages);

  function goNext() {
    if (currentStep < totalSteps && canGoNext) {
      direction = 1;
      currentStep += 1;
    }
  }

  function goBack() {
    if (currentStep > 1) {
      direction = -1;
      currentStep -= 1;
    }
  }

  function getReviewStepNumber(): number {
    return needsSettingsStep ? 3 : 2;
  }

  function isOnReviewStep(): boolean {
    return currentStep === getReviewStepNumber();
  }

  function isOnSettingsStep(): boolean {
    return needsSettingsStep && currentStep === 2;
  }

  async function handleSubmit() {
    if (selectedStages.size === 0) return;

    try {
      reprocessing = true;
      const stagesArray = Array.from(selectedStages);
      const requestBody = buildReprocessBody({
        stages: stagesArray,
        showSpeakerSettings,
        settings: transcriptionSettings,
        systemDefaults: transcriptionSystemDefaults,
        minSpeakers,
        maxSpeakers,
        numSpeakers,
        model: selectedReprocessModel,
        modelChoiceEnabled,
        audioStreamIndex: bulkMode ? null : selectedAudioTrack,
        audioStreams: bulkMode ? null : file?.audio_streams,
        language: bulkMode ? null : selectedLanguage
      });

      if (bulkMode) {
        const results = await reprocessFiles(
          bulkFiles.map((f) => f.uuid),
          requestBody
        );
        const { successful, failed } = countBulkResults(results);

        if (successful > 0) {
          toastStore.success($t('reprocess.bulkStartedSuccess', { count: successful }));
        }
        if (failed > 0) {
          toastStore.error($t('reprocess.bulkStartFailed', { count: failed }));
        }

        dispatch('reprocess', { stages: stagesArray, count: successful });
      } else {
        // Single file mode
        await reprocessFile(file!.uuid!, requestBody);

        toastStore.success($t('reprocess.startedSuccess'));
        dispatch('reprocess', {
          fileId: file!.uuid,
          stages: stagesArray,
        });
      }

      showModal = false;
    } catch (error) {
      console.error('Error reprocessing:', error);
      if (bulkMode) {
        toastStore.error(
          getErrorMessage(error, $t('reprocess.bulkStartFailed', { count: bulkFiles.length }))
        );
      } else {
        // Surface the server's reason when it gave one — the refusal for a file
        // whose media is missing (409) is only actionable if the user is told
        // which precondition failed. Falls back to the generic string otherwise.
        toastStore.error(getErrorMessage(error, $t('reprocess.startFailed')));
      }
      reprocessing = false;
    }
  }

  function resetState() {
    selectedStages = new Set();
    minSpeakers = null;
    maxSpeakers = null;
    numSpeakers = null;
    currentStep = 1;
    direction = 1;
    reprocessing = false;
  }

  async function loadSpeakerPrefill() {
    try {
      const [settings, defaults] = await Promise.all([
        getTranscriptionSettings(),
        getTranscriptionSystemDefaults()
      ]);
      transcriptionSettings = settings;
      transcriptionSystemDefaults = defaults;
      // Only fill what the user has not already typed while this was loading.
      const prefill = speakerPrefill(settings);
      if (minSpeakers === null) minSpeakers = prefill.minSpeakers;
      if (maxSpeakers === null) maxSpeakers = prefill.maxSpeakers;
    } catch {
      // Blank fields still resolve to the user's saved range on the server.
      transcriptionSettings = null;
      transcriptionSystemDefaults = null;
    }
  }

  // Svelte 4 does not re-run `$:` blocks for state assigned inside a reactive pass (handleOpen
  // runs from one), so apply the preselection after the current flush.
  async function startOnSettingsStep(stages: string[]) {
    await tick();
    selectedStages = new Set(stages);
    if (requiresSettingsStep(selectedStages, showsSpeakerSettings(selectedStages, isCloudASR))) {
      currentStep = 2;
    }
  }

  // Reset state and fetch ASR status when modal opens
  function handleOpen(open: boolean, target: typeof file) {
    if (!open) return;
    resetState();
    selectedReprocessModel = null;
    selectedLanguage = null;
    selectedAudioTrack = target?.audio_stream_index ?? target?.audio_streams?.[0]?.index ?? null;
    void loadSpeakerPrefill();
    if (initialStages.length > 0) void startOnSettingsStep(initialStages);
    // Ensure LLM status is loaded (may not be ready if user navigated quickly)
    void llmStatusStore.initialize();
    ASRSettingsApi.getStatus().then((status) => {
      isCloudASR = status.is_cloud_provider ?? false;
      activeASRProvider = status.active_provider ?? 'local';
      activeASRModel = status.active_model ?? '';
    }).catch(() => {
      isCloudASR = false;
      activeASRProvider = 'local';
      activeASRModel = '';
    });
    ASRSettingsApi.getActiveLocalModel().then((info) => {
      adminDefaultModel = info.active_model || 'large-v3-turbo';
    }).catch(() => {
      adminDefaultModel = 'large-v3-turbo';
    });
  }

  $: handleOpen(showModal, file);

  function handleClose() {
    dispatch('close');
    showModal = false;
  }

  function selectAll() {
    selectedStages = new Set(selectableStageIds(allStages));
  }

  function deselectAll() {
    selectedStages = new Set();
  }

  $: hasDiarizationDisabled = selectionHasDiarizationDisabled(file, bulkMode, bulkFiles);

  $: allSelected = selectableStageIds(allStages).every((id) => selectedStages.has(id));

  function getStageLabelById(id: string): string {
    const stage = allStages.find(s => s.id === id);
    return stage ? $t(stage.labelKey) : id;
  }
</script>

<BaseModal isOpen={showModal} title={bulkMode ? $t('reprocess.bulkModalTitle', { count: bulkFiles.length }) : $t('reprocess.modalTitle')} size="md" zIndex={9999} onClose={handleClose}>
        <!-- Step Indicator -->
        <div class="step-indicator">
          {#each stepLabels as label, i (label)}
            {@const stepNum = i + 1}
            <button
              class="step-dot"
              class:active={currentStep === stepNum}
              class:completed={currentStep > stepNum}
              on:click={() => { if (stepNum < currentStep) { direction = -1; currentStep = stepNum; } }}
              disabled={stepNum > currentStep}
              type="button"
              aria-label={$t('reprocess.stepAria', { label: $t(label), step: stepNum })}
            >
              {#if currentStep > stepNum}
                <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round">
                  <polyline points="20 6 9 17 4 12"></polyline>
                </svg>
              {:else}
                <span class="step-number">{stepNum}</span>
              {/if}
            </button>
            <span
              class="step-label"
              class:active={currentStep === stepNum}
              class:completed={currentStep > stepNum}
            >{$t(label)}</span>
            {#if i < stepLabels.length - 1}
              <div class="step-line" class:completed={currentStep > stepNum}></div>
            {/if}
          {/each}
        </div>

        <!-- Body -->
        <div class="modal-body">
          {#key currentStep}
            <div
              class="step-content"
              in:fly|local={{ x: direction * 40, duration: 200, easing: cubicOut }}
            >
              <!-- Step 1: Select Stages -->
              {#if currentStep === 1}
                <div class="step-instruction-row">
                  <p class="step-instruction">{$t('reprocess.selectStages')}</p>
                  <button
                    type="button"
                    class="select-all-btn"
                    on:click={() => allSelected ? deselectAll() : selectAll()}
                  >
                    {allSelected ? $t('reprocess.deselectAll') : $t('reprocess.selectAll')}
                  </button>
                </div>

                <!-- ASR provider banner -->
                {#if isCloudASR && selectedStages.has('transcription')}
                  <div class="warning-banner warning-info" style="margin-top: 0; margin-bottom: 0.75rem;">
                    <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                      <path d="M22 12h-4l-3 9L9 3l-3 9H2"/>
                    </svg>
                    <span>{$t('reprocess.cloudASRInfo', { provider: ASRSettingsApi.getProviderDisplayName(activeASRProvider), model: activeASRModel })}</span>
                  </div>
                {/if}

                {#if hasDiarizationDisabled}
                  {@render infoBanner($t('reprocess.diarizationWasDisabled'))}
                {/if}

                {#each stageGroups as group (group.labelKey)}
                  <div class="stage-group">
                    <div class="stage-group-header">
                      <span class="stage-group-label">{$t(group.labelKey)}</span>
                      <div class="stage-group-line"></div>
                    </div>
                    {#each group.stages as stage (stage.id)}
                      <label
                        class="stage-item"
                        class:disabled={stage.disabled}
                        class:selected={selectedStages.has(stage.id)}
                      >
                        <div class="stage-checkbox-area">
                          <input
                            type="checkbox"
                            checked={selectedStages.has(stage.id)}
                            disabled={stage.disabled}
                            on:change={() => (selectedStages = toggleStage(selectedStages, stage.id))}
                            class="stage-checkbox"
                          />
                          <div class="stage-check-custom" class:checked={selectedStages.has(stage.id)} class:disabled={stage.disabled}>
                            {#if selectedStages.has(stage.id)}
                              <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round">
                                <polyline points="20 6 9 17 4 12"></polyline>
                              </svg>
                            {/if}
                          </div>
                        </div>
                        <div class="stage-info">
                          <span class="stage-label">{$t(stage.labelKey)}</span>
                          <span class="stage-desc">{$t(stage.descKey)}</span>
                          {#if stage.disabled && stage.disabledReason}
                            <span class="stage-disabled-hint">{stage.disabledReason}</span>
                          {/if}
                        </div>
                      </label>
                    {/each}
                  </div>
                {/each}

              <!-- Step 2: Settings (only when needsSettingsStep && currentStep === 2) -->
              {:else if isOnSettingsStep()}
                <!-- Model Selection -->
                {#if selectedStages.has('transcription') && !modelChoiceEnabled}
                  <p class="model-hint-text" style="margin-bottom: 1rem;" data-testid="reprocess-model-managed">
                    {$t('uploader.modelManaged')}
                  </p>
                {:else if selectedStages.has('transcription')}
                  <div class="setting-field" style="margin-bottom: 1rem;">
                    <label for="reprocess-model-select">
                      {$t('uploader.whisperModel')}
                    </label>
                    <PerFileModelSelect
                      id="reprocess-model-select"
                      bind:value={selectedReprocessModel}
                      adminModel={adminDefaultModel}
                    />
                    {#if selectedReprocessModel === 'base'}
                      <p class="model-hint-text">{$t('uploader.fastProcessingHint')}</p>
                    {/if}
                  </div>
                {/if}

                {#if !bulkMode && (selectedStages.has('transcription') || selectedStages.has('rediarize'))}
                  <AudioTrackSelect streams={file?.audio_streams} bind:value={selectedAudioTrack} />
                {/if}

                {#if !bulkMode && selectedStages.has('transcription')}
                  <ForcedLanguageSelect
                    languages={transcriptionSystemDefaults?.available_source_languages}
                    savedLanguage={transcriptionSettings?.source_language}
                    highlight={file?.audio_language_mismatch === true}
                    bind:value={selectedLanguage}
                  />
                {/if}

                {#if showSpeakerSettings}
                <div class="speaker-settings-section">
                  <div class="stage-group-header">
                    <span class="stage-group-label">{$t('reprocess.speakerSettings')}</span>
                    <div class="stage-group-line"></div>
                  </div>

                  <div class="settings-row">
                    <div class="setting-field">
                      <label for="modal-min-speakers">{$t('reprocess.minSpeakers')}</label>
                      <input
                        id="modal-min-speakers"
                        type="number"
                        min="1"
                        placeholder={$t('reprocess.defaultPlaceholder')}
                        bind:value={minSpeakers}
                        disabled={numSpeakers !== null}
                      />
                    </div>

                    <div class="setting-field">
                      <label for="modal-max-speakers">{$t('reprocess.maxSpeakers')}</label>
                      <input
                        id="modal-max-speakers"
                        type="number"
                        min="1"
                        placeholder={$t('reprocess.defaultPlaceholder')}
                        bind:value={maxSpeakers}
                        disabled={numSpeakers !== null}
                      />
                    </div>
                  </div>

                  <div class="setting-field">
                    <label for="modal-num-speakers">
                      {$t('reprocess.fixedCount')}
                      <span class="hint">{$t('reprocess.fixedCountHint')}</span>
                    </label>
                    <input
                      id="modal-num-speakers"
                      type="number"
                      min="1"
                      placeholder={$t('reprocess.autoPlaceholder')}
                      bind:value={numSpeakers}
                    />
                  </div>

                  {#if minSpeakers !== null && maxSpeakers !== null && minSpeakers > maxSpeakers}
                    <div class="validation-error">
                      {$t('reprocess.validationError')}
                    </div>
                  {/if}

                  <!-- V4 Info Card -->
                  {#if selectedStages.has('transcription') || selectedStages.has('rediarize')}
                    {@render v4InfoCard()}
                  {/if}
                </div>
                {/if}

              <!-- Step 3 (or Step 2 when no settings): Review -->
              {:else if isOnReviewStep()}
                <p class="step-instruction">{$t('reprocess.reviewTitle')}</p>

                <!-- Bulk info banner -->
                {#if bulkMode}
                  {@render infoBanner($t('reprocess.bulkInfoMessage', { count: bulkFiles.length }))}
                {/if}

                <!-- Selected stages -->
                <div class="review-section">
                  <span class="review-label">{$t('reprocess.reviewStages')}</span>
                  <div class="review-pills">
                    {#each Array.from(selectedStages) as stageId (stageId)}
                      <span class="stage-pill">{getStageLabelById(stageId)}</span>
                    {/each}
                  </div>
                </div>

                <!-- Speaker settings summary -->
                {#if showSpeakerSettings}
                  <div class="review-section">
                    <span class="review-label">{$t('reprocess.reviewSpeakerSettings')}</span>
                    <div class="review-settings">
                      {#if numSpeakers !== null}
                        <span class="review-setting">{$t('reprocess.reviewFixedSpeakers', { count: numSpeakers })}</span>
                      {:else if minSpeakers !== null || maxSpeakers !== null}
                        {#if minSpeakers !== null}
                          <span class="review-setting">{$t('reprocess.reviewMinSpeakers', { min: minSpeakers })}</span>
                        {/if}
                        {#if maxSpeakers !== null}
                          <span class="review-setting">{$t('reprocess.reviewMaxSpeakers', { max: maxSpeakers })}</span>
                        {/if}
                      {:else}
                        <span class="review-setting muted">{$t('reprocess.reviewDefaultSettings')}</span>
                      {/if}
                    </div>
                  </div>
                {/if}

                <!-- V4 info card on review (if no settings step was shown) -->
                {#if !needsSettingsStep && (selectedStages.has('transcription') || selectedStages.has('rediarize'))}
                  {@render v4InfoCard()}
                {/if}

                <!-- Warnings -->
                {#if selectedStages.has('transcription')}
                  <div class="warning-banner warning-destructive">
                    <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                      <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"></path>
                      <line x1="12" y1="9" x2="12" y2="13"></line>
                      <line x1="12" y1="17" x2="12.01" y2="17"></line>
                    </svg>
                    <span>
                      {bulkMode
                        ? $t('reprocess.bulkWarningDataLoss', { count: bulkFiles.length })
                        : $t('reprocess.warningDataLoss')}
                    </span>
                  </div>
                {/if}

                {#if selectedStages.has('rediarize')}
                  <div class="warning-banner warning-caution">
                    <InfoIcon />
                    <span>{$t('reprocess.warningRediarize')}</span>
                  </div>
                {/if}
              {/if}
            </div>
          {/key}
        </div>

        <svelte:fragment slot="footer">
          {#if currentStep > 1}
            <button
              class="btn btn-lg btn-secondary"
              on:click={goBack}
              type="button"
            >
              {$t('reprocess.back')}
            </button>
          {:else}
            <button
              class="btn btn-lg btn-secondary"
              on:click={handleClose}
              type="button"
            >
              {$t('reprocess.cancel')}
            </button>
          {/if}

          {#if isLastStep}
            <button
              class="btn btn-lg btn-primary"
              on:click={handleSubmit}
              disabled={!isValid || reprocessing}
              type="button"
            >
              {#if reprocessing}
                <Spinner size="small" color="white" />
              {/if}
              {#if reprocessing}
                {$t('reprocess.buttonLabelProcessing')}
              {:else if bulkMode}
                {$t('reprocess.bulkStartReprocessing', { count: bulkFiles.length })}
              {:else}
                {$t('reprocess.startReprocessing')}
              {/if}
            </button>
          {:else}
            <button
              class="btn btn-lg btn-primary"
              on:click={goNext}
              disabled={!canGoNext}
              type="button"
            >
              {$t('reprocess.next')}
              <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <polyline points="9 18 15 12 9 6"></polyline>
              </svg>
            </button>
          {/if}
        </svelte:fragment>
</BaseModal>

{#snippet infoBanner(text: string)}
  <div class="warning-banner warning-info" style="margin-top: 0; margin-bottom: 0.75rem;">
    <InfoIcon />
    <span>{text}</span>
  </div>
{/snippet}

<!-- What a transcription / re-diarize re-run does to the speaker data. -->
{#snippet v4InfoCard()}
  <div class="v4-info-card">
    <div class="v4-info-header">
      <InfoIcon />
      <span>{$t('reprocess.v4InfoTitle')}</span>
    </div>
    {#if selectedStages.has('transcription')}
      <div class="v4-path">
        <strong>{$t('reprocess.v4PathTranscription')}</strong>
        <span>{$t('reprocess.v4PathTranscriptionDesc')}</span>
      </div>
    {:else}
      <div class="v4-path">
        <strong>{$t('reprocess.v4PathRediarize')}</strong>
        <span>{$t('reprocess.v4PathRediarizeDesc')}</span>
      </div>
    {/if}
  </div>
{/snippet}

<style>
  /* Step Indicator */
  .step-indicator {
    display: flex;
    align-items: center;
    justify-content: center;
    padding: var(--space-4) var(--space-6);
    gap: 0;
    border-bottom: 1px solid var(--border-color);
    flex-shrink: 0;
  }

  .step-dot {
    width: 28px;
    height: 28px;
    border-radius: var(--radius-round);
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: var(--font-size-xs);
    font-weight: var(--font-weight-semibold);
    border: 2px solid var(--border-color);
    background: var(--background-color);
    color: var(--text-secondary);
    cursor: default;
    transition: all var(--duration-base) ease;
    flex-shrink: 0;
    padding: 0;
  }

  .step-dot.active {
    border-color: var(--primary-color);
    background: var(--primary-color);
    color: var(--text-on-solid);
  }

  .step-dot.completed {
    border-color: var(--primary-color);
    background: var(--primary-color);
    color: var(--text-on-solid);
    cursor: pointer;
  }

  .step-dot.completed:hover {
    opacity: 0.85;
  }

  .step-dot:disabled:not(.completed):not(.active) {
    cursor: default;
  }

  .step-number {
    line-height: 1;
  }

  .step-label {
    font-size: var(--font-size-xs);
    font-weight: var(--font-weight-medium);
    color: var(--text-secondary);
    margin-inline-start: var(--space-1-5);
    white-space: nowrap;
    transition: color var(--duration-base) ease;
  }

  .step-label.active {
    color: var(--primary-on-surface);
    font-weight: var(--font-weight-semibold);
  }

  .step-label.completed {
    color: var(--primary-on-surface);
  }

  .step-line {
    flex: 1;
    height: 2px;
    background: var(--border-color);
    margin: 0 var(--space-2);
    min-width: 20px;
    max-width: 60px;
    transition: background var(--duration-base) ease;
  }

  .step-line.completed {
    background: var(--primary-color);
  }

  /* Body */
  .modal-body {
    padding: var(--space-4) var(--space-6) var(--space-5);
    overflow: hidden auto;
    flex: 1;
    min-height: 200px;
    position: relative;
  }

  .step-content {
    position: relative;
  }

  .step-instruction-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: var(--space-3);
  }

  .step-instruction {
    margin: 0 0 var(--space-3);
    font-size: var(--font-size-md);
    color: var(--text-secondary);
  }

  .step-instruction-row .step-instruction {
    margin: 0;
  }

  .select-all-btn {
    background: none;
    border: 1px solid var(--border-color);
    color: var(--primary-on-surface);
    font-size: var(--font-size-xs);
    padding: var(--space-1) var(--space-2-5);
    border-radius: var(--radius-sm);
    cursor: pointer;
    white-space: nowrap;
    transition: background-color var(--duration-fast), border-color var(--duration-fast);
  }

  .select-all-btn:hover {
    background-color: var(--surface-color);
    border-color: var(--primary-color);
  }

  /* Stage Groups */
  .stage-group {
    margin-bottom: var(--space-4);
  }

  .stage-group:last-of-type {
    margin-bottom: 0;
  }

  .stage-group-header {
    display: flex;
    align-items: center;
    gap: var(--space-3);
    margin-bottom: var(--space-2);
  }

  .stage-group-label {
    font-size: var(--font-size-xs);
    font-weight: var(--font-weight-medium);
    color: var(--text-secondary);
    white-space: nowrap;
  }

  .stage-group-line {
    flex: 1;
    height: 1px;
    background: var(--border-color);
  }

  /* Stage Items */
  .stage-item {
    display: flex;
    align-items: flex-start;
    gap: var(--space-3);
    padding: var(--space-2-5) var(--space-3);
    border-radius: var(--radius-lg);
    cursor: pointer;
    transition: background-color var(--duration-fast) ease;
    margin-bottom: var(--space-1);
  }

  .stage-item:hover:not(.disabled) {
    background: var(--surface-color);
  }

  .stage-item.selected:not(.disabled) {
    background: rgba(var(--accent-rgb), 0.06);
  }

  .stage-item.disabled {
    cursor: not-allowed;
    opacity: 0.55;
  }

  /* Custom checkbox */
  .stage-checkbox-area {
    position: relative;
    flex-shrink: 0;
    width: 18px;
    height: 18px;
    margin-top: 1px;
  }

  .stage-checkbox {
    position: absolute;
    opacity: 0;
    width: 18px;
    height: 18px;
    cursor: pointer;
    z-index: var(--z-base);
    margin: 0;
  }

  .stage-checkbox:disabled {
    cursor: not-allowed;
  }

  .stage-check-custom {
    width: 18px;
    height: 18px;
    border: 2px solid var(--border-color);
    border-radius: var(--radius-sm);
    display: flex;
    align-items: center;
    justify-content: center;
    transition: all var(--duration-fast) ease;
    background: var(--background-color);
  }

  .stage-check-custom.checked {
    background: var(--primary-color);
    border-color: var(--primary-color);
    color: var(--text-on-solid);
  }

  .stage-check-custom.disabled {
    background: var(--surface-color);
    border-color: var(--border-color);
    opacity: 0.6;
  }

  /* Stage info */
  .stage-info {
    display: flex;
    flex-direction: column;
    gap: var(--space-0-5);
    min-width: 0;
  }

  .stage-label {
    font-size: var(--font-size-md);
    font-weight: var(--font-weight-medium);
    color: var(--text-color);
    line-height: 1.3;
  }

  .stage-desc {
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
    line-height: 1.4;
  }

  .stage-disabled-hint {
    font-size: var(--font-size-xs);
    color: var(--warning-text);
    font-style: italic;
    line-height: 1.3;
  }

  /* Review step */
  .review-section {
    margin-bottom: var(--space-4);
  }

  .review-label {
    font-size: var(--font-size-sm);
    font-weight: var(--font-weight-medium);
    color: var(--text-secondary);
    display: block;
    margin-bottom: var(--space-2);
  }

  .review-pills {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-1-5);
  }

  .stage-pill {
    display: inline-flex;
    align-items: center;
    padding: var(--space-1) var(--space-3);
    border-radius: var(--radius-pill);
    font-size: var(--font-size-sm);
    font-weight: var(--font-weight-medium);
    background: rgba(var(--accent-rgb), 0.1);
    color: var(--primary-on-surface);
    border: 1px solid rgba(var(--accent-rgb), 0.2);
  }

  .review-settings {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2);
  }

  .review-setting {
    font-size: var(--font-size-md);
    color: var(--text-color);
  }

  .review-setting.muted {
    color: var(--text-secondary);
    font-style: italic;
  }

  /* V4 Info Card */
  .v4-info-card {
    margin-top: var(--space-4);
    border: 1px solid rgba(var(--accent-rgb), 0.2);
    border-radius: var(--radius-lg);
    overflow: hidden;
    background: rgba(var(--accent-rgb), 0.03);
  }

  .v4-info-header {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    padding: var(--space-2-5) var(--space-3);
    font-size: var(--font-size-sm);
    font-weight: var(--font-weight-semibold);
    color: var(--primary-on-surface);
    background: rgba(var(--accent-rgb), 0.06);
    border-bottom: 1px solid rgba(var(--accent-rgb), 0.1);
  }

  .v4-info-header :global(svg) {
    flex-shrink: 0;
  }

  .v4-path {
    padding: var(--space-2-5) var(--space-3);
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
  }

  .v4-path strong {
    font-size: var(--font-size-md);
    color: var(--text-color);
  }

  .v4-path span {
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
    line-height: 1.4;
  }

  /* Warning banners */
  .warning-banner {
    display: flex;
    align-items: flex-start;
    gap: var(--space-2);
    padding: var(--space-2-5) var(--space-3);
    border-radius: var(--radius-md);
    font-size: var(--font-size-sm);
    line-height: 1.45;
    margin-top: var(--space-3);
  }

  .warning-banner :global(svg) {
    flex-shrink: 0;
    margin-top: 1px;
  }

  .warning-destructive {
    background-color: rgba(var(--error-color-rgb), 0.08);
    border: 1px solid rgba(var(--error-color-rgb), 0.2);
    color: var(--text-secondary);
  }

  .warning-destructive :global(svg) {
    color: var(--error-text);
  }

  .warning-caution {
    background-color: rgba(var(--warning-color-rgb), 0.08);
    border: 1px solid rgba(var(--warning-color-rgb), 0.2);
    color: var(--text-secondary);
  }

  .warning-caution :global(svg) {
    color: var(--warning-text);
  }

  .warning-info {
    background-color: rgba(var(--accent-rgb), 0.06);
    border: 1px solid rgba(var(--accent-rgb), 0.15);
    color: var(--text-secondary);
  }

  .warning-info :global(svg) {
    color: var(--primary-on-surface);
  }

  /* Speaker Settings */
  .speaker-settings-section {
    margin-top: 0;
  }

  .settings-row {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: var(--space-3);
    margin-bottom: var(--space-3);
  }

  .setting-field {
    display: flex;
    flex-direction: column;
    gap: var(--space-1-5);
  }

  .setting-field label {
    font-size: var(--font-size-sm);
    font-weight: var(--font-weight-medium);
    color: var(--text-color);
  }

  .setting-field .hint {
    font-weight: var(--font-weight-regular);
    color: var(--text-secondary);
    font-size: var(--font-size-xs);
  }

  .setting-field input:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .setting-field input::placeholder {
    color: var(--text-secondary);
  }

  .model-hint-text {
    margin: var(--space-1-5) 0 0;
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
    line-height: 1.4;
  }

  .validation-error {
    padding: var(--space-1-5) var(--space-2-5);
    background-color: var(--error-bg);
    border: 1px solid var(--error-border);
    border-radius: var(--radius-md);
    color: var(--error-text);
    font-size: var(--font-size-xs);
    margin-top: var(--space-2);
  }

  /* Dark mode */
  :global([data-theme='dark']) .stage-pill {
    background: rgba(var(--accent-rgb), 0.15);
  }

  /* Responsive design */
  @media (max-width: 768px) {
    .btn-lg {
      width: 100%;
      min-height: 44px;
    }

    .settings-row {
      grid-template-columns: 1fr;
    }

    .step-label {
      display: none;
    }

    .step-line {
      min-width: 30px;
    }

    .step-indicator {
      padding: var(--space-3) var(--space-4);
    }

    .modal-body {
      padding: var(--space-4);
    }

    .stage-item {
      padding: var(--space-3);
    }

    .setting-field input {
      min-height: 44px;
      font-size: var(--font-size-base);
    }
  }

  /* Reduced motion support */
  @media (prefers-reduced-motion: reduce) {
    .btn-lg,
    .stage-item,
    .stage-check-custom,
    .step-dot {
      transition: none;
    }
  }
</style>
