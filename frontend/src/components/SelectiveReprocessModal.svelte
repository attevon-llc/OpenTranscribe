<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { fly } from 'svelte/transition';
  import { cubicOut } from 'svelte/easing';
  import { t } from '$stores/locale';
  import { isLLMAvailable, llmStatusStore } from '../stores/llmStatus';
  import axiosInstance from '../lib/axios';
  import { toastStore } from '../stores/toast';
  import Spinner from './ui/Spinner.svelte';
  import BaseModal from './ui/BaseModal.svelte';
  import { ASRSettingsApi } from '$lib/api/asrSettings';

  export let showModal: boolean = false;
  export let file: any = null;
  export let reprocessing: boolean = false;
  export let bulkMode: boolean = false;
  export let bulkFiles: any[] = [];

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

  // ASR provider info
  let isCloudASR = false;
  let activeASRProvider = 'local';
  let activeASRModel = '';

  // Model selection for reprocessing
  let selectedReprocessModel: string | null = null;
  let adminDefaultModel = '';

  // Computed state
  // In bulk mode, gallery file objects use the list schema (no transcript_segments/total_segments).
  // A completed file always has a transcript, so use status as the indicator.
  // For mixed selections (completed + error), enable stages if ANY file qualifies.
  $: hasTranscript = bulkMode
    ? bulkFiles.some((f: any) => f.status === 'completed')
    : file?.status === 'completed' && ((file?.transcript_segments?.length ?? 0) > 0 || (file?.total_segments ?? 0) > 0);
  $: hasWordTimestamps = bulkMode
    ? bulkFiles.some((f: any) => f.status === 'completed')
    : file?.status === 'completed' && ((file?.transcript_segments?.length ?? 0) > 0 || (file?.total_segments ?? 0) > 0);
  $: showSpeakerSettings =
    (selectedStages.has('transcription') || selectedStages.has('rediarize')) && !isCloudASR;
  $: isValid =
    selectedStages.size > 0 &&
    (!showSpeakerSettings ||
      !(minSpeakers !== null && maxSpeakers !== null && minSpeakers > maxSpeakers));

  // Step navigation — show settings step for speaker config OR model selection
  $: needsSettingsStep = showSpeakerSettings || selectedStages.has('transcription');
  $: stepLabels = needsSettingsStep
    ? ['reprocess.stepStages', 'reprocess.stepSettings', 'reprocess.stepReview']
    : ['reprocess.stepStages', 'reprocess.stepReview'];
  $: totalSteps = stepLabels.length;
  $: isLastStep = currentStep === totalSteps;
  $: canGoNext = currentStep === 1 ? selectedStages.size > 0 : true;

  // Validation guards
  $: if (minSpeakers !== null && minSpeakers < 1) minSpeakers = 1;
  $: if (maxSpeakers !== null && maxSpeakers < 1) maxSpeakers = 1;
  $: if (numSpeakers !== null && numSpeakers < 1) numSpeakers = 1;

  // Stage definitions
  interface StageDefinition {
    id: string;
    labelKey: string;
    descKey: string;
    disabled: boolean;
    disabledReason: string;
  }

  $: coreStages = [
    {
      id: 'transcription',
      labelKey: 'reprocess.stageTranscription',
      descKey: 'reprocess.stageTranscriptionDesc',
      disabled: false,
      disabledReason: '',
    },
    {
      id: 'rediarize',
      labelKey: 'reprocess.stageRediarize',
      descKey: 'reprocess.stageRediarizeDesc',
      disabled: !hasWordTimestamps || isCloudASR,
      disabledReason: isCloudASR ? $t('reprocess.rediarizeCloudDisabled') : $t('reprocess.stageRediarizeDisabled'),
    },
  ] as StageDefinition[];

  $: searchStages = [
    {
      id: 'search_indexing',
      labelKey: 'reprocess.stageSearchIndexing',
      descKey: 'reprocess.stageSearchIndexingDesc',
      disabled: !hasTranscript,
      disabledReason: $t('reprocess.transcriptRequired'),
    },
    {
      id: 'analytics',
      labelKey: 'reprocess.stageAnalytics',
      descKey: 'reprocess.stageAnalyticsDesc',
      disabled: !hasTranscript,
      disabledReason: $t('reprocess.transcriptRequired'),
    },
  ] as StageDefinition[];

  $: aiStages = [
    {
      id: 'speaker_llm',
      labelKey: 'reprocess.stageSpeakerLLM',
      descKey: 'reprocess.stageSpeakerLLMDesc',
      disabled: !hasTranscript || !$isLLMAvailable,
      disabledReason: !hasTranscript
        ? $t('reprocess.transcriptRequired')
        : $t('reprocess.llmNotAvailable'),
    },
    {
      id: 'summarization',
      labelKey: 'reprocess.stageSummarization',
      descKey: 'reprocess.stageSummarizationDesc',
      disabled: !hasTranscript || !$isLLMAvailable,
      disabledReason: !hasTranscript
        ? $t('reprocess.transcriptRequired')
        : $t('reprocess.llmNotAvailable'),
    },
    {
      id: 'topic_extraction',
      labelKey: 'reprocess.stageTopicExtraction',
      descKey: 'reprocess.stageTopicExtractionDesc',
      disabled: !hasTranscript || !$isLLMAvailable,
      disabledReason: !hasTranscript
        ? $t('reprocess.transcriptRequired')
        : $t('reprocess.llmNotAvailable'),
    },
  ] as StageDefinition[];

  $: allStages = [...coreStages, ...searchStages, ...aiStages];

  function toggleStage(stageId: string) {
    const newSet = new Set(selectedStages);

    if (newSet.has(stageId)) {
      newSet.delete(stageId);
    } else {
      newSet.add(stageId);
      if (stageId === 'transcription') {
        newSet.delete('rediarize');
      } else if (stageId === 'rediarize') {
        newSet.delete('transcription');
      }
    }

    selectedStages = newSet;
  }

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
      const requestBody: Record<string, unknown> = {
        stages: stagesArray,
      };

      if (showSpeakerSettings) {
        if (minSpeakers !== null) requestBody.min_speakers = minSpeakers;
        if (maxSpeakers !== null) requestBody.max_speakers = maxSpeakers;
        if (numSpeakers !== null) requestBody.num_speakers = numSpeakers;
      }

      if (selectedReprocessModel) {
        requestBody.whisper_model = selectedReprocessModel;
      }

      if (bulkMode) {
        // Bulk mode: POST to bulk-action endpoint
        requestBody.file_uuids = bulkFiles.map((f: any) => f.uuid);
        requestBody.action = 'reprocess';

        const response = await axiosInstance.post(
          '/files/management/bulk-action',
          requestBody
        );
        const results = response.data;
        const successful = results.filter((r: any) => r.success);
        const failed = results.filter((r: any) => !r.success);

        if (successful.length > 0) {
          toastStore.success($t('reprocess.bulkStartedSuccess', { count: successful.length }));
        }
        if (failed.length > 0) {
          toastStore.error($t('reprocess.bulkStartFailed', { count: failed.length }));
        }

        dispatch('reprocess', { stages: stagesArray, count: successful.length });
      } else {
        // Single file mode
        await axiosInstance.post(
          `/files/${file.uuid}/reprocess`,
          requestBody
        );

        toastStore.success($t('reprocess.startedSuccess'));
        dispatch('reprocess', {
          fileId: file.uuid,
          stages: stagesArray,
        });
      }

      showModal = false;
    } catch (error) {
      console.error('Error reprocessing:', error);
      if (bulkMode) {
        toastStore.error($t('reprocess.bulkStartFailed', { count: bulkFiles.length }));
      } else {
        toastStore.error($t('reprocess.startFailed'));
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

  // Reset state and fetch ASR status when modal opens
  $: if (showModal) {
    resetState();
    selectedReprocessModel = null;
    // Ensure LLM status is loaded (may not be ready if user navigated quickly)
    llmStatusStore.initialize();
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

  function handleClose() {
    dispatch('close');
    showModal = false;
  }

  function selectAll() {
    const newSet = new Set<string>();
    for (const stage of allStages) {
      if (!stage.disabled) {
        // Transcription subsumes rediarize — pick transcription
        if (stage.id === 'rediarize') continue;
        newSet.add(stage.id);
      }
    }
    selectedStages = newSet;
  }

  function deselectAll() {
    selectedStages = new Set();
  }

  $: hasDiarizationDisabled = bulkMode
    ? bulkFiles.some((f: any) => f.diarization_disabled)
    : file?.diarization_disabled === true;

  $: allSelected = allStages.filter(s => !s.disabled && s.id !== 'rediarize').every(s => selectedStages.has(s.id));

  function getStageLabelById(id: string): string {
    const stage = allStages.find(s => s.id === id);
    return stage ? $t(stage.labelKey) : id;
  }
</script>

<BaseModal isOpen={showModal} title={bulkMode ? $t('reprocess.bulkModalTitle', { count: bulkFiles.length }) : $t('reprocess.modalTitle')} maxWidth="540px" zIndex={9999} onClose={handleClose}>
        <!-- Step Indicator -->
        <div class="step-indicator">
          {#each stepLabels as label, i}
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
                  <div class="warning-banner warning-info" style="margin-top: 0; margin-bottom: 0.75rem;">
                    <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                      <circle cx="12" cy="12" r="10"></circle>
                      <line x1="12" y1="16" x2="12" y2="12"></line>
                      <line x1="12" y1="8" x2="12.01" y2="8"></line>
                    </svg>
                    <span>{$t('reprocess.diarizationWasDisabled')}</span>
                  </div>
                {/if}

                <!-- Core Processing -->
                <div class="stage-group">
                  <div class="stage-group-header">
                    <span class="stage-group-label">{$t('reprocess.coreProcessing')}</span>
                    <div class="stage-group-line"></div>
                  </div>
                  {#each coreStages as stage (stage.id)}
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
                          on:change={() => toggleStage(stage.id)}
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

                <!-- Search & Discovery -->
                <div class="stage-group">
                  <div class="stage-group-header">
                    <span class="stage-group-label">{$t('reprocess.searchDiscovery')}</span>
                    <div class="stage-group-line"></div>
                  </div>
                  {#each searchStages as stage (stage.id)}
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
                          on:change={() => toggleStage(stage.id)}
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

                <!-- AI Features -->
                <div class="stage-group">
                  <div class="stage-group-header">
                    <span class="stage-group-label">{$t('reprocess.aiFeatures')}</span>
                    <div class="stage-group-line"></div>
                  </div>
                  {#each aiStages as stage (stage.id)}
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
                          on:change={() => toggleStage(stage.id)}
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

              <!-- Step 2: Settings (only when needsSettingsStep && currentStep === 2) -->
              {:else if isOnSettingsStep()}
                <!-- Model Selection -->
                {#if selectedStages.has('transcription')}
                  <div class="setting-field" style="margin-bottom: 1rem;">
                    <label for="reprocess-model-select">
                      {$t('uploader.whisperModel')}
                    </label>
                    <select id="reprocess-model-select" bind:value={selectedReprocessModel} class="model-select">
                      <option value={null}>
                        {$t('uploader.highQuality')} ({adminDefaultModel})
                      </option>
                      <option value="base">
                        {$t('uploader.fastProcessing')}
                      </option>
                    </select>
                    {#if selectedReprocessModel === 'base'}
                      <p class="model-hint-text">{$t('uploader.fastProcessingHint')}</p>
                    {/if}
                  </div>
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
                    <div class="v4-info-card">
                      <div class="v4-info-header">
                        <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                          <circle cx="12" cy="12" r="10"></circle>
                          <line x1="12" y1="16" x2="12" y2="12"></line>
                          <line x1="12" y1="8" x2="12.01" y2="8"></line>
                        </svg>
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
                  {/if}
                </div>
                {/if}

              <!-- Step 3 (or Step 2 when no settings): Review -->
              {:else if isOnReviewStep()}
                <p class="step-instruction">{$t('reprocess.reviewTitle')}</p>

                <!-- Bulk info banner -->
                {#if bulkMode}
                  <div class="warning-banner warning-info" style="margin-top: 0; margin-bottom: 0.75rem;">
                    <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                      <circle cx="12" cy="12" r="10"></circle>
                      <line x1="12" y1="16" x2="12" y2="12"></line>
                      <line x1="12" y1="8" x2="12.01" y2="8"></line>
                    </svg>
                    <span>{$t('reprocess.bulkInfoMessage', { count: bulkFiles.length })}</span>
                  </div>
                {/if}

                <!-- Selected stages -->
                <div class="review-section">
                  <span class="review-label">{$t('reprocess.reviewStages')}</span>
                  <div class="review-pills">
                    {#each Array.from(selectedStages) as stageId}
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
                  <div class="v4-info-card">
                    <div class="v4-info-header">
                      <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                        <circle cx="12" cy="12" r="10"></circle>
                        <line x1="12" y1="16" x2="12" y2="12"></line>
                        <line x1="12" y1="8" x2="12.01" y2="8"></line>
                      </svg>
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
                    <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                      <circle cx="12" cy="12" r="10"></circle>
                      <line x1="12" y1="16" x2="12" y2="12"></line>
                      <line x1="12" y1="8" x2="12.01" y2="8"></line>
                    </svg>
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
              class="modal-button cancel-button"
              on:click={goBack}
              type="button"
            >
              {$t('reprocess.back')}
            </button>
          {:else}
            <button
              class="modal-button cancel-button"
              on:click={handleClose}
              type="button"
            >
              {$t('reprocess.cancel')}
            </button>
          {/if}

          {#if isLastStep}
            <button
              class="modal-button primary-button"
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
              class="modal-button primary-button"
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

<style>
  /* Step Indicator */
  .step-indicator {
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 1rem 1.5rem;
    gap: 0;
    border-bottom: 1px solid var(--border-color);
    flex-shrink: 0;
  }

  .step-dot {
    width: 28px;
    height: 28px;
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 0.75rem;
    font-weight: 600;
    border: 2px solid var(--border-color);
    background: var(--background-color);
    color: var(--text-secondary);
    cursor: default;
    transition: all 0.25s ease;
    flex-shrink: 0;
    padding: 0;
  }

  .step-dot.active {
    border-color: var(--primary-color, #3b82f6);
    background: #3b82f6;
    color: white;
  }

  .step-dot.completed {
    border-color: var(--primary-color, #3b82f6);
    background: #3b82f6;
    color: white;
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
    font-size: 0.72rem;
    font-weight: 500;
    color: var(--text-secondary);
    margin-left: 0.35rem;
    white-space: nowrap;
    transition: color 0.2s ease;
  }

  .step-label.active {
    color: var(--primary-color, #3b82f6);
    font-weight: 600;
  }

  .step-label.completed {
    color: var(--primary-color, #3b82f6);
  }

  .step-line {
    flex: 1;
    height: 2px;
    background: var(--border-color);
    margin: 0 0.5rem;
    min-width: 20px;
    max-width: 60px;
    transition: background 0.25s ease;
  }

  .step-line.completed {
    background: #3b82f6;
  }

  /* Body */
  .modal-body {
    padding: 1rem 1.5rem 1.25rem;
    overflow-y: auto;
    overflow-x: hidden;
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
    margin-bottom: 0.75rem;
  }

  .step-instruction-row .step-instruction {
    margin: 0;
  }

  .step-instruction {
    margin: 0 0 0.75rem;
    font-size: 0.85rem;
    color: var(--text-secondary);
  }

  .select-all-btn {
    background: none;
    border: 1px solid var(--border-color);
    color: var(--primary-color);
    font-size: 0.75rem;
    padding: 0.25rem 0.6rem;
    border-radius: 4px;
    cursor: pointer;
    white-space: nowrap;
    transition: background-color 0.15s, border-color 0.15s;
  }

  .select-all-btn:hover {
    background-color: var(--surface-color);
    border-color: var(--primary-color);
  }

  /* Stage Groups */
  .stage-group {
    margin-bottom: 1rem;
  }

  .stage-group:last-of-type {
    margin-bottom: 0;
  }

  .stage-group-header {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    margin-bottom: 0.5rem;
  }

  .stage-group-label {
    font-size: 0.7rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.05em;
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
    gap: 0.75rem;
    padding: 0.6rem 0.75rem;
    border-radius: 8px;
    cursor: pointer;
    transition: background-color 0.15s ease;
    margin-bottom: 0.25rem;
  }

  .stage-item:hover:not(.disabled) {
    background: var(--surface-color);
  }

  .stage-item.selected:not(.disabled) {
    background: rgba(59, 130, 246, 0.06);
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
    z-index: 1;
    margin: 0;
  }

  .stage-checkbox:disabled {
    cursor: not-allowed;
  }

  .stage-check-custom {
    width: 18px;
    height: 18px;
    border: 2px solid var(--border-color);
    border-radius: 4px;
    display: flex;
    align-items: center;
    justify-content: center;
    transition: all 0.15s ease;
    background: var(--background-color);
  }

  .stage-check-custom.checked {
    background: #3b82f6;
    border-color: var(--primary-color, #3b82f6);
    color: white;
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
    gap: 0.15rem;
    min-width: 0;
  }

  .stage-label {
    font-size: 0.875rem;
    font-weight: 500;
    color: var(--text-primary);
    line-height: 1.3;
  }

  .stage-desc {
    font-size: 0.75rem;
    color: var(--text-secondary);
    line-height: 1.4;
  }

  .stage-disabled-hint {
    font-size: 0.7rem;
    color: #f59e0b;
    font-style: italic;
    line-height: 1.3;
  }

  /* Review step */
  .review-section {
    margin-bottom: 1rem;
  }

  .review-label {
    font-size: 0.78rem;
    font-weight: 600;
    color: var(--text-secondary);
    text-transform: uppercase;
    letter-spacing: 0.03em;
    display: block;
    margin-bottom: 0.5rem;
  }

  .review-pills {
    display: flex;
    flex-wrap: wrap;
    gap: 0.4rem;
  }

  .stage-pill {
    display: inline-flex;
    align-items: center;
    padding: 0.3rem 0.7rem;
    border-radius: 20px;
    font-size: 0.78rem;
    font-weight: 500;
    background: rgba(59, 130, 246, 0.1);
    color: var(--primary-color, #3b82f6);
    border: 1px solid rgba(59, 130, 246, 0.2);
  }

  .review-settings {
    display: flex;
    flex-wrap: wrap;
    gap: 0.5rem;
  }

  .review-setting {
    font-size: 0.85rem;
    color: var(--text-primary);
  }

  .review-setting.muted {
    color: var(--text-secondary);
    font-style: italic;
  }

  /* V4 Info Card */
  .v4-info-card {
    margin-top: 1rem;
    border: 1px solid rgba(59, 130, 246, 0.2);
    border-radius: 8px;
    overflow: hidden;
    background: rgba(59, 130, 246, 0.03);
  }

  .v4-info-header {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.6rem 0.75rem;
    font-size: 0.8rem;
    font-weight: 600;
    color: var(--primary-color, #3b82f6);
    background: rgba(59, 130, 246, 0.06);
    border-bottom: 1px solid rgba(59, 130, 246, 0.1);
  }

  .v4-info-header svg {
    flex-shrink: 0;
  }

  .v4-path {
    padding: 0.6rem 0.75rem;
    display: flex;
    flex-direction: column;
    gap: 0.2rem;
  }

  .v4-path strong {
    font-size: 0.82rem;
    color: var(--text-primary);
  }

  .v4-path span {
    font-size: 0.75rem;
    color: var(--text-secondary);
    line-height: 1.4;
  }

  /* Warning banners */
  .warning-banner {
    display: flex;
    align-items: flex-start;
    gap: 0.5rem;
    padding: 0.6rem 0.75rem;
    border-radius: 6px;
    font-size: 0.78rem;
    line-height: 1.45;
    margin-top: 0.75rem;
  }

  .warning-banner svg {
    flex-shrink: 0;
    margin-top: 1px;
  }

  .warning-destructive {
    background-color: rgba(239, 68, 68, 0.08);
    border: 1px solid rgba(239, 68, 68, 0.2);
    color: var(--text-secondary);
  }

  .warning-destructive svg {
    color: #ef4444;
  }

  .warning-caution {
    background-color: rgba(245, 158, 11, 0.08);
    border: 1px solid rgba(245, 158, 11, 0.2);
    color: var(--text-secondary);
  }

  .warning-caution svg {
    color: #f59e0b;
  }

  .warning-info {
    background-color: rgba(59, 130, 246, 0.06);
    border: 1px solid rgba(59, 130, 246, 0.15);
    color: var(--text-secondary);
  }

  .warning-info svg {
    color: var(--primary-color);
  }

  /* Speaker Settings */
  .speaker-settings-section {
    margin-top: 0;
  }

  .settings-row {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 0.75rem;
    margin-bottom: 0.75rem;
  }

  .setting-field {
    display: flex;
    flex-direction: column;
    gap: 0.35rem;
  }

  .setting-field label {
    font-size: 0.8rem;
    font-weight: 500;
    color: var(--text-primary);
  }

  .setting-field .hint {
    font-weight: 400;
    color: var(--text-secondary);
    font-size: 0.7rem;
  }

  .setting-field input {
    padding: 0.45rem 0.6rem;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    background-color: var(--input-background, var(--card-background));
    color: var(--text-primary);
    font-size: 0.85rem;
  }

  .setting-field input:focus {
    outline: none;
    border-color: var(--primary-color);
    box-shadow: 0 0 0 2px rgba(59, 130, 246, 0.1);
  }

  .setting-field input:disabled {
    opacity: 0.5;
    cursor: not-allowed;
    background-color: var(--disabled-background, #f5f5f5);
  }

  .setting-field input::placeholder {
    color: var(--text-muted);
  }

  .model-select {
    width: 100%;
    padding: 0.5rem 0.75rem;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    background-color: var(--input-background, var(--card-background));
    color: var(--text-primary);
    font-size: 0.85rem;
    cursor: pointer;
  }

  .model-select:focus {
    outline: none;
    border-color: var(--primary-color);
    box-shadow: 0 0 0 2px rgba(59, 130, 246, 0.1);
  }

  .model-hint-text {
    margin: 0.35rem 0 0;
    font-size: 0.78rem;
    color: var(--text-secondary);
    line-height: 1.4;
  }

  .validation-error {
    padding: 0.4rem 0.6rem;
    background-color: rgba(239, 68, 68, 0.1);
    border: 1px solid rgba(239, 68, 68, 0.3);
    border-radius: 6px;
    color: #dc2626;
    font-size: 0.75rem;
    margin-top: 0.5rem;
  }

  .modal-button {
    padding: 0.55rem 1.1rem;
    border: none;
    border-radius: 10px;
    font-size: 0.9rem;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s ease;
    min-width: 100px;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 0.5rem;
  }

  .cancel-button {
    background: var(--card-background);
    color: var(--text-color);
    border: 1px solid var(--border-color);
    box-shadow: 0 2px 4px rgba(0, 0, 0, 0.05);
  }

  .cancel-button:hover {
    background: var(--button-hover);
    transform: scale(1.02);
    box-shadow: 0 4px 8px rgba(0, 0, 0, 0.08);
  }

  .cancel-button:active {
    transform: scale(1);
  }

  .primary-button {
    background: #3b82f6;
    color: white;
    box-shadow: 0 2px 4px rgba(59, 130, 246, 0.2);
  }

  .primary-button:hover:not(:disabled) {
    background: #2563eb;
    transform: scale(1.02);
    box-shadow: 0 4px 8px rgba(var(--primary-color-rgb, 59, 130, 246), 0.3);
  }

  .primary-button:active:not(:disabled) {
    transform: scale(1);
  }

  .primary-button:disabled {
    opacity: 0.6;
    cursor: not-allowed;
    transform: none;
  }

  /* Dark mode */
  :global([data-theme='dark']) .cancel-button:hover {
    background: var(--button-hover);
    color: var(--text-color);
    border-color: var(--border-color);
  }

  :global([data-theme='dark']) .setting-field input:disabled {
    background-color: rgba(255, 255, 255, 0.05);
  }

  :global([data-theme='dark']) .validation-error {
    background-color: rgba(239, 68, 68, 0.15);
    color: #f87171;
  }

  :global([data-theme='dark']) .stage-pill {
    background: rgba(59, 130, 246, 0.15);
  }

  /* Responsive design */
  @media (max-width: 768px) {
    .modal-button {
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
      padding: 0.75rem 1rem;
    }

    .modal-body {
      padding: 1rem;
    }

    .stage-item {
      padding: 0.75rem;
    }

    .setting-field input,
    .setting-field select,
    .model-select {
      min-height: 44px;
      font-size: 1rem;
    }
  }

  /* Reduced motion support */
  @media (prefers-reduced-motion: reduce) {
    .modal-button,
    .stage-item,
    .stage-check-custom,
    .step-dot {
      transition: none;
    }
  }

  /* Focus styles */
  .modal-button:focus {
    outline: 2px solid var(--primary-color, #3b82f6);
    outline-offset: 2px;
  }

  .cancel-button:focus {
    outline: 2px solid var(--border-color);
    outline-offset: 2px;
  }
</style>
