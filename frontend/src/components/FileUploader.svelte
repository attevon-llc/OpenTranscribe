<script lang="ts">
  import { createEventDispatcher, onMount, onDestroy, tick } from 'svelte';
  import axiosInstance from '$lib/axios';
  import ConfirmationModal from './ConfirmationModal.svelte';
  import BulkAudioExtractionModal from './BulkAudioExtractionModal.svelte';

  import { recordingStore, recordingManager, hasActiveRecording } from '$stores/recording';
  import { uploadsStore } from '$stores/uploads';
  import type { UploadTranscriptionParams } from '$lib/services/uploadService';
  import { toastStore } from '$stores/toast';
  import { t } from '$stores/locale';
  import { loadEffectivePrivacy } from '$stores/privacyPolicy';
  import { isCloudEdition } from '$lib/edition';
  import { capabilities, isCapabilityEnabled } from '$stores/capabilities';
  import { usageStore, isOverLimit, showQuotaExceeded } from '$lib/cloud';
  import { get } from 'svelte/store';

  import { loadProtectedMediaAuthConfig } from '$lib/services/configService';
  import type { AudioExtractionSettings } from '$lib/api/audioExtractionSettings';
  import {
    type TranscriptionSettings,
    type TranscriptionSystemDefaults,
    isLightweightModel,
    speakerPrefill
  } from '$lib/api/transcriptionSettings';
  import { warrantsLargeUploadWarning } from '$lib/utils/uploadLimits';
  import { listTags } from '$lib/api/tags';

  import {
    defaultSteps, withExtractionStep, withoutExtractionStep, shouldOfferExtraction,
    buildTranscriptionParams, type StepConfig
  } from '$lib/upload/wizardSteps';
  import {
    restorePreviousValues, savePreviousValues as persistPreviousValues,
    loadRecordingSettings, loadAudioExtractionSettings, loadTranscriptionPreferences,
    loadAdminDefaultModel, type UploadPreviousValues
  } from '$lib/upload/uploadPreferences';
  import { startBulkExtraction, extractAndQueue } from '$lib/upload/bulkExtraction';
  import {
    MEDIA_URL_REGEX, validateSelectedFile, partitionMultipleFiles, buildUrlPayload, urlErrorToast
  } from '$lib/upload/submitUpload';

  // Step components
  import MediaFilePanel from './upload/MediaFilePanel.svelte';
  import MediaUrlPanel from './upload/MediaUrlPanel.svelte';
  import MediaRecordPanel from './upload/MediaRecordPanel.svelte';
  import UploadStepTags from './upload/UploadStepTags.svelte';
  import UploadStepCollections from './upload/UploadStepCollections.svelte';
  import UploadStepSpeakers from './upload/UploadStepSpeakers.svelte';
  import UploadStepModel from './upload/UploadStepModel.svelte';
  import UploadStepReview from './upload/UploadStepReview.svelte';
  import UploadStepExtraction from './upload/UploadStepExtraction.svelte';

  // ── Types ──
  interface FileWithSize extends File { size: number; }
  // ── Stepper State ──
  let steps: StepConfig[] = defaultSteps();
  let currentStepIndex = 0;
  let maxStepReached = 0;

  $: activeSteps = steps.filter(s => !s.skipped && (s.id !== 'speakers' || speakerCountApplies));
  $: currentStep = activeSteps[currentStepIndex];
  $: isFirstStep = currentStepIndex === 0;
  $: isLastStep = currentStepIndex === activeSteps.length - 1;
  // The forward button names its destination when that destination is Review, and the
  // "Review with defaults" jump is hidden there: it would be a second button doing the
  // same thing, the duplication item 8 already removed for Skip.
  $: nextIsReview = currentStepIndex === activeSteps.length - 2;
  $: if (currentStepIndex > maxStepReached) maxStepReached = currentStepIndex;

  // ── Tab & Media State ──
  let activeTab: 'file' | 'url' | 'record' = 'file';

  // Deployment-owned controls (the server enforces both; this only hides them).
  $: urlIngestEnabled = isCapabilityEnabled($capabilities, 'url_ingest');
  $: modelChoiceEnabled = isCapabilityEnabled($capabilities, 'transcription.model_choice');
  $: if (!urlIngestEnabled && activeTab === 'url') switchTab('file');
  $: if (!modelChoiceEnabled && selectedWhisperModel !== null) selectedWhisperModel = null;

  let file: FileWithSize | null = null;
  let mediaUrl = '';
  let error = '';
  let isDuplicateFile = false;
  let duplicateFileId: string | null = null;
  let processingUrl = false;

  // ── Speaker Settings ──
  let minSpeakers: number | null = null;
  let maxSpeakers: number | null = null;
  let numSpeakers: number | null = null;
  // The uploader's statement that the speakers agreed to voice identification (issue #1356).
  // Unchecked records nothing. Only offered while voiceprints are on for the caller.
  let voiceprintConsent = false;
  let voiceprintsOn = true;
  let skipSummary = false;
  let selectedWhisperModel: string | null = null;
  let adminDefaultModel = 'large-v3-turbo';
  let transcriptionSettings: TranscriptionSettings | null = null;
  let transcriptionSystemDefaults: TranscriptionSystemDefaults | null = null;

  // The server discards the speaker range when speaker detection is off or the Fast (CPU) model
  // runs, so the step that collects it is not offered rather than collecting a value to drop.
  $: speakerDetectionOff = transcriptionSettings?.diarization_source === 'off';
  $: fastModelChosen = isLightweightModel(selectedWhisperModel);
  $: speakerCountApplies = !speakerDetectionOff && !fastModelChosen;

  // ── Organization State ──
  let selectedCollections: Array<{uuid: string; name: string}> = [];
  let availableCollections: Array<{uuid: string; name: string; media_count?: number}> = [];
  let selectedTags: string[] = [];
  let availableTags: Array<{uuid: string; name: string; usage_count: number}> = [];

  // ── Remember Previous ──
  let rememberValues = true;
  let hasPreviousTags = false;
  let hasPreviousCollections = false;

  // ── Audio Extraction ──
  let audioExtractionSettings: AudioExtractionSettings | null = null;
  let needsExtraction = false;  // true when a large video is detected
  let showBulkAudioExtractionModal = false;
  let bulkVideosToExtract: File[] = [];
  let bulkRegularFiles: File[] = [];

  // ── Recording ──
  let showRecordingWarningModal = false;
  let pendingNavigationAction: (() => void) | null = null;
  let maxRecordingDuration = 7200;
  let recordingQuality = 'high';
  let autoStopEnabled = true;

  // ── URL Panel Ref ──
  let urlPanelRef: MediaUrlPanel;

  // ── Derived ──
  $: recordedBlob = $recordingStore.recordedBlob;
  $: recordingSupported = $recordingStore.recordingSupported;

  $: mediaReady =
    (activeTab === 'file' && file !== null && !isDuplicateFile) ||
    (activeTab === 'url' && mediaUrl.trim() !== '' && MEDIA_URL_REGEX.test(mediaUrl.trim())) ||
    (activeTab === 'record' && recordedBlob !== null);

  $: hasValidationError =
    speakerCountApplies && minSpeakers !== null && maxSpeakers !== null && minSpeakers > maxSpeakers;

  $: tagsSkipped = steps.find(s => s.id === 'tags')?.skipped ?? false;
  $: collectionsSkipped = steps.find(s => s.id === 'collections')?.skipped ?? false;

  // ── Event Dispatcher ──
  const dispatch = createEventDispatcher<{
    uploadComplete: {
      fileId?: string;
      uploadId?: string;
      isDuplicate?: boolean;
      isUrl?: boolean;
      multiple?: boolean;
      count?: number;
      isRecording?: boolean;
      isFile?: boolean;
    };
    uploadError: { error: string };
  }>();

  // ── Lifecycle ──
  let handleSetTabEvent: ((event: Event) => void) | null = null;
  let handleDirectUpload: ((event: Event) => void) | null = null;

  onMount(() => {
    void loadEffectivePrivacy().then((answer) => {
      voiceprintsOn = answer?.voiceprint_matching?.enabled !== false;
    });
    void loadProtectedMediaAuthConfig();
    applyRecordingSettings();

    void loadAudioExtractionSettings().then((v) => { audioExtractionSettings = v; });
    void loadTranscriptionPreferences().then(({ settings, systemDefaults }) => {
      transcriptionSettings = settings;
      transcriptionSystemDefaults = systemDefaults;
      applyTranscriptionPreferences();
    });
    void loadAdminDefaultModel().then((v) => { adminDefaultModel = v; });

    (async () => {
      try {
        const [collectionsRes, tags] = await Promise.all([
          axiosInstance.get('/collections'),
          listTags(),
        ]);
        availableCollections = collectionsRes.data;
        availableTags = tags;
        loadPreviousValues();
      } catch {
        console.error('Failed to load collections/tags');
      }
    })();

    handleSetTabEvent = (event: Event) => {
      const customEvent = event as CustomEvent;
      if (customEvent.detail?.activeTab) {
        if (customEvent.detail.activeTab === 'url' && !urlIngestEnabled) return;
        activeTab = customEvent.detail.activeTab;
        if (currentStepIndex !== 0) { currentStepIndex = 0; }
      }
    };

    handleDirectUpload = (event: Event) => {
      const customEvent = event as CustomEvent;
      if (customEvent.detail?.file) {
        file = customEvent.detail.file;
        activeTab = 'file';
        setTimeout(() => uploadFile(), 100);
      }
    };

    window.addEventListener('setFileUploaderTab', handleSetTabEvent);
    window.addEventListener('directFileUpload', handleDirectUpload);
  });

  onDestroy(() => {
    if (handleSetTabEvent) window.removeEventListener('setFileUploaderTab', handleSetTabEvent);
    if (handleDirectUpload) window.removeEventListener('directFileUpload', handleDirectUpload);
  });

  // ── Transcription Preferences ──
  function applyTranscriptionPreferences() {
    if (!transcriptionSettings) return;
    ({ minSpeakers, maxSpeakers } = speakerPrefill(transcriptionSettings));
  }

  function getTranscriptionParams(): UploadTranscriptionParams {
    return buildTranscriptionParams({
      speakerCountApplies, transcriptionSettings, transcriptionSystemDefaults,
      minSpeakers, maxSpeakers, numSpeakers, selectedWhisperModel, skipSummary,
      voiceprintConsent, voiceprintsOn
    });
  }

  function getOrganizeParams() {
    return {
      collectionIds: selectedCollections.length > 0 ? selectedCollections.map(c => c.uuid) : undefined,
      tagNames: selectedTags.length > 0 ? [...selectedTags] : undefined,
    };
  }

  // ── Recording Settings ──
  function applyRecordingSettings() {
    ({ maxRecordingDuration, recordingQuality, autoStopEnabled } = loadRecordingSettings());
  }

  // ── Remember Previous Values ──
  function loadPreviousValues() {
    const prev = restorePreviousValues(availableCollections, steps);
    if (!prev) return;
    if (prev.tags) { selectedTags = prev.tags; hasPreviousTags = true; }
    if (prev.collections) { selectedCollections = prev.collections; hasPreviousCollections = true; }
    if (prev.minSpeakers !== undefined) minSpeakers = prev.minSpeakers;
    if (prev.maxSpeakers !== undefined) maxSpeakers = prev.maxSpeakers;
    if (prev.numSpeakers !== undefined) numSpeakers = prev.numSpeakers;
    if (prev.skipSummary) skipSummary = prev.skipSummary;
    if (prev.selectedWhisperModel !== undefined) selectedWhisperModel = prev.selectedWhisperModel;
    if (prev.steps) steps = prev.steps;
    rememberValues = true;
  }

  function savePreviousValues() {
    if (!rememberValues) return;
    const skippedSteps = steps.filter(s => s.skipped).map(s => s.id);
    const values: UploadPreviousValues = {
      collectionIds: selectedCollections.map(c => c.uuid),
      collectionNames: selectedCollections.map(c => c.name),
      tagNames: [...selectedTags],
      minSpeakers, maxSpeakers, numSpeakers, skipSummary, selectedWhisperModel,
      skippedSteps,
      timestamp: Date.now(),
    };
    persistPreviousValues(values);
  }

  function clearPreviousTags() {
    selectedTags = [];
    hasPreviousTags = false;
  }

  function clearPreviousCollections() {
    selectedCollections = [];
    hasPreviousCollections = false;
  }

  // ── Step Navigation ──
  function goNext() {
    if (currentStepIndex < activeSteps.length - 1) {
      currentStepIndex += 1;
      void tick().then(focusStepContent);
    }
  }

  function goBack() {
    if (currentStepIndex > 0) {
      currentStepIndex -= 1;
      void tick().then(focusStepContent);
    }
  }

  function goToStep(index: number) {
    if (index === currentStepIndex) return;
    currentStepIndex = index;
    void tick().then(focusStepContent);
  }

  function skipToReview() {
    const reviewIndex = activeSteps.length - 1;
    maxStepReached = reviewIndex;
    currentStepIndex = reviewIndex;
    void tick().then(focusStepContent);
  }

  function focusStepContent() {
    const el = document.querySelector('.step-body');
    if (el) {
      const firstInput = el.querySelector<HTMLElement>('input, select, button, textarea');
      firstInput?.focus();
    }
  }

  function handleStepKeydown(e: KeyboardEvent) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      if (isLastStep) { handleSubmit(); }
      else if (currentStep?.id === 'media' && mediaReady) { goNext(); }
      else if (currentStep?.optional || currentStepIndex > 0) { goNext(); }
    }
  }

  // ── Tab Switching ──
  function switchTab(tab: 'file' | 'url' | 'record') {
    if (tab === activeTab) return;
    activeTab = tab;
    if (tab === 'file') { mediaUrl = ''; processingUrl = false; }
    else if (tab === 'url') { file = null; error = ''; isDuplicateFile = false; }
    else if (tab === 'record') { file = null; error = ''; isDuplicateFile = false; mediaUrl = ''; processingUrl = false; }
  }

  // ── File Handling (coordinator) ──
  function handleFileSelect(event: CustomEvent<{ file: File }>) {
    const selectedFile = event.detail.file;
    error = '';
    isDuplicateFile = false;
    duplicateFileId = null;

    const verdict = validateSelectedFile(selectedFile);
    error = verdict.error;
    if (!verdict.ok) {
      file = null;
      return;
    }

    file = selectedFile as FileWithSize;

    // Check if large video needs extraction step
    const offerExtraction = shouldOfferExtraction(selectedFile, audioExtractionSettings);

    if (offerExtraction && !needsExtraction) {
      needsExtraction = true;
      steps = withExtractionStep(steps);
    } else if (!offerExtraction && needsExtraction) {
      // Remove extraction step if file changed to non-video
      needsExtraction = false;
      steps = withoutExtractionStep(steps);
    }
  }

  function handleMultipleFiles(event: CustomEvent<{ files: File[] }>) {
    const files = event.detail.files;
    const { validFiles, invalidFiles } = partitionMultipleFiles(files);

    if (invalidFiles.length > 0) {
      toastStore.error($t('uploader.skippedInvalidFiles', { count: invalidFiles.length, files: invalidFiles.join('\n') }));
    }

    // Same soft warning the single-file path gives — large files are accepted, just slow.
    const largeFiles = validFiles.filter(f => warrantsLargeUploadWarning(f.size));
    if (largeFiles.length > 0) {
      toastStore.warning($t('uploader.largeFilesWarning', { count: largeFiles.length }));
    }

    if (validFiles.length > 0) {
      const { collectionIds, tagNames } = getOrganizeParams();
      uploadsStore.addFiles(validFiles, collectionIds, tagNames, getTranscriptionParams());
      dispatch('uploadComplete', { multiple: true, count: validFiles.length });
      toastStore.success($t('uploader.addedToQueueOnly', { count: validFiles.length }));
      savePreviousValues();
    }
  }

  function handleFileRemove() {
    file = null;
    error = '';
    isDuplicateFile = false;
    duplicateFileId = null;
    if (needsExtraction) {
      needsExtraction = false;
      steps = withoutExtractionStep(steps);
    }
  }

  function acknowledgeDuplicate() {
    if (duplicateFileId) {
      dispatch('uploadComplete', { fileId: duplicateFileId, isDuplicate: true });
    }
    isDuplicateFile = false;
    error = '';
    duplicateFileId = null;
  }

  // ── Extraction ──
  let extractionChoice: 'extract' | 'full' = 'extract';

  function closeBulkModal() {
    showBulkAudioExtractionModal = false;
    bulkVideosToExtract = [];
    bulkRegularFiles = [];
  }

  function handleBulkExtractionConfirm() {
    const { collectionIds, tagNames } = getOrganizeParams();
    const transcriptionParams = getTranscriptionParams();
    if (bulkRegularFiles.length > 0) {
      uploadsStore.addFiles(bulkRegularFiles, collectionIds, tagNames, transcriptionParams);
    }
    if (bulkVideosToExtract.length > 0) startBulkExtraction(bulkVideosToExtract, transcriptionParams, { collectionIds, tagNames });
    dispatch('uploadComplete', { multiple: true, count: bulkVideosToExtract.length + bulkRegularFiles.length });
    closeBulkModal();
  }

  function handleBulkUploadAllFull() {
    const { collectionIds, tagNames } = getOrganizeParams();
    const allFiles = [...bulkVideosToExtract, ...bulkRegularFiles];
    if (allFiles.length > 0) {
      uploadsStore.addFiles(allFiles, collectionIds, tagNames, getTranscriptionParams());
    }
    dispatch('uploadComplete', { multiple: true, count: allFiles.length });
    toastStore.success($t('uploader.addedToQueueOnly', { count: allFiles.length }));
    closeBulkModal();
  }

  const handleBulkExtractionCancel = closeBulkModal;

  // ── Recording Warning ──
  function handleRecordingWarningConfirm() {
    recordingManager.clearRecording();
    if (pendingNavigationAction) { pendingNavigationAction(); pendingNavigationAction = null; }
    showRecordingWarningModal = false;
  }
  function handleRecordingWarningCancel() {
    pendingNavigationAction = null;
    showRecordingWarningModal = false;
  }

  // ── Upload Actions ──

  /**
   * Cloud-edition pre-flight quota check. If the org is already over its plan
   * limit, pop the QuotaExceededModal + upgrade CTA and block the upload before
   * we queue anything. Returns true when the upload may proceed. No-op (always
   * true) in the community edition.
   */
  function passesQuotaPreflight(): boolean {
    if (!isCloudEdition) return true;
    const usage = get(usageStore);
    if (usage.loaded && isOverLimit(usage)) {
      showQuotaExceeded($t('cloud.quota.preflightMessage'));
      return false;
    }
    return true;
  }

  function handleSubmit() {
    if (!passesQuotaPreflight()) return;
    if (activeTab === 'file') uploadFile();
    else if (activeTab === 'url') processMediaUrl();
    else if (activeTab === 'record') uploadRecordedAudio();
  }

  function uploadFile() {
    if (!file) return;

    if (needsExtraction && extractionChoice === 'extract') {
      // Extract audio in background, then queue the result
      const fileToExtract = file;
      const extractionParams = getTranscriptionParams();
      const organize = getOrganizeParams();
      savePreviousValues();
      resetAllState();
      dispatch('uploadComplete', { isFile: true });
      toastStore.info($t('uploader.extractingAudioFrom', { count: 1 }));

      // Queued when extraction finishes; params were read above, before resetAllState() cleared consent.
      void extractAndQueue(fileToExtract, extractionParams, organize);
      return;
    }

    // Normal file upload
    try {
      const transcriptionParams = getTranscriptionParams();
      const { collectionIds, tagNames } = getOrganizeParams();
      const uploadId = uploadsStore.addFile(file, transcriptionParams, collectionIds, tagNames);
      savePreviousValues();
      resetAllState();
      dispatch('uploadComplete', { uploadId, isFile: true });
      toastStore.success($t('uploader.fileAddedToQueue'));
    } catch {
      toastStore.error($t('uploader.failedToAddFileToQueue'));
    }
  }

  async function processMediaUrl() {
    if (!mediaUrl.trim()) { toastStore.error($t('uploader.enterMediaUrl')); return; }
    if (!MEDIA_URL_REGEX.test(mediaUrl.trim())) { toastStore.error($t('uploader.invalidMediaUrl')); return; }
    if (processingUrl) return;

    processingUrl = true;
    try {
      const { collectionIds, tagNames } = getOrganizeParams();
      const payload = buildUrlPayload(
        mediaUrl,
        urlPanelRef ? urlPanelRef.getUrlPayloadExtras() : null,
        { collectionIds, tagNames },
        skipSummary,
        getTranscriptionParams()
      );

      const response = await axiosInstance.post('/files/process-url', payload);
      const responseData = response.data;

      savePreviousValues();
      resetAllState();

      if (responseData.type === 'playlist') {
        dispatch('uploadComplete', { isUrl: true, multiple: true });
        toastStore.success(responseData.message || $t('uploader.playlistProcessingStarted'));
      } else {
        dispatch('uploadComplete', { fileId: responseData.uuid, isUrl: true });
        toastStore.success($t('uploader.mediaVideoAdded', { title: responseData.title || 'video' }));
      }
    } catch (error: unknown) {
      const failure = urlErrorToast(error);
      if (failure.kind === 'warning') toastStore.warning(failure.message);
      else toastStore.error(failure.message);
    } finally {
      processingUrl = false;
    }
  }

  function uploadRecordedAudio() {
    const blob = recordingManager.getRecordedBlob();
    if (!blob) return;
    try {
      const filename = `recording_${new Date().toISOString().replace(/[:.]/g, '-')}.webm`;
      const { collectionIds, tagNames } = getOrganizeParams();
      const uploadId = uploadsStore.addRecording(
        blob,
        filename,
        collectionIds,
        tagNames,
        getTranscriptionParams()
      );
      savePreviousValues();
      recordingManager.clearRecording();
      resetAllState();
      dispatch('uploadComplete', { uploadId, isRecording: true });
      toastStore.success($t('uploader.recordingAddedToQueue'));
    } catch {
      toastStore.error($t('uploader.failedToAddToQueue'));
    }
  }

  function resetAllState() {
    file = null;
    error = '';
    isDuplicateFile = false;
    duplicateFileId = null;
    mediaUrl = '';
    processingUrl = false;
    currentStepIndex = 0;
    maxStepReached = 0;
    voiceprintConsent = false;
    extractionChoice = 'extract';
    // Remove extraction step if it was dynamically added
    if (needsExtraction) {
      needsExtraction = false;
      steps = withoutExtractionStep(steps);
    }
  }

  // ── Utilities ──
  function getSubmitLabel(): string {
    if (activeTab === 'url') return processingUrl ? $t('uploader.processing') : $t('uploader.processVideo');
    if (activeTab === 'record') return $t('uploader.upload');
    return $t('uploader.upload');
  }
