<script lang="ts">
  import { resolve } from '$app/paths';
  import { onMount, onDestroy, tick } from 'svelte';
  import type { Collection } from '$lib/types/collection';
  import type { Tag } from '$lib/types/tag';
  import { guardUnsavedChanges } from '$lib/navigation/unsavedChangesGuard';
  import axiosInstance from '$lib/axios';
  import { websocketStore, type Notification } from '$stores/websocket';
  import { handleFileNotification } from '$lib/fileDetail/notificationHandler';
  import { isForFile, latestNotificationForFile, notificationSignature } from '$lib/fileDetail/notificationSelect';
  import { createPageTimers } from '$lib/fileDetail/pageTimers';
  import {
    asFileDetailState,
    splitFileDetail,
    fetchAISuggestions,
    fetchFileAnalytics,
    getFileDetail,
    mergeTranscriptRefresh,
    paginationFrom,
    readRedactionState,
    sawMaskedSpans,
    startRedactionScan,
    type FileDetailPayload,
    type FileDetailState,
  } from '$lib/fileDetail/fileDetailData';
  import { createRedactionView } from '$lib/fileDetail/redactionView';
  import { createSegmentPager } from '$lib/fileDetail/segmentPaging';
  import { createFileSpeakers } from '$lib/fileDetail/fileSpeakers';
  import { syncTranscriptStore } from '$lib/fileDetail/speakerLoader';
  import {
    hasUnsavedSpeakerNames,
    type SpeakerItem,
    type SpeakerNameBaseline,
  } from '$lib/fileDetail/speakerNames';
  import { seekTimeFromSearch } from '$lib/fileDetail/deepLinks';
  import { createSegmentEditor, hasUnsavedSegmentEdit } from '$lib/fileDetail/segmentEdit';

  import FilePlayer from '$components/fileDetail/FilePlayer.svelte';
  import MetadataDisplay from '$components/MetadataDisplay.svelte';
  import ProcessingSection from '$components/fileDetail/ProcessingSection.svelte';
  import AudioTracksNotice from '$components/fileDetail/AudioTracksNotice.svelte';
  import AnalyticsSection from '$components/AnalyticsSection.svelte';
  import TranscriptDisplay from '$components/TranscriptDisplay.svelte';
  import TranscriptActionsBar from '$components/transcript/TranscriptActionsBar.svelte';
  import SpeakerEditorPanel from '$components/transcript/SpeakerEditorPanel.svelte';
  import { createDownloadStreamManager, type DownloadMode } from '$lib/fileDetail/downloadStream';
  import FileHeader from '$components/FileHeader.svelte';
  import TagsSection from '$components/TagsSection.svelte';
  import CommentSection from '$components/CommentSection.svelte';
  import CollectionsSection from '$components/CollectionsSection.svelte';
  import SelectiveReprocessModal from '$components/SelectiveReprocessModal.svelte';
  import { toastStore } from '$stores/toast';
  import { t } from '$stores/locale';
  import { getErrorMessage, getErrorStatus } from '$lib/utils/apiError';
  import FileSummary from '$components/fileDetail/FileSummary.svelte';
  import TranscriptViewModal from '$components/transcript/TranscriptViewModal.svelte';
  import TranscriptExportDialogs from '$components/fileDetail/TranscriptExportDialogs.svelte';
  import FileActionButtons from '$components/fileDetail/FileActionButtons.svelte';
  import RedactionControls from '$components/fileDetail/RedactionControls.svelte';
  import RedactionPendingPanel from '$components/fileDetail/RedactionPendingPanel.svelte';
  import SpeakerProfileConfirmModal from '$components/fileDetail/SpeakerProfileConfirmModal.svelte';
  import { isLLMAvailable, isLLMSettingsUnavailable } from '$stores/llmStatus';
  import { transcriptStore } from '$stores/transcriptStore';
  import { downloadStore } from '$stores/downloads';
  import { supportSessionGate } from '$stores/supportSession';
  import type { TagSuggestion, CollectionSuggestion } from '$lib/api/suggestions';
  import type { PlaybackMode } from '$lib/api/mediaUrl';
  import { createMediaStream } from '$lib/fileDetail/mediaStream';
  import { applyReprocessStart, reprocessRegeneratesSummary } from '$lib/fileDetail/reprocess';
  import Spinner from '$components/ui/Spinner.svelte';
  import FileDetailSkeleton from '$components/FileDetailSkeleton.svelte';
  import { provideFileFrozen } from '$lib/legalHold/fileHoldContext';
  import { onHoldRefused } from '$lib/legalHold/refusal';
  import { markHeld } from '$lib/legalHold/summary';

  // Props - SvelteKit passes data from +page.ts
  export let data;
  $: id = data.id;

  let file: FileDetailState | null = null;
  // Legal hold: every edit control below reads this through context (legal-hold plan, Lane D).
  const fileFrozenStore = provideFileFrozen();
  $: fileFrozenStore.set(file?.hold?.held === true);
  // A hold placed while this page is open: a refused edit switches the page to the frozen view.
  onDestroy(onHoldRefused((heldBy) => { if (file) file = { ...file, hold: markHeld(file.hold, heldBy) }; }));
  let fileId = '';
  let videoUrl = '';
  let streamContentType = '';
  let playbackMode: PlaybackMode | null = null;
  let pageErrorMessage = '';
  // 404/403: the file is missing or not visible to this user; retrying can't help.
  let fileNotFound = false;
  let videoErrorMessage = '';
  let filePlayer: FilePlayer | null = null;
  let currentTime = 0;
  let isLoading = true;
  // `?t=` from search results: the player seeks there once, then this is cleared.
  let initialSeekTime = seekTimeFromSearch(window.location.search);
  let collections: Collection[] = [];

  // DOM-touching deferred work goes through this so onDestroy can cancel it (see pageTimers).
  const pageTimers = createPageTimers();
  const scheduleTimeout = pageTimers.schedule;

  let showMetadata = false;
  let isTagsExpanded = false;
  let isCollectionsExpanded = false;
  let isAnalyticsExpanded = false;
  let savingTranscript = false;
  let savingSpeakers = false;
  let editingSegmentId: string | number | null = null;
  let editingSegmentText = '';
  // The text the open segment editor STARTED with. Without it "is this dirty" would be
  // "is an editor open", and merely clicking the pencil would prompt on the way out.
  let editingSegmentOriginalText = '';
  // Owned here, not passed one-way into TranscriptDisplay: its own copy is why a successful
  // bulk save never closed the speaker panel (#748 §5.2).
  let isEditingSpeakers = false;

  // The download SSE stream lives with the other `$lib/fileDetail/*` orchestration
  // modules; `TranscriptActionsBar`, which triggers it, renders on this page.
  const downloadManager = createDownloadStreamManager();
  $: downloadState = $downloadStore;
  $: currentDownload = (file ? downloadState[file.uuid] : undefined);
  $: isDownloading = !!currentDownload && ['preparing', 'processing', 'downloading'].includes(currentDownload.status);
  $: isVideoFile = file?.content_type?.startsWith('video/') ?? false;
  $: canEmbedSubtitles = isVideoFile && file?.status === 'completed';

  async function handleDownloadFromBar(event: CustomEvent<{ mode: DownloadMode }>) {
    if (isDownloading) {
      toastStore.warning($t('transcript.downloadAlreadyProcessing', { filename: file?.filename }));
      return;
    }
    try {
      await downloadManager.downloadMedia(file, event.detail.mode);
    } catch (error: unknown) {
      toastStore.error(getErrorMessage(error, $t('transcript.fileNotAvailable')));
    }
  }

  let speakerList: SpeakerItem[] = [];
  let originalSpeakerNames: SpeakerNameBaseline = new Map(); // Last-saved names, for change detection
  // Speaker list, renames (single, profile decision, bulk save), merges, live events.
  const speakers = createFileSpeakers({
    getFileId: () => fileId,
    getFile: () => file,
    setFile: (f) => (file = asFileDetailState(f)),
    getShowOriginal: () => showOriginal,
    getSpeakerList: () => speakerList,
    setSpeakerList: (list) => (speakerList = list),
    getBaseline: () => originalSpeakerNames,
    setBaseline: (baseline) => (originalSpeakerNames = baseline),
    setSavingSpeakers: (saving) => (savingSpeakers = saving),
    setProfileConfirmation: (dialog) => (speakerProfileDialog = dialog),
    closeSpeakerEditor: () => (isEditingSpeakers = false),
    isDiarizationDisabled: () => diarizationDisabled,
    refreshCaptionTrack,
    refreshAnalytics: () => void handleAnalyticsRefreshNeeded(),
    applyReloadedFile: (data) => {
      ({ file, collections } = splitFileDetail(data));
      processTranscriptData();
    },
    refetchSegmentsAfterMerge: () => segmentPager.refetchAfterMerge(),
  });
  const loadSpeakers = speakers.loadSpeakers;
  // Reported by CommentSection; the export flow reads it instead of refetching comments.
  let commentCount = 0;
  let reprocessing = false;
  let showReprocessModal = false;
  // Summary modal, generate and the ?view=summary deep link: see FileSummary.
  let fileSummary: FileSummary;
  let showTranscriptModal = false;
  let generatingSummary = false;
  let summaryGenerating = false; // WebSocket-driven summary generation status
  let currentProcessingStep = ''; // Current processing step from WebSocket notifications
  let lastProcessedNotificationState = ''; // Track processed notification state globally

  let totalSegments = 0;
  let totalSpeakerSegments = 0;
  let segmentLimit = 500;
  let loadingMoreSegments = false;
  $: hasMoreSegments = totalSegments > (file?.transcript_segments?.length || 0);

  let aiTagSuggestions: TagSuggestion[] = [];
  let aiCollectionSuggestions: CollectionSuggestion[] = [];

  // Caller's permission on this file. Backend convention (files/detail_response.py):
  // `null` = you are the owner. `undefined` = not fetched yet / fetch failed —
  // never treated as owner, so every gate below fails closed until load.
  let myPermission: string | null | undefined = undefined;
  $: permissionLoaded = myPermission !== undefined;
  // `my_permission` reads "owner" for an admin under a support grant, so a READ-ONLY grant
  // must be subtracted here (the server refuses the write regardless; this hides the controls).
  $: canEdit =
    permissionLoaded &&
    !$supportSessionGate.readOnly &&
    (myPermission === null || myPermission === 'editor' || myPermission === 'owner');

  // Content redaction: owner/admin can reveal the original (non-admin-forced categories).
  let showOriginal = false;
  let redactionActive = false; // true once we've seen redacted spans on this file
  // When redaction is enabled but detection hasn't finished, the transcript is withheld.
  let redactionPending = false;
  let redactionStatus = ''; // pending | processing | done | failed
  // Backend-decided: the viewer's policy would mask something (switch on AND a category).
  let redactionEnabled = false;
  $: canViewOriginal = permissionLoaded && (myPermission === null || myPermission === 'owner');
  $: showRedactionToggle = canViewOriginal && (redactionActive || showOriginal);

  $: llmAvailable = $isLLMAvailable;

  // When true, suppress all speaker-specific UI
  $: diarizationDisabled = file?.diarization_disabled === true;

  $: speakerNamesChanged = hasUnsavedSpeakerNames(speakerList, originalSpeakerNames);

  $: segmentEditUnsaved = hasUnsavedSegmentEdit({
    id: editingSegmentId,
    text: editingSegmentText,
    originalText: editingSegmentOriginalText,
  });

  // Issue #787: the "unsaved" dot beside the speaker panel used to be the ENTIRE
  // protection — a nav click or a tab close discarded a speaker-naming pass silently.
  // The predicate is re-read on every navigation, so saving clears the guard with it.
  $: hasUnsavedEdits = speakerNamesChanged || segmentEditUnsaved;
  guardUnsavedChanges(
    () => hasUnsavedEdits,
    () => $t('transcript.unsavedChangesPrompt')
  );

  // Reset spinners when LLM becomes unavailable
  $: if (!llmAvailable && (summaryGenerating || generatingSummary)) {
    summaryGenerating = false;
    generatingSummary = false;
  }

  // Transcript export / copy (server-side, issues #673/#821/#1278): see TranscriptExportDialogs.
  let exportDialogs: TranscriptExportDialogs;
  let documentBusy = false;
  let copyStatus: 'idle' | 'copying' | 'copied' | 'failed' | 'empty' = 'idle';

  // Update-globally / create-new dialog for a renamed speaker linked to a profile
  let speakerProfileDialog: { title: string; message: string } | null = null;

  /** Apply the redaction fields of a `GET /files/{id}` read. */
  function applyRedactionState(data: FileDetailPayload): void {
    const state = readRedactionState(data);
    redactionPending = state.redactionPending;
    redactionStatus = state.redactionStatus;
    redactionEnabled = state.redactionEnabled;
    if (sawMaskedSpans(data, showOriginal)) redactionActive = true;
  }

  /** `transcript_ready`: refresh the transcript without overwriting the rest of `file`. */
  async function fetchTranscriptData(): Promise<void> {
    if (!fileId) {
      console.error('FileDetail: No file ID provided to fetchTranscriptData');
      return;
    }

    try {
      const response = await axiosInstance.get(`/files/${fileId}`);

      if (response.data && typeof response.data === 'object' && file) {
        collections = response.data.collections || [];
        file = mergeTranscriptRefresh(file, response.data);
        processTranscriptData();
      }
    } catch (error) {
      console.error('Error fetching transcript data:', error);
    }
  }

  /** @param fromPrefetch First load only: join the gallery's hover prefetch (see getFileDetail). */
  async function fetchFileDetails(fileIdOrEvent?: string, fromPrefetch = false): Promise<void> {
    const targetFileId = typeof fileIdOrEvent === 'string' ? fileIdOrEvent : fileId;

    if (!targetFileId) {
      console.error('FileDetail: No file ID provided to fetchFileDetails');
      pageErrorMessage = $t('fileDetail.noFileIdProvided');
      isLoading = false;
      return;
    }

    try {
      isLoading = true;
      pageErrorMessage = '';
      fileNotFound = false;
      videoErrorMessage = '';

      const data = await getFileDetail(targetFileId, { fromPrefetch, showOriginal });
      if (!data || typeof data !== 'object') throw new Error('Invalid response format');

      const detail = data as FileDetailPayload;
      ({ file, collections } = splitFileDetail(detail));
      // The ONLY source of the caller's permission; every gate above derives from it.
      myPermission = (detail.my_permission as string | null | undefined) ?? null;
      applyRedactionState(detail);
      const pagination = paginationFrom(detail);
      totalSegments = pagination.totalSegments;
      totalSpeakerSegments = pagination.totalSpeakerSegments;
      segmentLimit = pagination.segmentLimit;

      // Set up video URL only if file might have media available
      if (detail.status !== 'error' && detail.status !== 'cancelled') {
        void setupVideoUrl(targetFileId, fromPrefetch);
      }

      processTranscriptData();
      isLoading = false;
    } catch (error) {
      console.error('Error fetching file details:', error);
      const status = getErrorStatus(error);
      if (status === 404 || status === 403) {
        fileNotFound = true;
        pageErrorMessage = $t('fileDetail.notFoundOrNoAccess');
      } else {
        pageErrorMessage = $t('fileDetail.failedToLoadFile');
      }
      isLoading = false;
    }
  }

  // "Show original" (offered only when `canViewOriginal`): swaps the transcript in place.
  let redactionToggleBusy = false;
  const { toggleShowOriginal } = createRedactionView({
    getFileId: () => fileId,
    getFile: () => file,
    setFile: (f) => (file = asFileDetailState(f)),
    getShowOriginal: () => showOriginal,
    setShowOriginal: (value) => (showOriginal = value),
    isToggleBusy: () => redactionToggleBusy,
    setToggleBusy: (busy) => (redactionToggleBusy = busy),
    applyRedactionState,
    onTranscriptReloaded: () => processTranscriptData(),
  });

  async function triggerRedaction(): Promise<void> {
    if (!file?.uuid) return;
    if (await startRedactionScan(file.uuid)) {
      redactionPending = true;
      redactionStatus = 'pending';
    }
  }

  const segmentPager = createSegmentPager({
    getFileId: () => fileId,
    getFile: () => file,
    setFile: (f) => (file = asFileDetailState(f)),
    getShowOriginal: () => showOriginal,
    getSegmentLimit: () => segmentLimit,
    getTotalSegments: () => totalSegments,
    setTotalSegments: (total) => (totalSegments = total),
    isLoadingMore: () => loadingMoreSegments,
    setLoadingMore: (loading) => (loadingMoreSegments = loading),
    hasMoreSegments: () => hasMoreSegments,
    syncTranscriptStore: () => syncTranscriptStore(file, speakerList),
    schedule: scheduleTimeout,
    notifyError: (key) => toastStore.error($t(key)),
  });
  const loadMoreSegments = segmentPager.loadMore;

  function handleLoadUpTo(event: CustomEvent<{ targetIndex: number; segmentUuid: string }>) {
    void segmentPager.loadUpTo(event.detail.targetIndex, event.detail.segmentUuid);
  }

  async function loadAISuggestions(): Promise<void> {
    if (!fileId) return;
    const suggestions = await fetchAISuggestions(fileId);
    if (suggestions) {
      aiTagSuggestions = suggestions.tags;
      aiCollectionSuggestions = suggestions.collections;
    }
  }

  /** Reload the speakers for freshly loaded transcript data. */
  function processTranscriptData() {
    if (!file || !Array.isArray(file.transcript_segments)) return;
    void loadSpeakers();
  }

  // Presigned media URL, refreshed before it expires during long playback.
  const mediaStream = createMediaStream((newUrl) => (videoUrl = newUrl));

  async function setupVideoUrl(targetFileId: string, keepCached = false) {
    try {
      const stream = await mediaStream.open(targetFileId, keepCached);
      // Content type and mode first, so the player picks <audio> vs <video> correctly.
      streamContentType = stream.contentType;
      playbackMode = stream.playbackMode;
      videoUrl = stream.url;
    } catch (error) {
      console.error('Failed to get video URL:', error);
      videoUrl = '';
      videoErrorMessage = 'Video not available for this file';
    }
  }

  function handleSegmentClick(event: CustomEvent<{ startTime: number }>) {
    const startTime = event.detail.startTime;
    seekToTime(startTime);
  }

  // The editor mutated a speaker in place; reassign so `speakerNamesChanged` recomputes.
  function handleSpeakerNameChanged() {
    speakerList = [...speakerList];
  }

  // A deleted speaker (segment reassignment left it empty) needs the same full refresh as a
  // merge, or the editor, merge panel and analytics drift apart.
  async function handleSpeakerDeleted(event: CustomEvent<{ speakerUuid?: string }>) {
    if (!event.detail.speakerUuid) return;
    await speakers.refreshAfterMerge();
  }

  // The backend recomputed analytics after a speaker/text change; fetch them.
  async function handleAnalyticsRefreshNeeded() {
    if (!file?.uuid) return;
    const result = await fetchFileAnalytics(file.uuid);
    if (result && file) file.analytics = result.analytics;
  }

  function handleSpeakerUpdate(event: CustomEvent<{ speakerId: string; newName: string }>) {
    void speakers.requestSpeakerRename(event.detail.speakerId, event.detail.newName);
  }

  /** Re-fetch the player's caption track (server-built) after a committed change. */
  function refreshCaptionTrack(reason: string): void {
    tick()
      .then(() => filePlayer?.updateSubtitles())
      .catch(error => console.warn(`Failed to update captions after ${reason}:`, error));
  }

  // Inline segment editing; the state stays here so an open edit survives a reload.
  const segmentEditor = createSegmentEditor({
    getFileId: () => fileId,
    getFile: () => file,
    setFile: (f) => (file = asFileDetailState(f)),
    getEdit: () => ({ id: editingSegmentId, text: editingSegmentText, originalText: editingSegmentOriginalText }),
    setEdit: (edit) => {
      editingSegmentId = edit.id;
      editingSegmentText = edit.text;
      editingSegmentOriginalText = edit.originalText;
    },
    setSaving: (saving) => (savingTranscript = saving),
    refreshCaptionTrack,
    refreshAnalytics: () => void handleAnalyticsRefreshNeeded(),
  });

  function handleExportTranscript(event: CustomEvent<{ format: string }>) {
    if (!file || !file.transcript_segments) return;
    // CommentSection already holds this and reports it as it changes.
    exportDialogs.openExport(event.detail.format, commentCount > 0);
  }

  function handleSeekTo(event: CustomEvent<{ time: number } | number>) {
    const detail = event.detail;
    const time = typeof detail === 'number' ? detail : detail.time;
    seekToTime(time);
  }

  function seekToTime(time: number) {
    if (filePlayer) filePlayer.seekToTime(time);
    else console.warn('VideoPlayer component not available for seeking');
  }

  // `file.tags` is the update path: assigning a member of the reactive `file`
  // invalidates it, which re-renders TagsSection with the new tag objects.
  function handleTagsUpdated(event: CustomEvent<{ tags: Tag[] }>) {
    if (file) {
      file.tags = event.detail.tags;
    }
  }

  function handleCollectionsUpdated(event: CustomEvent<{ collections: Collection[] }>) {
    const { collections: updatedCollections } = event.detail;

    collections = updatedCollections;

    if (file) {
      file.collections = updatedCollections;
      file = { ...file }; // Trigger reactivity
    }
  }

  function handleVideoRetry() {
    void fetchFileDetails();
  }

  // Handle reprocess event from the SelectiveReprocessModal (header button). The modal has
  // already made the API call; this is the optimistic UI, and websocket notifications
  // carry the rest (no immediate refetch).
  async function handleReprocessFromModal(event: CustomEvent<{ fileId: string; stages?: string[] }>) {
    const { fileId: reprocessFileId, stages } = event.detail;

    try {
      reprocessing = true;

      lastProcessedNotificationState = '';

      if (file) {
        file = applyReprocessStart(file, stages);
        if (reprocessRegeneratesSummary(stages, llmAvailable)) {
          summaryGenerating = true;
          generatingSummary = true;
        }
      }
    } catch (error) {
      console.error('Error handling reprocess event:', error);

      // Revert optimistic update on error
      if (file) {
        await fetchFileDetails(reprocessFileId);
      }
    } finally {
      reprocessing = false;
    }
  }

  // Live updates: websocket notifications for this file, and the speaker CustomEvents.
  let wsUnsubscribe: () => void;
  let stopSpeakerEvents: () => void;

  function handleNotifications(notifications: Notification[]) {
    const latest = latestNotificationForFile(notifications, fileId);
    if (!latest) return;
    // Only act when the content changed (progressive notifications update in place).
    const signature = notificationSignature(latest);
    if (signature === lastProcessedNotificationState) return;
    lastProcessedNotificationState = signature;
    // Skip if fileId is not set yet (component still initializing)
    if (!fileId || !isForFile(latest, fileId)) return;

    handleFileNotification(latest, {
      getFileId: () => fileId,
      getFile: () => file,
      getLlmAvailable: () => llmAvailable,
      getRedactionStatus: () => redactionStatus,
      getVideoPlayerComponent: () => filePlayer,
      setFile: (f) => (file = asFileDetailState(f)),
      setCurrentProcessingStep: (s) => (currentProcessingStep = s),
      setSummaryGenerating: (v) => (summaryGenerating = v),
      setGeneratingSummary: (v) => (generatingSummary = v),
      setReprocessing: (v) => (reprocessing = v),
      setRedactionStatus: (s) => (redactionStatus = s),
      setRedactionPending: (v) => (redactionPending = v),
      fetchTranscriptData,
      loadSpeakers,
      loadAISuggestions,
      fetchFileDetails,
      t: $t,
      toastError: (message, durationMs) => toastStore.error(message, durationMs),
      toastInfo: (message) => toastStore.info(message),
    });
  }

  onMount(() => {
    stopSpeakerEvents = speakers.listen();

    if (id) {
      fileId = id;
    } else {
      console.error('FileDetail: No id parameter provided');
      const urlParams = new URLSearchParams(window.location.search);
      const pathParts = window.location.pathname.split('/');
      fileId = urlParams.get('id') || pathParts[pathParts.length - 1] || '';
    }

    if (fileId) {
      fetchFileDetails(undefined, true).catch(err => {
        console.error('Error loading file details:', err);
      });

      loadAISuggestions().catch(err => {
        console.error('Error loading AI suggestions:', err);
      });
    } else {
      pageErrorMessage = $t('fileDetail.invalidFileId');
      isLoading = false;
    }

    wsUnsubscribe = websocketStore.subscribe(($ws) => handleNotifications($ws.notifications));
  });

  onDestroy(() => {
    // Cancel every deferred DOM callback FIRST; it also stops anything still in flight
    // (a late fetch resolution) from scheduling a new one after this teardown has run.
    pageTimers.destroy();
    downloadManager.cleanup();

    mediaStream.stop();

    if (wsUnsubscribe) {
      wsUnsubscribe();
    }

    stopSpeakerEvents?.();

    transcriptStore.clear();
  });

