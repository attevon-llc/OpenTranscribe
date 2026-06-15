<script lang="ts">
  import { createEventDispatcher, onMount, onDestroy, tick } from 'svelte';
  import axiosInstance from '$lib/axios';
  import ConfirmationModal from './ConfirmationModal.svelte';
  import BulkAudioExtractionModal from './BulkAudioExtractionModal.svelte';

  // Global stores
  import { recordingStore, recordingManager, hasActiveRecording, isRecording, recordingStartTime } from '$stores/recording';
  import { uploadsStore } from '$stores/uploads';
  import { toastStore } from '$stores/toast';
  import { isOnline } from '$stores/network';
  import { t } from '$stores/locale';
  import { isCloudEdition } from '$lib/edition';
  import { usageStore, isOverLimit } from '$stores/cloudBilling';
  import { showQuotaExceeded } from '$stores/quotaModal';
  import { get } from 'svelte/store';

  // APIs
  import { loadProtectedMediaAuthConfig } from '$lib/services/configService';
  import type { ExtractedAudio } from '$lib/types/audioExtraction';
  import { getAudioExtractionSettings, type AudioExtractionSettings } from '$lib/api/audioExtractionSettings';
  import { audioExtractionService } from '$lib/services/audioExtractionService';
  import {
    getTranscriptionSettings,
    getTranscriptionSystemDefaults,
    type TranscriptionSettings,
    type TranscriptionSystemDefaults,
    DEFAULT_TRANSCRIPTION_SETTINGS
  } from '$lib/api/transcriptionSettings';
  import { ASRSettingsApi } from '$lib/api/asrSettings';

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
  type StepId = 'media' | 'extraction' | 'tags' | 'collections' | 'speakers' | 'model' | 'review';
  interface StepConfig { id: StepId; labelKey: string; optional: boolean; skipped: boolean; }
  interface UploadPreviousValues {
    collectionIds: string[];
    collectionNames: string[];
    tagNames: string[];
    minSpeakers: number | null;
    maxSpeakers: number | null;
    numSpeakers: number | null;
    skipSummary: boolean;
    selectedWhisperModel: string | null;
    skippedSteps: string[];  // step IDs that were skipped (e.g. ['tags', 'collections'])
    timestamp: number;
  }

  // ── Constants ──
  const MEDIA_URL_REGEX = /^https?:\/\/.+$/;
  const PREVIOUS_VALUES_KEY = 'opentr:uploadPreviousValues';
  const FILE_SIZE_LIMIT = 2 * 1024 * 1024 * 1024;
  const LARGE_FILE_THRESHOLD = 100 * 1024 * 1024;

  // ── Stepper State ──
  let steps: StepConfig[] = [
    { id: 'media',       labelKey: 'uploader.stepMedia',       optional: false, skipped: false },
    { id: 'tags',        labelKey: 'uploader.stepTags',        optional: true,  skipped: false },
    { id: 'collections', labelKey: 'uploader.stepCollections', optional: true,  skipped: false },
    { id: 'speakers',    labelKey: 'uploader.stepSpeakers',    optional: false, skipped: false },
    { id: 'model',       labelKey: 'uploader.stepModel',       optional: false, skipped: false },
    { id: 'review',      labelKey: 'uploader.stepReview',      optional: false, skipped: false },
  ];
  let currentStepIndex = 0;
  let maxStepReached = 0;

  $: activeSteps = steps.filter(s => !s.skipped);
  $: currentStep = activeSteps[currentStepIndex];
  $: isFirstStep = currentStepIndex === 0;
  $: isLastStep = currentStepIndex === activeSteps.length - 1;
  $: if (currentStepIndex > maxStepReached) maxStepReached = currentStepIndex;

  // ── Tab & Media State ──
  let activeTab: 'file' | 'url' | 'record' = 'file';
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
  let skipSummary = false;
  let selectedWhisperModel: string | null = null;
  let adminDefaultModel = 'large-v3-turbo';
  let transcriptionSettings: TranscriptionSettings | null = null;
  let transcriptionSystemDefaults: TranscriptionSystemDefaults | null = null;

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

  $: hasValidationError = minSpeakers !== null && maxSpeakers !== null && minSpeakers > maxSpeakers;

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
    void loadProtectedMediaAuthConfig();
    loadRecordingSettings();

    (async () => {
      try {
        audioExtractionSettings = await getAudioExtractionSettings();
      } catch {
        audioExtractionSettings = { auto_extract_enabled: true, extraction_threshold_mb: 100, remember_choice: false, show_modal: true };
      }
    })();

    (async () => {
      try {
        const [userSettings, systemDefaults] = await Promise.all([
          getTranscriptionSettings(),
          getTranscriptionSystemDefaults()
        ]);
        transcriptionSettings = userSettings;
        transcriptionSystemDefaults = systemDefaults;
        applyTranscriptionPreferences();
      } catch {
        transcriptionSettings = { ...DEFAULT_TRANSCRIPTION_SETTINGS };
        transcriptionSystemDefaults = {
          min_speakers: 1, max_speakers: 20, garbage_cleanup_enabled: true, garbage_cleanup_threshold: 50,
          valid_speaker_prompt_behaviors: ['always_prompt', 'use_defaults', 'use_custom'],
          available_source_languages: { auto: 'Auto-detect', en: 'English' },
          available_llm_output_languages: { en: 'English' }, common_languages: ['auto', 'en'],
          vad_threshold: 0.5, vad_min_silence_ms: 2000, vad_min_speech_ms: 250, vad_speech_pad_ms: 400,
          hallucination_silence_threshold: null, repetition_penalty: 1.0,
          diarization_source_default: 'provider', valid_diarization_sources: ['provider', 'local', 'pyannote', 'off']
        };
      }
    })();

    (async () => {
      try {
        const activeInfo = await ASRSettingsApi.getActiveLocalModel();
        adminDefaultModel = activeInfo.active_model || 'large-v3-turbo';
      } catch { adminDefaultModel = 'large-v3-turbo'; }
    })();

    (async () => {
      try {
        const [collectionsRes, tagsRes] = await Promise.all([
          axiosInstance.get('/collections'),
          axiosInstance.get('/tags'),
        ]);
        availableCollections = collectionsRes.data;
        availableTags = tagsRes.data;
        loadPreviousValues();
      } catch {
        console.error('Failed to load collections/tags');
      }
    })();

    handleSetTabEvent = (event: Event) => {
      const customEvent = event as CustomEvent;
      if (customEvent.detail?.activeTab) {
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
    const behavior = transcriptionSettings.speaker_prompt_behavior;
    switch (behavior) {
      case 'always_prompt':
        minSpeakers = transcriptionSettings.min_speakers || null;
        maxSpeakers = transcriptionSettings.max_speakers || null;
        break;
      case 'use_defaults':
        minSpeakers = null;
        maxSpeakers = null;
        break;
      case 'use_custom':
        minSpeakers = transcriptionSettings.min_speakers || null;
        maxSpeakers = transcriptionSettings.max_speakers || null;
        break;
    }
  }

  function getEffectiveSpeakerSettings() {
    return { minSpeakers, maxSpeakers, numSpeakers };
  }

  function getOrganizeParams() {
    return {
      collectionIds: selectedCollections.length > 0 ? selectedCollections.map(c => c.uuid) : undefined,
      tagNames: selectedTags.length > 0 ? [...selectedTags] : undefined,
    };
  }

  // ── Recording Settings ──
  function loadRecordingSettings() {
    const settings = localStorage.getItem('recordingSettings');
    if (settings) {
      try {
        const parsed = JSON.parse(settings);
        maxRecordingDuration = (parsed.maxRecordingDuration || 120) * 60;
        recordingQuality = parsed.recordingQuality || 'high';
        autoStopEnabled = parsed.autoStopEnabled !== undefined ? parsed.autoStopEnabled : true;
      } catch { /* use defaults */ }
    }
  }

  // ── Remember Previous Values ──
  function loadPreviousValues() {
    try {
      const stored = localStorage.getItem(PREVIOUS_VALUES_KEY);
      if (!stored) return;
      const prev: UploadPreviousValues = JSON.parse(stored);

      // Restore tags
      if (prev.tagNames?.length > 0) {
        selectedTags = [...prev.tagNames];
        hasPreviousTags = true;
      }

      // Restore collections (validate IDs still exist)
      if (prev.collectionIds?.length > 0) {
        const validCollections = prev.collectionIds
          .map(id => availableCollections.find(c => c.uuid === id))
          .filter(Boolean) as Array<{uuid: string; name: string}>;
        if (validCollections.length > 0) {
          selectedCollections = validCollections;
          hasPreviousCollections = true;
        }
      }

      // Restore speaker settings
      if (prev.minSpeakers !== undefined) minSpeakers = prev.minSpeakers;
      if (prev.maxSpeakers !== undefined) maxSpeakers = prev.maxSpeakers;
      if (prev.numSpeakers !== undefined) numSpeakers = prev.numSpeakers;

      // Restore model/options
      if (prev.skipSummary) skipSummary = prev.skipSummary;
      if (prev.selectedWhisperModel !== undefined) selectedWhisperModel = prev.selectedWhisperModel;

      // Restore which steps were skipped
      if (prev.skippedSteps?.length > 0) {
        steps = steps.map(s =>
          s.optional && prev.skippedSteps.includes(s.id) ? { ...s, skipped: true } : s
        );
      }

      rememberValues = true;
    } catch { /* ignore parse errors */ }
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
    localStorage.setItem(PREVIOUS_VALUES_KEY, JSON.stringify(values));
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

    if (!selectedFile.type || !selectedFile.type.match(/^(audio|video)\//)) {
      error = $t('uploader.fileTypeError');
      file = null;
      return;
    }

    const MAX_FILE_SIZE = 15 * 1024 * 1024 * 1024;
    if (selectedFile.size > MAX_FILE_SIZE) {
      error = $t('uploader.fileTooLargeError', { fileSize: formatFileSize(selectedFile.size), maxSize: formatFileSize(MAX_FILE_SIZE) });
      file = null;
      return;
    }

    if (selectedFile.size > 2 * 1024 * 1024 * 1024) {
      error = $t('uploader.largeFileWarning', { fileSize: formatFileSize(selectedFile.size) });
    }

    file = selectedFile as FileWithSize;

    // Check if large video needs extraction step
    const isVideo = selectedFile.type.startsWith('video/');
    const thresholdMb = audioExtractionSettings?.extraction_threshold_mb || 100;
    const isLargeFile = selectedFile.size > thresholdMb * 1024 * 1024;
    const shouldOfferExtraction = audioExtractionSettings?.auto_extract_enabled !== false && isVideo && isLargeFile;

    if (shouldOfferExtraction && !needsExtraction) {
      // Insert extraction step after media
      needsExtraction = true;
      steps = [
        steps[0], // media
        { id: 'extraction', labelKey: 'uploader.stepExtraction', optional: false, skipped: false },
        ...steps.slice(1), // tags, collections, speakers, model, review
      ];
    } else if (!shouldOfferExtraction && needsExtraction) {
      // Remove extraction step if file changed to non-video
      needsExtraction = false;
      steps = steps.filter(s => s.id !== 'extraction');
    }
  }

  function handleMultipleFiles(event: CustomEvent<{ files: File[] }>) {
    const files = event.detail.files;
    const validFiles: File[] = [];
    const invalidFiles: string[] = [];

    files.forEach(f => {
      if (f.size > FILE_SIZE_LIMIT) {
        invalidFiles.push(`${f.name} (${$t('uploader.tooLarge', { size: formatFileSize(f.size) })})`);
        return;
      }
      const isValidType = f.type && (f.type.startsWith('audio/') || f.type.startsWith('video/'));
      if (!isValidType) {
        const ext = f.name.split('.').pop()?.toLowerCase() || '';
        const validExts = ['mp3','wav','ogg','flac','aac','m4a','wma','opus','mp4','avi','mov','wmv','flv','webm','mkv','3gp','f4v'];
        if (!validExts.includes(ext)) {
          invalidFiles.push(`${f.name} (${$t('uploader.unsupportedFormat')})`);
          return;
        }
      }
      validFiles.push(f);
    });

    if (invalidFiles.length > 0) {
      toastStore.error($t('uploader.skippedInvalidFiles', { count: invalidFiles.length, files: invalidFiles.join('\n') }));
    }

    if (validFiles.length > 0) {
      const { collectionIds, tagNames } = getOrganizeParams();
      uploadsStore.addFiles(validFiles, collectionIds, tagNames);
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
      steps = steps.filter(s => s.id !== 'extraction');
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

  // ── Audio Extraction Modal Handlers ──
  // ── Extraction ──
  let extractionChoice: 'extract' | 'full' = 'extract';


  function handleBulkExtractionConfirm() {
    showBulkAudioExtractionModal = false;
    const { collectionIds, tagNames } = getOrganizeParams();
    if (bulkRegularFiles.length > 0) uploadsStore.addFiles(bulkRegularFiles, collectionIds, tagNames);
    if (bulkVideosToExtract.length > 0) startBulkExtraction(bulkVideosToExtract);
    dispatch('uploadComplete', { multiple: true, count: bulkVideosToExtract.length + bulkRegularFiles.length });
    bulkVideosToExtract = [];
    bulkRegularFiles = [];
  }

  function handleBulkUploadAllFull() {
    showBulkAudioExtractionModal = false;
    const { collectionIds, tagNames } = getOrganizeParams();
    const allFiles = [...bulkVideosToExtract, ...bulkRegularFiles];
    if (allFiles.length > 0) uploadsStore.addFiles(allFiles, collectionIds, tagNames);
    dispatch('uploadComplete', { multiple: true, count: allFiles.length });
    toastStore.success($t('uploader.addedToQueueOnly', { count: allFiles.length }));
    bulkVideosToExtract = [];
    bulkRegularFiles = [];
  }

  function handleBulkExtractionCancel() {
    showBulkAudioExtractionModal = false;
    bulkVideosToExtract = [];
    bulkRegularFiles = [];
  }

  function startBulkExtraction(videoFiles: File[]) {
    toastStore.info($t('uploader.extractingAudioFrom', { count: videoFiles.length }));
    videoFiles.forEach(async (videoFile) => {
      try {
        const ea = await audioExtractionService.extractAudio(videoFile);
        uploadsStore.addExtractedAudio(ea.blob, ea.filename, ea.metadata, ea.metadata.compressionRatio);
      } catch {
        toastStore.error($t('uploader.failedToExtractAudio', { filename: videoFile.name }));
      }
    });
  }

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
      savePreviousValues();
      resetAllState();
      dispatch('uploadComplete', { isFile: true });
      toastStore.info($t('uploader.extractingAudioFrom', { count: 1 }));

      // Extraction runs in background — result is queued when done
      (async () => {
        try {
          const extractedAudio = await audioExtractionService.extractAudio(fileToExtract);
          uploadsStore.addExtractedAudio(
            extractedAudio.blob,
            extractedAudio.filename,
            extractedAudio.metadata,
            extractedAudio.metadata.compressionRatio
          );
          toastStore.success($t('uploader.audioExtractedSuccess', { ratio: extractedAudio.metadata.compressionRatio }));
        } catch {
          toastStore.error($t('uploader.failedToExtractAudio', { filename: fileToExtract.name }));
        }
      })();
      return;
    }

    // Normal file upload
    try {
      const speakerParams = getEffectiveSpeakerSettings();
      const { collectionIds, tagNames } = getOrganizeParams();
      const uploadId = uploadsStore.addFile(file, speakerParams, collectionIds, tagNames);
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
      const payload: any = { url: mediaUrl.trim() };

      // Get extras from URL panel (auth, download quality)
      if (urlPanelRef) {
        const extras = urlPanelRef.getUrlPayloadExtras();
        Object.assign(payload, extras);
      }

      const { collectionIds, tagNames } = getOrganizeParams();
      if (collectionIds) payload.collection_ids = collectionIds;
      if (tagNames) payload.tag_names = tagNames;
      if (skipSummary) payload.skip_summary = true;

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
      const axiosError = error as any;
      if (axiosError.response?.status === 409) {
        toastStore.warning(axiosError.response.data.detail || $t('uploader.duplicateMediaVideo'));
      } else if (axiosError.response?.status === 400) {
        toastStore.error(axiosError.response.data.detail || $t('uploader.invalidMediaUrl'));
      } else {
        toastStore.error($t('uploader.failedToProcessUrl'));
      }
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
      const uploadId = uploadsStore.addRecording(blob, filename, collectionIds, tagNames);
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
    extractionChoice = 'extract';
    // Remove extraction step if it was dynamically added
    if (needsExtraction) {
      needsExtraction = false;
      steps = steps.filter(s => s.id !== 'extraction');
    }
  }

  // ── Utilities ──
  function formatFileSize(bytes: number): string {
    if (bytes === 0) return '0 Bytes';
    const k = 1024;
    const sizes = ['Bytes', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return `${parseFloat((bytes / Math.pow(k, i)).toFixed(2))} ${sizes[i]}`;
  }

  function getSubmitLabel(): string {
    if (activeTab === 'url') return processingUrl ? $t('uploader.processing') : $t('uploader.processVideo');
    if (activeTab === 'record') return $t('uploader.upload');
    return $t('uploader.upload');
  }
</script>

<!-- svelte-ignore a11y-no-static-element-interactions -->
<div class="uploader-container" on:keydown={handleStepKeydown}>
  <!-- Stepper Indicator (always visible so users see the full journey) -->
  <div class="step-indicator" role="navigation" aria-label="Upload steps">
    {#each activeSteps as step, i}
      <button
        class="step-item"
        class:active={currentStepIndex === i}
        class:completed={i < currentStepIndex}
        class:visited={i > currentStepIndex && i <= maxStepReached}
        on:click={() => goToStep(i)}
        disabled={i > maxStepReached}
        type="button"
        aria-label="{$t(step.labelKey)} (step {i + 1} of {activeSteps.length})"
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
      <div class="step-content">
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
            <button class="tab-button" class:active={activeTab === 'url'} on:click={() => switchTab('url')}>
              <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"></path>
                <path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"></path>
              </svg>
              {$t('uploader.mediaUrl')}
            </button>
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
              on:cancel={() => { mediaUrl = ''; processingUrl = false; }}
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
            {transcriptionSettings}
            {transcriptionSystemDefaults}
          />

        {:else if currentStep?.id === 'model'}
          <UploadStepModel
            bind:selectedWhisperModel
            {adminDefaultModel}
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
            {skipSummary}
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
      {#if currentStep?.optional && !isLastStep}
        <button type="button" class="nav-btn nav-skip" on:click={goNext}>
          {$t('uploader.skip')}
        </button>
      {/if}
    </div>

    <div class="nav-right">
      {#if !isFirstStep && !isLastStep && mediaReady}
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
          {$t('uploader.next')}
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
  }

  /* ── Stepper Indicator ── */
  .step-indicator {
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 0 0.5rem 1rem;
    gap: 0;
    flex-shrink: 0;
  }

  /* The whole step (dot + label) is one clickable button */
  .step-item {
    display: inline-flex;
    align-items: center;
    gap: 0.375rem;
    padding: 0.25rem 0.375rem;
    background: transparent;
    border: none;
    border-radius: 6px;
    cursor: default;
    transition: background 0.15s ease;
    flex-shrink: 0;
  }

  .step-item:disabled { cursor: default; }

  .step-item:not(:disabled) { cursor: pointer; }

  .step-item:not(:disabled):hover {
    background: rgba(59, 130, 246, 0.08);
  }

  :global(.dark) .step-item:not(:disabled):hover {
    background: rgba(59, 130, 246, 0.12);
  }

  .step-item:focus-visible {
    outline: 2px solid var(--primary-color, #3b82f6);
    outline-offset: 2px;
  }

  /* Dot (child of .step-item, no longer a button itself) */
  .step-dot {
    width: 24px;
    height: 24px;
    border-radius: 50%;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    font-size: 0.6875rem;
    font-weight: 600;
    border: 2px solid var(--border-color);
    background: var(--background-color);
    color: var(--text-secondary);
    transition: all 0.25s ease;
    flex-shrink: 0;
  }

  .step-item.active .step-dot {
    border-color: var(--primary-color, #3b82f6);
    background: #3b82f6;
    color: white;
  }

  .step-item.completed .step-dot {
    border-color: var(--primary-color, #3b82f6);
    background: #3b82f6;
    color: white;
  }

  .step-item.visited .step-dot {
    border-color: var(--primary-color, #3b82f6);
    color: var(--primary-color, #3b82f6);
  }

  .step-number { line-height: 1; }

  /* Label */
  .step-label {
    font-size: 0.65rem;
    font-weight: 500;
    color: var(--text-secondary);
    white-space: nowrap;
    transition: color 0.2s ease;
  }

  .step-item.active .step-label { color: var(--primary-color, #3b82f6); font-weight: 600; }
  .step-item.completed .step-label { color: var(--primary-color, #3b82f6); }
  .step-item.visited .step-label { color: var(--primary-color, #3b82f6); opacity: 0.7; }

  /* Connector line between steps */
  .step-line {
    flex: 1;
    height: 2px;
    background: var(--border-color);
    margin: 0 0.25rem;
    min-width: 12px;
    max-width: 36px;
    transition: background 0.25s ease;
  }

  .step-line.completed { background: #3b82f6; }
  .step-line.visited { background: rgba(59, 130, 246, 0.4); }

  /* ── Step Content ── */
  .step-body {
    flex: 1;
    min-height: 200px;
    position: relative;
  }

  .step-content {
    padding: 0 0.25rem;
  }

  /* ── Tab Navigation ── */
  .tab-navigation {
    display: flex;
    justify-content: center;
    gap: 0.5rem;
    border-bottom: 1px solid var(--border-color);
    margin-bottom: 1rem;
    padding: 0;
    width: 100%;
    position: relative;
  }

  .tab-button {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.625rem 0.875rem;
    background: none;
    border: none;
    cursor: pointer;
    color: var(--text-secondary);
    font-size: 0.9rem;
    font-weight: 500;
    border-radius: 6px 6px 0 0;
    transition: all 0.2s ease;
    position: relative;
  }

  .tab-button:hover { color: var(--primary-color); background-color: rgba(59, 130, 246, 0.05); }
  .tab-button.active { color: var(--primary-color); border-bottom: 2px solid var(--primary-color); }
  .tab-button:disabled { opacity: 0.5; cursor: not-allowed; }
  .tab-button:disabled:hover { background: transparent; color: var(--text-secondary); }

  /* ── Messages ── */
  .message { display: flex; align-items: flex-start; gap: 0.75rem; padding: 0.75rem; border-radius: 8px; margin-bottom: 0.75rem; font-size: 0.8125rem; }
  .message-icon { flex-shrink: 0; }
  .message-content { flex: 1; }

  .duplicate-message {
    background: rgba(59, 130, 246, 0.1);
    border: 1px solid var(--primary-color);
    border-left: 4px solid var(--primary-color);
    color: var(--primary-color);
  }

  .duplicate-message strong { display: block; margin-bottom: 0.25rem; font-size: 0.875rem; }
  .duplicate-message p { margin: 0 0 0.5rem 0; line-height: 1.4; }

  .btn-acknowledge {
    padding: 0.375rem 0.75rem;
    background: #3b82f6;
    color: white;
    border: none;
    border-radius: 4px;
    cursor: pointer;
    font-weight: 500;
    font-size: 0.8125rem;
    transition: background 0.15s ease;
  }

  .btn-acknowledge:hover { background: #2563eb; }

  .error-msg {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.625rem 0.75rem;
    background: rgba(239, 68, 68, 0.08);
    border: 1px solid rgba(239, 68, 68, 0.2);
    border-radius: 8px;
    color: var(--text-primary);
    font-size: 0.8125rem;
    margin-bottom: 0.75rem;
  }

  .error-msg svg { flex-shrink: 0; color: #ef4444; }

  /* ── Background Recording ── */
  .bg-recording {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.375rem 0.75rem;
    margin-top: 0.5rem;
    background: rgba(220, 38, 38, 0.08);
    border: 1px solid rgba(220, 38, 38, 0.2);
    border-radius: 6px;
    cursor: pointer;
    font-size: 0.75rem;
    color: #dc2626;
    width: 100%;
  }

  .bg-recording:hover { background: rgba(220, 38, 38, 0.12); }

  .rec-pulse {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: #dc2626;
    animation: recPulse 1.5s ease-in-out infinite;
  }

  @keyframes recPulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.3; } }

  /* ── Footer Navigation ── */
  .step-footer {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.5rem;
    padding: 0.75rem 0.25rem 0;
    margin-top: 0.75rem;
    border-top: 1px solid var(--border-color);
    flex-shrink: 0;
    position: relative;
    z-index: 1;
    background: var(--background-color);
  }

  .nav-left, .nav-right {
    display: flex;
    gap: 0.5rem;
    align-items: center;
  }

  .nav-btn {
    display: inline-flex;
    align-items: center;
    gap: 0.375rem;
    padding: 0.5rem 1rem;
    border-radius: 8px;
    font-size: 0.875rem;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.15s ease;
    white-space: nowrap;
  }

  .nav-back {
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    color: var(--text-secondary);
  }

  .nav-back:hover { background: var(--button-hover); color: var(--text-primary); }

  .nav-skip {
    background: transparent;
    border: 1px solid var(--border-color);
    color: var(--text-secondary);
  }

  .nav-skip:hover { background: var(--surface-color); color: var(--text-primary); }

  /* "Review with defaults" — prominent secondary action, clearly distinct from Next */
  .nav-review-defaults {
    background: rgba(59, 130, 246, 0.08);
    border: 1px solid var(--primary-color, #3b82f6);
    color: var(--primary-color, #3b82f6);
    font-weight: 600;
  }

  .nav-review-defaults:hover {
    background: rgba(59, 130, 246, 0.15);
    transform: translateY(-1px);
  }

  :global(.dark) .nav-review-defaults {
    background: rgba(59, 130, 246, 0.12);
  }

  :global(.dark) .nav-review-defaults:hover {
    background: rgba(59, 130, 246, 0.22);
  }

  .nav-next, .nav-submit {
    background: #3b82f6;
    border: 1px solid #3b82f6;
    color: white;
    box-shadow: 0 2px 4px rgba(59, 130, 246, 0.2);
  }

  .nav-next:hover:not(:disabled), .nav-submit:hover:not(:disabled) {
    background: #2563eb;
    border-color: #2563eb;
    box-shadow: 0 4px 8px rgba(59, 130, 246, 0.25);
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
    .tab-button { padding: 0.5rem 0.625rem; font-size: 0.8125rem; }
    .tab-button svg { width: 16px; height: 16px; }
  }

  @media (prefers-reduced-motion: reduce) {
    .rec-pulse { animation: none; }
  }
</style>