</script>

<div role="presentation" class="uploader-container" on:keydown={handleStepKeydown}>
  <!-- Stepper Indicator (always visible so users see the full journey) -->
  <div class="step-indicator" role="navigation" aria-label={$t('uploader.stepsNav')}>
    {#each activeSteps as step, i (step.id)}
      <button
        class="step-item"
        class:active={currentStepIndex === i}
        class:completed={i < currentStepIndex}
        class:visited={i > currentStepIndex && i <= maxStepReached}
        on:click={() => goToStep(i)}
        disabled={i > maxStepReached}
        type="button"
        aria-label={$t('uploader.stepAria', { label: $t(step.labelKey), step: i + 1, total: activeSteps.length })}
        aria-current={currentStepIndex === i ? 'step' : undefined}
      >
        <span class="step-dot">
          {#if i < currentStepIndex}
            <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round">
              <polyline points="20 6 9 17 4 12"></polyline>
            </svg>
          {:else}
            <span class="step-number">{i + 1}</span>
          {/if}
        </span>
        <span class="step-label">{$t(step.labelKey)}</span>
      </button>
      {#if i < activeSteps.length - 1}
        <div class="step-line" class:completed={i < currentStepIndex} class:visited={i >= currentStepIndex && i < maxStepReached}></div>
      {/if}
    {/each}
  </div>

  <!-- Step Content -->
  <div class="step-body" role="tabpanel">
      <div
        class="step-content"
        class:step-content--narrow={!!currentStep?.contentWidth}
        style={currentStep?.contentWidth ? `max-width: ${currentStep.contentWidth};` : ''}
      >
        {#if currentStep?.id === 'media'}
          <!-- Tab Navigation -->
          <div class="tab-navigation">
            <button class="tab-button" class:active={activeTab === 'file'} on:click={() => switchTab('file')}>
              <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
                <polyline points="17 8 12 3 7 8"></polyline>
                <line x1="12" y1="3" x2="12" y2="15"></line>
              </svg>
              {$t('uploader.uploadFile')}
            </button>
            {#if urlIngestEnabled}
            <button class="tab-button" class:active={activeTab === 'url'} on:click={() => switchTab('url')}>
              <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"></path>
                <path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"></path>
              </svg>
              {$t('uploader.mediaUrl')}
            </button>
            {/if}
            <button
              class="tab-button"
              class:active={activeTab === 'record'}
              on:click={() => switchTab('record')}
              disabled={!recordingSupported}
            >
              <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M12 1a4 4 0 0 0-4 4v7a4 4 0 0 0 8 0V5a4 4 0 0 0-4-4z"></path>
                <path d="M19 10v2a7 7 0 0 1-14 0v-2"></path>
                <line x1="12" y1="19" x2="12" y2="23"></line>
                <line x1="8" y1="23" x2="16" y2="23"></line>
              </svg>
              {$t('uploader.recordAudio')}
            </button>
          </div>

          <!-- Duplicate notification -->
          {#if isDuplicateFile}
            <div class="message duplicate-message">
              <div class="message-icon">
                <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M16 17L21 12L16 7"></path><path d="M21 12H9"></path><path d="M3 3V21"></path>
                </svg>
              </div>
              <div class="message-content">
                <strong>{$t('uploader.duplicateDetected')}</strong>
                <p>{$t('uploader.duplicateMessage')}</p>
                <button class="btn-acknowledge" on:click={acknowledgeDuplicate}>{$t('uploader.useExistingFile')}</button>
              </div>
            </div>
          {/if}

          <!-- Error messages -->
          {#if error && !isDuplicateFile}
            <div class="message error-msg">
              <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <circle cx="12" cy="12" r="10"></circle>
                <line x1="12" y1="8" x2="12" y2="12"></line>
                <line x1="12" y1="16" x2="12.01" y2="16"></line>
              </svg>
              <span>{error}</span>
            </div>
          {/if}

          <!-- Tab Content -->
          {#if activeTab === 'file'}
            <MediaFilePanel
              {file}
              on:fileSelect={handleFileSelect}
              on:multipleFiles={handleMultipleFiles}
              on:fileRemove={handleFileRemove}
            />
          {:else if activeTab === 'url'}
            <MediaUrlPanel
              bind:this={urlPanelRef}
              bind:mediaUrl
              {processingUrl}
              on:urlChange={(e) => mediaUrl = e.detail.url}
            />
          {:else}
            <MediaRecordPanel
              {recordingSupported}
              {maxRecordingDuration}
              {recordingQuality}
              {autoStopEnabled}
            />
          {/if}

          <!-- Background recording indicator -->
          {#if $hasActiveRecording && activeTab !== 'record'}
            <button class="bg-recording" on:click={() => switchTab('record')} title={$t('uploader.returnToRecording')}>
              <div class="rec-pulse"></div>
              <span>{$t('uploader.recording')}...</span>
            </button>
          {/if}

        {:else if currentStep?.id === 'extraction'}
          <UploadStepExtraction
            {file}
            bind:choice={extractionChoice}
          />

        {:else if currentStep?.id === 'tags'}
          <UploadStepTags
            bind:selectedTags
            {availableTags}
            hasPrevious={hasPreviousTags}
            on:tagsChange={(e) => selectedTags = e.detail.tags}
            on:clearPrevious={clearPreviousTags}
          />

        {:else if currentStep?.id === 'collections'}
          <UploadStepCollections
            bind:selectedCollections
            {availableCollections}
            hasPrevious={hasPreviousCollections}
            on:collectionsChange={(e) => selectedCollections = e.detail.collections}
            on:collectionsListUpdated={(e) => availableCollections = e.detail.collections}
            on:clearPrevious={clearPreviousCollections}
          />

        {:else if currentStep?.id === 'speakers'}
          <UploadStepSpeakers
            bind:minSpeakers
            bind:maxSpeakers
            bind:numSpeakers
            bind:voiceprintConsent
            {voiceprintsOn}
            {transcriptionSettings}
            {transcriptionSystemDefaults}
          />

        {:else if currentStep?.id === 'model'}
          <UploadStepModel
            bind:selectedWhisperModel
            {adminDefaultModel}
            {modelChoiceEnabled}
            bind:skipSummary
          />

        {:else if currentStep?.id === 'review'}
          <UploadStepReview
            {activeTab}
            fileName={file?.name ?? ''}
            {mediaUrl}
            {selectedTags}
            {selectedCollections}
            {minSpeakers}
            {maxSpeakers}
            {numSpeakers}
            speakerCountNote={speakerDetectionOff ? 'off' : fastModelChosen ? 'fast' : null}
            {skipSummary}
            voiceprintConsent={voiceprintConsent && voiceprintsOn && speakerCountApplies}
            {selectedWhisperModel}
            {adminDefaultModel}
            {transcriptionSystemDefaults}
            {tagsSkipped}
            {collectionsSkipped}
            extractionChoice={needsExtraction ? extractionChoice : null}
          />
        {/if}
      </div>
  </div>

  <!-- Footer Navigation -->
  <div class="step-footer">
    <div class="nav-left">
      {#if !isFirstStep}
        <button type="button" class="nav-btn nav-back" on:click={goBack}>
          <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
            <line x1="19" y1="12" x2="5" y2="12"></line>
            <polyline points="12 19 5 12 12 5"></polyline>
          </svg>
          {$t('uploader.back')}
        </button>
      {/if}
      <!-- No "Skip" button here (#739): it called `goNext` — the same handler as
           the Next button beside it — so it offered a choice that did not exist.
           Advancing past an optional step without filling it in IS skipping it;
           "Review with defaults" remains the way to jump straight to the end. -->
    </div>

    <div class="nav-right">
      {#if !isFirstStep && !isLastStep && !nextIsReview && mediaReady}
        <button
          type="button"
          class="nav-btn nav-review-defaults"
          on:click={skipToReview}
          title={$t('uploader.reviewWithDefaultsTooltip')}
        >
          {$t('uploader.reviewWithDefaults')}
          <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
            <polyline points="13 17 18 12 13 7"></polyline>
            <polyline points="6 17 11 12 6 7"></polyline>
          </svg>
        </button>
      {/if}

      {#if isLastStep}
        <button
          type="button"
          class="nav-btn nav-submit"
          on:click={handleSubmit}
          disabled={hasValidationError || processingUrl}
        >
          {getSubmitLabel()}
        </button>
      {:else}
        <button
          type="button"
          class="nav-btn nav-next"
          on:click={goNext}
          disabled={(currentStep?.id === 'media' && !mediaReady) || hasValidationError}
          title={hasValidationError ? $t('uploader.minMaxValidationError') : ''}
        >
          {nextIsReview ? $t('uploader.stepReview') : $t('uploader.next')}
          <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
            <line x1="5" y1="12" x2="19" y2="12"></line>
            <polyline points="12 5 19 12 12 19"></polyline>
          </svg>
        </button>
      {/if}
    </div>
  </div>
</div>

<!-- Modals -->
<ConfirmationModal
  bind:isOpen={showRecordingWarningModal}
  title={$t('uploader.recordingInProgressTitle')}
  message={$t('uploader.recordingWarningMessage')}
  confirmText={$t('uploader.discardRecording')}
  cancelText={$t('uploader.keepRecording')}
  confirmButtonClass="modal-warning-button"
  cancelButtonClass="modal-primary-button"
  on:confirm={handleRecordingWarningConfirm}
  on:cancel={handleRecordingWarningCancel}
/>

<BulkAudioExtractionModal
  bind:isOpen={showBulkAudioExtractionModal}
  videoFiles={bulkVideosToExtract}
  regularFiles={bulkRegularFiles}
  on:confirmExtraction={handleBulkExtractionConfirm}
  on:uploadAllFull={handleBulkUploadAllFull}
  on:cancel={handleBulkExtractionCancel}
/>

<style>
  .uploader-container {
    display: flex;
    flex-direction: column;
    width: 100%;
    padding: 0;
    min-height: 300px;
    /* Fill the modal's now-fixed height (#739) so the footer sits at the bottom
       of the dialog instead of floating under the content with a dead gap
       beneath it. `.step-body` is `flex: 1`, so it absorbs the slack. */
    height: 100%;
  }

  /* ── Stepper Indicator ── */
  .step-indicator {
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 0 var(--space-2) var(--space-4);
    gap: 0;
    flex-shrink: 0;
  }

  /* The whole step (dot + label) is one clickable button */
  .step-item {
    display: inline-flex;
    align-items: center;
    gap: var(--space-1-5);
    padding: var(--space-1) var(--space-1-5);
    background: transparent;
    border: none;
    border-radius: var(--radius-md);
    cursor: default;
    transition: background var(--duration-fast) ease;
    flex-shrink: 0;
  }

  .step-item:disabled { cursor: default; }

  .step-item:not(:disabled) { cursor: pointer; }

  .step-item:not(:disabled):hover {
    background: rgba(var(--accent-rgb), 0.08);
  }

  :global([data-theme='dark']) .step-item:not(:disabled):hover {
    background: rgba(var(--accent-rgb), 0.12);
  }

  .step-item:focus-visible {
    outline: 2px solid var(--primary-color);
    outline-offset: 2px;
  }

  /* Dot (child of .step-item, no longer a button itself) */
  .step-dot {
    width: 24px;
    height: 24px;
    border-radius: var(--radius-round);
    display: inline-flex;
    align-items: center;
    justify-content: center;
    font-size: var(--font-size-2xs);
    font-weight: var(--font-weight-semibold);
    border: 2px solid var(--border-color);
    background: var(--background-color);
    color: var(--text-secondary);
    transition: all 0.25s ease;
    flex-shrink: 0;
  }

  .step-item.active .step-dot {
    border-color: var(--primary-color);
    background: var(--primary-color);
    color: var(--text-on-solid);
  }

  .step-item.completed .step-dot {
    border-color: var(--primary-color);
    background: var(--primary-color);
    color: var(--text-on-solid);
  }

  .step-item.visited .step-dot {
    border-color: var(--primary-color);
    color: var(--primary-on-surface);
  }

  .step-number { line-height: 1; }

  /* Label */
  .step-label {
    font-size: 0.65rem;
    font-weight: var(--font-weight-medium);
    color: var(--text-secondary);
    white-space: nowrap;
    transition: color var(--duration-base) ease;
  }

  /* --primary-on-surface, not --primary-color: the accent is a FILL colour tuned for a
   * white background, and at 10.4px on the dark surface it measures 2.83:1 — well under
   * WCAG AA. The on-surface token inverts to a light blue in dark mode. Caught by #972's
   * dark-theme axe scan; the light-only scan had always passed. The `.visited` rule is
   * the worst of the three, since it then multiplies by opacity 0.7. */
  .step-item.active .step-label { color: var(--primary-on-surface); font-weight: var(--font-weight-semibold); }
  .step-item.completed .step-label { color: var(--primary-on-surface); }
  .step-item.visited .step-label { color: var(--primary-on-surface); opacity: 0.7; }

  /* Connector line between steps */
  .step-line {
    flex: 1;
    height: 2px;
    background: var(--border-color);
    margin: 0 var(--space-1);
    min-width: 12px;
    max-width: 36px;
    transition: background 0.25s ease;
  }

  .step-line.completed { background: var(--primary-color); }
  .step-line.visited { background: rgba(var(--accent-rgb), 0.4); }

  /* ── Step Content ── */
  .step-body {
    flex: 1;
    min-height: 200px;
    position: relative;
    /* A step taller than the fixed-height dialog scrolls here, inside the step
       area, so the stepper above and the nav buttons below stay put (#739). */
    overflow-y: auto;
    overscroll-behavior: contain;
  }

  .step-content {
    padding: 0 var(--space-1);
  }

  /* Narrower steps (e.g. Tags) fit their natural width instead of stretching to
     the modal's full width — #751 item 1. margin-inline is logical so it stays
     centred in RTL too. */
  .step-content--narrow {
    margin-inline: auto;
    width: 100%;
  }

  /* ── Tab Navigation ── */
  .tab-navigation {
    display: flex;
    justify-content: center;
    gap: var(--space-2);
    border-bottom: 1px solid var(--border-color);
    margin-bottom: var(--space-4);
    padding: 0;
    width: 100%;
    position: relative;
  }

  .tab-button {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    padding: var(--space-2-5) var(--space-3-5);
    background: none;
    border: none;
    cursor: pointer;
    color: var(--text-secondary);
    font-size: 0.9rem;
    font-weight: var(--font-weight-medium);
    border-radius: var(--radius-md) var(--radius-md) 0 0;
    transition: all var(--duration-base) ease;
    position: relative;
  }

  .tab-button:hover { color: var(--primary-on-surface); background-color: rgba(var(--accent-rgb), 0.05); }
  .tab-button.active { color: var(--primary-on-surface); border-bottom: 2px solid var(--primary-color); }
  .tab-button:disabled { opacity: 0.5; cursor: not-allowed; }
  .tab-button:disabled:hover { background: transparent; color: var(--text-secondary); }

  /* ── Messages ── */
  .message { display: flex; align-items: flex-start; gap: var(--space-3); padding: var(--space-3); border-radius: var(--radius-lg); margin-bottom: var(--space-3); font-size: var(--font-size-sm); }
  .message-icon { flex-shrink: 0; }
  .message-content { flex: 1; }

  .duplicate-message {
    background: rgba(var(--accent-rgb), 0.1);
    border: 1px solid var(--primary-color);
    border-left: 4px solid var(--primary-color);
    color: var(--primary-on-surface);
  }

  .duplicate-message strong { display: block; margin-bottom: var(--space-1); font-size: var(--font-size-md); }
  .duplicate-message p { margin: 0 0 var(--space-2) 0; line-height: 1.4; }

  .btn-acknowledge {
    padding: var(--space-1-5) var(--space-3);
    background: var(--primary-color);
    color: var(--text-on-solid);
    border: none;
    border-radius: var(--radius-sm);
    cursor: pointer;
    font-weight: var(--font-weight-medium);
    font-size: var(--font-size-sm);
    transition: background var(--duration-fast) ease;
  }

  .btn-acknowledge:hover { background: var(--primary-color); }

  .error-msg {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    padding: var(--space-2-5) var(--space-3);
    background: rgba(239, 68, 68, 0.08);
    border: 1px solid rgba(239, 68, 68, 0.2);
    border-radius: var(--radius-lg);
    color: var(--text-primary);
    font-size: var(--font-size-sm);
    margin-bottom: var(--space-3);
  }

  .error-msg svg { flex-shrink: 0; color: #ef4444; }

  /* ── Background Recording ── */
  .bg-recording {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    padding: var(--space-1-5) var(--space-3);
    margin-top: var(--space-2);
    background: rgba(220, 38, 38, 0.08);
    border: 1px solid rgba(220, 38, 38, 0.2);
    border-radius: var(--radius-md);
    cursor: pointer;
    font-size: var(--font-size-xs);
    color: #dc2626;
    width: 100%;
  }

  .bg-recording:hover { background: rgba(220, 38, 38, 0.12); }

  .rec-pulse {
    width: 8px;
    height: 8px;
    border-radius: var(--radius-round);
    background: var(--error-solid);
    animation: recPulse 1.5s ease-in-out infinite;
  }

  @keyframes recPulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.3; } }

  /* ── Footer Navigation ── */
  .step-footer {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-2);
    padding: var(--space-3) var(--space-1) 0;
    margin-top: var(--space-3);
    border-top: 1px solid var(--border-color);
    flex-shrink: 0;
    position: relative;
    z-index: var(--z-base);
    /* Transparent, NOT `var(--background-color)`. That token is the PAGE
       background (#0f172a in dark), while the dialog surface is
       `--surface-color` (#1e293b) — so the footer painted a visibly different,
       inset band across the bottom of the modal in dark mode. In light the two
       are #f8fafc vs #ffffff, near-identical, which is why it went unnoticed.
       Transparent simply adopts whatever surface the wizard is placed on, and
       is safe now that `.step-body` scrolls its own overflow so no content can
       pass beneath this bar. */
    background: transparent;
  }

  .nav-left, .nav-right {
    display: flex;
    gap: var(--space-2);
    align-items: center;
  }

  .nav-btn {
    display: inline-flex;
    align-items: center;
    gap: var(--space-1-5);
    padding: var(--space-2) var(--space-4);
    border-radius: var(--radius-lg);
    font-size: var(--font-size-md);
    font-weight: var(--font-weight-medium);
    cursor: pointer;
    transition: all var(--duration-fast) ease;
    white-space: nowrap;
  }

  .nav-back {
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    color: var(--text-secondary);
  }

  .nav-back:hover { background: var(--button-hover); color: var(--text-primary); }

  /* "Review with defaults" — prominent secondary action, clearly distinct from Next */
  .nav-review-defaults {
    background: rgba(var(--accent-rgb), 0.08);
    border: 1px solid var(--primary-color);
    color: var(--primary-on-surface);
    font-weight: var(--font-weight-semibold);
  }

  .nav-review-defaults:hover {
    background: rgba(var(--accent-rgb), 0.15);
    transform: translateY(-1px);
  }

  :global([data-theme='dark']) .nav-review-defaults {
    background: rgba(var(--accent-rgb), 0.12);
  }

  :global([data-theme='dark']) .nav-review-defaults:hover {
    background: rgba(var(--accent-rgb), 0.22);
  }

  .nav-next, .nav-submit {
    background: var(--primary-color);
    border: 1px solid #3b82f6;
    color: var(--text-on-solid);
    box-shadow: var(--shadow-primary);
  }

  .nav-next:hover:not(:disabled), .nav-submit:hover:not(:disabled) {
    background: var(--primary-color);
    border-color: var(--primary-color);
    box-shadow: var(--shadow-primary-hover);
    transform: translateY(-1px);
  }

  .nav-next:disabled, .nav-submit:disabled {
    opacity: 0.5;
    cursor: not-allowed;
    transform: none;
    box-shadow: none;
  }

  /* ── Responsive ── */
  @media (max-width: 480px) {
    .step-label { display: none; }
    .step-line { min-width: 12px; }
    .tab-button { padding: var(--space-2) var(--space-2-5); font-size: var(--font-size-sm); }
    .tab-button svg { width: 16px; height: 16px; }
  }

  @media (prefers-reduced-motion: reduce) {
    .rec-pulse { animation: none; }
  }
</style>