</script>

<svelte:head>
  <title>{file?.filename ||
    (fileNotFound ? $t('fileDetail.notFoundTitle') : $t('fileDetail.loadingFile'))}</title>
</svelte:head>

<div class="file-detail-page">
  {#if isLoading}
    <!-- Skeleton loader mirroring the final layout — perceived as ~20% faster
         than a spinner because users see structure + anticipated content -->
    <FileDetailSkeleton />
  {:else if pageErrorMessage}
    <div class="error-container">
      <p class="error-message">{pageErrorMessage}</p>
      {#if fileNotFound}
        <a href={resolve('/')} class="back-to-gallery-link">{$t('nav.backToGallery')}</a>
      {:else}
        <button
          on:click={() => fetchFileDetails()}
          title={$t('fileDetail.retryTooltip')}
        >{$t('fileDetail.tryAgain')}</button>
      {/if}
    </div>
  {:else if file}
    <div class="file-header">
      <FileHeader
        {file}
        {currentProcessingStep}
        sharedPermission={myPermission ?? null}
        on:titleUpdated={(e) => { if (file) file.title = e.detail.title; }}
      />
    </div>

    <div class="main-content-grid">
      <!-- Left column: video player, actions, speaker editing, tags/collections/analytics,
           file details (issue #748 — reordered around speaker editing). -->
      <section class="video-column">
        <div class="video-header">
          <h4>{file?.content_type?.startsWith('audio/') ? $t('fileDetail.audio') : $t('fileDetail.video')}</h4>
          <!-- Action Buttons - right aligned above video -->
          <FileActionButtons
            {file}
            {canEdit}
            {llmAvailable}
            llmSettingsUnavailable={$isLLMSettingsUnavailable}
            {summaryGenerating}
            {generatingSummary}
            {reprocessing}
            on:viewTranscript={() => (showTranscriptModal = true)}
            on:showSummary={() => fileSummary.show()}
            on:generateSummary={() => fileSummary.generate()}
            on:openReprocess={() => (showReprocessModal = true)}
          />
        </div>

        <FilePlayer
          bind:this={filePlayer}
          bind:currentTime
          {file}
          {videoUrl}
          {streamContentType}
          {playbackMode}
          errorMessage={videoErrorMessage}
          {showOriginal}
          schedule={scheduleTimeout}
          {initialSeekTime}
          on:retry={handleVideoRetry}
          on:initialSeekDone={() => (initialSeekTime = null)}
        />

        {#if file && file.transcript_segments && file.transcript_segments.length > 0}
          <TranscriptActionsBar
            {file}
            {diarizationDisabled}
            {isEditingSpeakers}
            {isDownloading}
            {currentDownload}
            {isVideoFile}
            {canEmbedSubtitles}
            {documentBusy}
            on:exportTranscript={handleExportTranscript}
            on:toggleSpeakerEditor={() => (isEditingSpeakers = !isEditingSpeakers)}
            on:download={handleDownloadFromBar}
          />
        {/if}

        <!-- Issue #748 §5.5: the speaker editor REPLACES Tags/Collections/File-details
             while open (you don't edit tags while you're editing speakers) — Analytics
             stays visible, since it is the speaker context being labelled against (J7). -->
        {#if isEditingSpeakers && !diarizationDisabled}
          <SpeakerEditorPanel
            {file}
            {speakerList}
            {speakerNamesChanged}
            {savingSpeakers}
            on:speakerNameChanged={handleSpeakerNameChanged}
            on:speakerUpdate={handleSpeakerUpdate}
            on:speakersMerged={speakers.refreshAfterMerge}
            on:saveSpeakerNames={speakers.saveAllSpeakerNames}
            on:segmentClick={handleSegmentClick}
            on:loadUpTo={handleLoadUpTo}
          />
        {:else}
          <TagsSection
            {file} {canEdit}
            bind:isTagsExpanded
            {aiTagSuggestions}
            on:tagsUpdated={handleTagsUpdated}
          />

          <CollectionsSection
            bind:collections
            fileId={file?.uuid}
            bind:isExpanded={isCollectionsExpanded}
            {aiCollectionSuggestions}
            on:collectionsUpdated={handleCollectionsUpdated}
          />
        {/if}

        <AnalyticsSection
          {file}
          bind:isAnalyticsExpanded
          {speakerList}
          transcriptStore={$transcriptStore}
          {diarizationDisabled}
        />

        {#if !isEditingSpeakers}
          <AudioTracksNotice
            streams={file?.audio_streams}
            chosen={file?.audio_stream_index}
            detectedLanguage={file?.language}
            mismatch={file?.audio_language_mismatch === true}
            trackLanguage={file?.audio_track_language}
            canReprocess={canEdit && file?.hold?.held !== true}
            on:chooseTrack={() => (showReprocessModal = true)}
          />
          <MetadataDisplay
            {file}
            bind:showMetadata
          />
          <ProcessingSection steps={file?.processing_steps ?? []} />
        {/if}
      </section>

      <!-- Right column: Transcript, then comments (issue #748 — comments moved here from
           the left column, below everything else in the transcript column). -->
      <section class="transcript-column">
        {#if redactionPending}
          <RedactionPendingPanel />
        {:else if file && file.transcript_segments}
          <TranscriptDisplay
          bind:file
          {currentTime}
          {savingTranscript}
          {editingSegmentId}
          bind:editingSegmentText
          {speakerList}
          {diarizationDisabled}
          {totalSegments}
          {hasMoreSegments}
          {loadingMoreSegments}
          on:segmentClick={handleSegmentClick}
          on:editSegment={(e) => segmentEditor.open(e.detail.segment)}
          on:saveSegment={(e) => segmentEditor.save(e.detail.segment)}
          on:cancelEditSegment={segmentEditor.cancel}
          on:speakerUpdate={handleSpeakerUpdate}
          on:speakerCreated={loadSpeakers}
          on:speakerDeleted={handleSpeakerDeleted}
          on:analyticsRefreshNeeded={() => {
            // Fired after a committed segment-speaker change: the cues' names moved too.
            void handleAnalyticsRefreshNeeded();
            refreshCaptionTrack('segment speaker change');
          }}
          on:loadMore={loadMoreSegments}
        />
          <!-- Content-redaction controls: kept BELOW the transcript so the video and -->
          <!-- transcript columns stay top-aligned. -->
          <RedactionControls
            {showRedactionToggle}
            {canViewOriginal}
            {redactionEnabled}
            {redactionStatus}
            {showOriginal}
            {redactionToggleBusy}
            on:rescan={triggerRedaction}
            on:toggleOriginal={toggleShowOriginal}
          />
        {:else}
          <div class="no-transcript">
            {#if file?.status === 'processing' || file?.status === 'pending'}
              <div class="processing-placeholder">
                <Spinner size="large" />
                <p>{$t('fileDetail.generatingTranscript')}</p>
                <small>{$t('fileDetail.generatingTranscriptHint')}</small>
              </div>
            {:else}
              <p>{$t('fileDetail.noTranscript')}</p>
            {/if}
          </div>
        {/if}

        <!-- Outside the branches on purpose: comments are the viewer's own annotations, not
             transcript text, so a transcript withheld for redaction must not take the form
             with it. One instance also keeps the list across a branch change. -->
        <CommentSection
          fileId={file?.uuid ? String(file.uuid) : ''}
          {currentTime}
          duration={file?.duration ?? null}
          on:seekTo={handleSeekTo}
          on:commentsChanged={(e) => (commentCount = e.detail.count)}
        />
      </section>
    </div>

  {:else}
    <div class="no-file-container">
      <p>{$t('fileDetail.fileNotFound')}</p>
    </div>
  {/if}
</div>

<!-- Selective Reprocess Modal (outside conditional blocks to prevent flicker) -->
<SelectiveReprocessModal
  bind:showModal={showReprocessModal}
  {file}
  bind:reprocessing
  on:reprocess={handleReprocessFromModal}
/>

<TranscriptExportDialogs
  bind:this={exportDialogs}
  bind:documentBusy
  bind:copyStatus
  {file}
  {diarizationDisabled}
  {redactionActive}
  {showOriginal}
  {canViewOriginal}
  schedule={scheduleTimeout}
/>

<!-- Speaker Profile Confirmation Modal -->
{#if speakerProfileDialog}
  <SpeakerProfileConfirmModal
    title={speakerProfileDialog.title}
    message={speakerProfileDialog.message}
    on:updateProfile={() => speakers.decideProfile('update_profile')}
    on:createNewProfile={() => speakers.decideProfile('create_new_profile')}
    on:cancel={speakers.cancelProfileDecision}
  />
{/if}

<!-- Summary Modal -->
{#if file?.uuid}
  <FileSummary
    bind:this={fileSummary}
    bind:file
    bind:summaryGenerating
    bind:generatingSummary
    {isLoading}
  />
{/if}

<!-- Transcript view modal (issue #755 — consolidated with the search-result surface) -->
{#if file?.uuid}
  <TranscriptViewModal
    mode="file"
    bind:isOpen={showTranscriptModal}
    {file}
    {speakerList}
    fileName={file?.filename || 'Unknown File'}
    totalSegments={totalSpeakerSegments}
    {hasMoreSegments}
    {loadingMoreSegments}
    {diarizationDisabled}
    {showRedactionToggle}
    {showOriginal}
    {redactionToggleBusy}
    {copyStatus}
    on:toggleRedaction={toggleShowOriginal}
    on:copyTranscript={() => exportDialogs.copyTranscript()}
    on:close={() => showTranscriptModal = false}
    on:loadMore={loadMoreSegments}
  />
{/if}

<style>
  div.file-detail-page {
    padding: var(--space-8);
    max-width: 1200px;
    margin: 0 auto;
    font-family: var(--font-family-sans);
    color: var(--text-color);
  }

  .error-container,
  .no-file-container {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    min-height: 50vh;
    text-align: center;
  }

  .error-message {
    color: var(--error-text);
    margin-bottom: var(--space-4);
  }

  .error-container button {
    background: var(--primary-color);
    color: var(--text-on-solid);
    border: none;
    padding: var(--space-2-5) var(--space-5);
    border-radius: var(--radius-md);
    cursor: pointer;
    font-size: var(--font-size-md);
    font-weight: var(--font-weight-medium);
  }

  .error-container button:hover {
    background: var(--primary-hover);
  }

  .back-to-gallery-link {
    color: var(--primary-on-surface);
    font-size: var(--font-size-md);
    font-weight: var(--font-weight-medium);
    text-decoration: none;
  }

  .back-to-gallery-link:hover {
    text-decoration: underline;
  }


  .file-header {
    margin-bottom: var(--space-6);
  }


  .main-content-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: var(--space-8);
    align-items: start;
  }

  .video-column {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
  }

  /* The actions wrap under the heading when they do not fit beside it; the heading never
     gives way (it broke mid-word as "Vide" / "o" on a held file). */
  .video-header {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-2);
    justify-content: space-between;
    align-items: center;
    margin-bottom: 0;
    min-height: 32px;
  }

  .video-column h4 {
    flex-shrink: 0;
    margin: 0;
    font-size: 18px;
    font-weight: var(--font-weight-semibold);
    color: var(--text-primary);
  }

  .transcript-column {
    display: flex;
    flex-direction: column;
    gap: var(--space-4);
  }


  @media (max-width: 1024px) {
    .main-content-grid {
      grid-template-columns: 1fr;
      gap: var(--space-6);
    }
  }

  @media (max-width: 768px) {
    div.file-detail-page {
      padding: var(--space-4);
    }

    .main-content-grid {
      gap: var(--space-5);
    }

    .video-header {
      flex-wrap: wrap;
      gap: var(--space-2);
    }
  }

  /* Transcript segment highlighting styles */
  :global(.transcript-segment .segment-content) {
    border: 1px solid transparent;
    box-shadow: none;
    transition: all var(--duration-base) ease;
  }

  :global(.transcript-segment.active-segment .segment-content) {
    background-color: rgba(var(--accent-rgb), 0.12);
    border-color: rgba(var(--accent-rgb), 0.3);
    box-shadow: var(--shadow-primary);
  }

  :global(.transcript-segment.active-segment .segment-content:hover) {
    background-color: rgba(var(--accent-rgb), 0.16);
    border-color: rgba(var(--accent-rgb), 0.4);
  }

</style>
