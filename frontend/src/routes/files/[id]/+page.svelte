<script lang="ts">
  import { onMount, onDestroy, afterUpdate, tick } from 'svelte';
  import type { Collection } from '$lib/types/collection';
  import type { Tag } from '$lib/types/tag';
  import { guardUnsavedChanges } from '$lib/navigation/unsavedChangesGuard';
  import axiosInstance from '$lib/axios';
  import { websocketStore, type Notification } from '$stores/websocket';
  import { handleFileNotification } from '$lib/fileDetail/notificationHandler';
  import { isForFile, latestNotificationForFile, notificationSignature } from '$lib/fileDetail/notificationSelect';
  import { listenForSpeakerEvents } from '$lib/fileDetail/speakerEvents';
  import { createPageTimers } from '$lib/fileDetail/pageTimers';
  import {
    fetchFileAnalytics,
    getFileDetail,
    mergeTranscriptRefresh,
    paginationFrom,
    readRedactionState,
    sawMaskedSpans,
    startRedactionScan,
    type FileDetailPayload,
  } from '$lib/fileDetail/fileDetailData';
  import { createSegmentPager, redactParams } from '$lib/fileDetail/segmentPaging';
  import { createSpeakerLoader, syncTranscriptStore } from '$lib/fileDetail/speakerLoader';
  import { createSpeakerSaveFlow } from '$lib/fileDetail/speakerSave';
  import {
    hasUnsavedSpeakerNames,
    type SpeakerItem,
    type SpeakerNameBaseline,
  } from '$lib/fileDetail/speakerNames';
  import { markActiveSegment, scrollToSegmentAtTime } from '$lib/fileDetail/segmentDom';
  import { patchSegmentInFile } from '$lib/fileDetail/segmentSync';
  import { clearProcessedVideoCache, saveSegmentText, segmentSaveErrorKey } from '$lib/fileDetail/segmentEdit';

  import VideoPlayer from '$components/VideoPlayer.svelte';
  import WaveformPlayer from '$components/WaveformPlayer.svelte';
  import MetadataDisplay from '$components/MetadataDisplay.svelte';
  import ProcessingSection from '$components/fileDetail/ProcessingSection.svelte';
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
  import SummaryModal from '$components/SummaryModal.svelte';
  import TranscriptViewModal from '$components/transcript/TranscriptViewModal.svelte';
  import TranscriptExportDialogs from '$components/fileDetail/TranscriptExportDialogs.svelte';
  import FileActionButtons from '$components/fileDetail/FileActionButtons.svelte';
  import RedactionControls from '$components/fileDetail/RedactionControls.svelte';
  import RedactionPendingPanel from '$components/fileDetail/RedactionPendingPanel.svelte';
  import SpeakerProfileConfirmModal from '$components/fileDetail/SpeakerProfileConfirmModal.svelte';
  import { isLLMAvailable } from '$stores/llmStatus';
  import { transcriptStore } from '$stores/transcriptStore';
  import { downloadStore } from '$stores/downloads';
  import { supportSessionGate } from '$stores/supportSession';
  import { getAISuggestions, type TagSuggestion, type CollectionSuggestion } from '$lib/api/suggestions';
  import type { SummarizeOptions } from '$lib/api/summarize';
  import { fetchSummaryData, startSummary } from '$lib/fileDetail/summaryActions';
  import type { PlaybackMode } from '$lib/api/mediaUrl';
  import { createMediaStream } from '$lib/fileDetail/mediaStream';
  import { applyReprocessStart, reprocessRegeneratesSummary } from '$lib/fileDetail/reprocess';
  import Spinner from '$components/ui/Spinner.svelte';
  import FileDetailSkeleton from '$components/FileDetailSkeleton.svelte';

  // Props - SvelteKit passes data from +page.ts
  export let data;
  $: id = data.id;

  // State variables
  let file: any = null;
  let fileId = '';
  let videoUrl = '';
  let streamContentType = '';
  let playbackMode: PlaybackMode | null = null;
  let pageErrorMessage = '';
  // 404/403: the file is missing or not visible to this user; retrying can't help.
  let fileNotFound = false;
  let videoErrorMessage = '';
  let videoPlayerComponent: any = null;
  let currentTime = 0;
  let isLoading = true;
  let playerInitialized = false;
  let videoElementChecked = false;
  let collections: Collection[] = [];

  // DOM-touching deferred work goes through this so onDestroy can cancel it (see pageTimers).
  const pageTimers = createPageTimers();
  const scheduleTimeout = pageTimers.schedule;

  // UI state
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
  // Issue #748 §5.2: this used to be passed one-way into `TranscriptDisplay`, which
  // toggled its OWN local copy — the page's copy was never set to `true`, so the
  // `isEditingSpeakers = false` after a successful bulk save (below) was a dead
  // statement and the panel never actually closed. `TranscriptActionsBar` and
  // `SpeakerEditorPanel` now render directly in this page, so the page owns the
  // flag outright with no prop-forwarding indirection to lose it in.
  let isEditingSpeakers = false;

  // The download SSE stream lives with the other `$lib/fileDetail/*` orchestration
  // modules; `TranscriptActionsBar`, which triggers it, renders on this page.
  const downloadManager = createDownloadStreamManager();
  $: downloadState = $downloadStore;
  $: currentDownload = downloadState[file?.uuid];
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
  const { loadSpeakers } = createSpeakerLoader({
    getFile: () => file,
    getSpeakerList: () => speakerList,
    setSpeakerList: (speakers) => (speakerList = speakers),
    getBaseline: () => originalSpeakerNames,
    setBaseline: (baseline) => (originalSpeakerNames = baseline),
  });
  // Reported by CommentSection; the export flow reads it instead of refetching comments.
  let commentCount = 0;
  let reprocessing = false;
  let showReprocessModal = false;
  let summaryData: any = null;
  let showSummaryModal = false;
  let showTranscriptModal = false;
  let generatingSummary = false;
  let summaryGenerating = false; // WebSocket-driven summary generation status
  let currentProcessingStep = ''; // Current processing step from WebSocket notifications
  let lastProcessedNotificationState = ''; // Track processed notification state globally

  // Transcript pagination state
  let totalSegments = 0;
  let totalSpeakerSegments = 0;
  let segmentLimit = 500;
  let segmentOffset = 0;
  let loadingMoreSegments = false;
  $: hasMoreSegments = totalSegments > (file?.transcript_segments?.length || 0);

  // AI Suggestions state
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

  // LLM availability for summary functionality
  $: llmAvailable = $isLLMAvailable;

  // Diarization disabled flag — when true, suppress all speaker-specific UI
  $: diarizationDisabled = file?.diarization_disabled === true;

  // Detect changes in speaker names - depends on speaker display_name values
  $: speakerNamesChanged = hasUnsavedSpeakerNames(speakerList, originalSpeakerNames);

  // An open segment editor only counts as unsaved once the text actually differs — opening
  // one and closing the tab must not prompt. Trimmed on both sides so trailing whitespace
  // the textarea introduced is not "an edit".
  $: segmentEditUnsaved =
    editingSegmentId !== null &&
    editingSegmentText.trim() !== editingSegmentOriginalText.trim();

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
      file = detail;
      collections = (detail.collections as Collection[]) || [];
      // The ONLY source of the caller's permission; every gate above derives from it.
      myPermission = (detail.my_permission as string | null | undefined) ?? null;
      applyRedactionState(detail);
      const pagination = paginationFrom(detail);
      totalSegments = pagination.totalSegments;
      totalSpeakerSegments = pagination.totalSpeakerSegments;
      segmentLimit = pagination.segmentLimit;
      segmentOffset = pagination.segmentOffset;

      // Set up video URL only if file might have media available
      if (file.status !== 'error' && file.status !== 'cancelled') {
        setupVideoUrl(targetFileId, fromPrefetch);
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

  /**
   * Toggle between the redacted view and the original (owner/admin only).
   * Refreshes ONLY the transcript data — no isLoading flip, no video/player
   * re-setup — so the text swaps in place without a full-page flicker.
   * Admin-forced categories always remain masked regardless of this toggle.
   */
  let redactionToggleBusy = false;

  async function toggleShowOriginal(): Promise<void> {
    if (redactionToggleBusy) return;
    redactionToggleBusy = true;
    showOriginal = !showOriginal;
    try {
      await refreshTranscriptOnly();
    } finally {
      redactionToggleBusy = false;
    }
  }

  /**
   * Re-fetch transcript segments (honoring the current redact flag) and update
   * transcript-related state in place. Everything else on the page — player,
   * metadata, analytics — is left untouched.
   */
  async function refreshTranscriptOnly(): Promise<void> {
    try {
      const response = await axiosInstance.get(`/files/${fileId}`, {
        params: redactParams(showOriginal),
      });
      if (response.data && typeof response.data === 'object') {
        // Replace BOTH segment representations: TranscriptDisplay prefers the
        // backend-pre-grouped `grouped_segments`, so leaving the stale copy in
        // place would keep rendering the old (masked/unmasked) text.
        file = {
          ...file,
          transcript_segments: response.data.transcript_segments,
          grouped_segments: response.data.grouped_segments
        };
        applyRedactionState(response.data);
        processTranscriptData();
      }
    } catch (error) {
      console.error('Error refreshing transcript:', error);
      toastStore.error($t('fileDetail.failedToLoadFile'));
    }
  }

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
    setFile: (f) => (file = f),
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
    segmentPager.loadUpTo(event.detail.targetIndex, event.detail.segmentUuid);
  }

  /**
   * Load AI suggestions for tags and collections
   *
   * Always loads suggestions if they exist in the database, regardless of current LLM configuration.
   * This ensures users can see and use previously generated suggestions even if LLM is no longer configured.
   */
  async function loadAISuggestions(): Promise<void> {
    if (!fileId) return;

    try {
      const suggestions = await getAISuggestions(fileId);
      // Load suggestions regardless of status or current LLM configuration
      // The UI components will handle display logic based on status
      if (suggestions && suggestions.status !== 'rejected') {
        aiTagSuggestions = suggestions.tags || [];
        aiCollectionSuggestions = suggestions.collections || [];
      }
    } catch (error) {
      console.error('Error loading AI suggestions:', error);
      // Silent fail - suggestions are optional (404 is expected if none exist)
    }
  }

  /** Reload the speakers for freshly loaded transcript data. */
  function processTranscriptData() {
    if (!file || !Array.isArray(file.transcript_segments)) return;
    loadSpeakers();
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
      // Reset video element check flag to prompt afterUpdate to try initialization
      videoElementChecked = false;
    } catch (error) {
      console.error('Failed to get video URL:', error);
      videoUrl = '';
      videoErrorMessage = 'Video not available for this file';
    }
  }

  /**
   * Initialize the video player with enhanced streaming capabilities
   */
  function initializePlayer() {
    if (playerInitialized || !videoUrl) {
      return;
    }

    const mediaElement = document.querySelector('#player') as HTMLMediaElement;

    if (!mediaElement) {
      return;
    }

    // Mark as initialized - all player initialization is now handled by VideoPlayer component
    playerInitialized = true;
  }

  // Event handlers for components
  function handleSegmentClick(event: any) {
    const startTime = event.detail.startTime;
    seekToTime(startTime);
  }

  // Handle speaker name input changes (for reactivity)
  function handleSpeakerNameChanged(event: CustomEvent) {
    // Trigger reactivity by reassigning the speakerList array
    // This ensures the reactive statement detects the change
    speakerList = [...speakerList];
  }

  // Handle speakers merged event - refresh all related data silently (no loading spinner)
  async function handleSpeakersMerged() {
    if (!file?.uuid) return;

    try {
      // Silently refresh file data without showing loading state.
      // Honour the reveal flag, as refreshTranscriptOnly does — without it, merging
      // speakers while "show original" is on silently re-masks the transcript.
      const response = await axiosInstance.get(`/files/${file.uuid}`, {
        params: redactParams(showOriginal)
      });

      if (response.data && typeof response.data === 'object') {
        // Update file data (includes analytics and transcript segments)
        file = response.data;
        collections = response.data.collections || [];

        // Process transcript data from the refreshed response
        processTranscriptData();
      }

      // Reload speakers to get updated list
      await loadSpeakers();
    } catch (error) {
      console.error('Error refreshing data after speaker merge:', error);
      toastStore.error($t('fileDetail.dataRefreshFailed'));
    }
  }

  // Handle new speaker created - just reload speakers without touching file/analytics
  // The segment assignment will trigger analyticsRefreshNeeded which handles analytics
  async function handleSpeakerCreated() {
    await loadSpeakers();
  }

  // Handle speaker deletion after segment reassignment leaves a speaker with no segments
  async function handleSpeakerDeleted(event: CustomEvent) {
    const { speakerUuid } = event.detail;
    if (!speakerUuid) return;

    // Use the same comprehensive refresh as handleSpeakersMerged
    // This ensures all speaker-related components (Edit Speakers, Merge Speakers, Analytics)
    // stay in sync - just calling loadSpeakers() wasn't triggering proper reactivity
    await handleSpeakersMerged();
  }

  // Handle analytics refresh after segment speaker change (backend refreshes analytics, frontend fetches)
  async function handleAnalyticsRefreshNeeded() {
    if (!file?.uuid) return;
    const result = await fetchFileAnalytics(file.uuid);
    if (result && file) file.analytics = result.analytics;
  }

  // Speaker renames: single, linked-profile decision, and the editor's bulk save.
  const speakerSave = createSpeakerSaveFlow({
    getFile: () => file,
    setFile: (f) => (file = f),
    getSpeakerList: () => speakerList,
    setSpeakerList: (speakers) => (speakerList = speakers),
    getBaseline: () => originalSpeakerNames,
    setBaseline: (baseline) => (originalSpeakerNames = baseline),
    loadSpeakers,
    setSavingSpeakers: (saving) => (savingSpeakers = saving),
    setProfileConfirmation: (dialog) => (speakerProfileDialog = dialog),
    closeSpeakerEditor: () => (isEditingSpeakers = false),
    isDiarizationDisabled: () => diarizationDisabled,
    refreshCaptionTrack,
    refreshAnalytics: () => handleAnalyticsRefreshNeeded(),
  });

  function handleSpeakerUpdate(event: CustomEvent<{ speakerId: string; newName: string }>) {
    speakerSave.requestSpeakerRename(event.detail.speakerId, event.detail.newName);
  }

  /** Re-fetch the player's caption track (server-built) after a committed change. */
  function refreshCaptionTrack(reason: string): void {
    tick()
      .then(() => videoPlayerComponent?.updateSubtitles?.())
      .catch(error => console.warn(`Failed to update captions after ${reason}:`, error));
  }

  function handleEditSegment(event: any) {
    const segment = event.detail.segment;
    editingSegmentId = segment.uuid;
    editingSegmentText = segment.text;
    editingSegmentOriginalText = segment.text ?? '';
  }

  async function handleSaveSegment(event: CustomEvent<{ segment: { uuid: string } }>) {
    const segment = event.detail.segment;
    if (!segment || !editingSegmentText) return;

    try {
      savingTranscript = true;
      const segmentUuid = segment.uuid;
      const saved = await saveSegmentText(fileId, segmentUuid, editingSegmentText);

      if (saved) {
        // Update the transcript store FIRST for reactivity (its readers do not read `file`)
        transcriptStore.updateSegmentText(segmentUuid, editingSegmentText);

        // Patch only `text`, so the segment's speaker fields cannot be clobbered.
        file = patchSegmentInFile(file, segmentUuid, { text: saved.text ?? editingSegmentText });

        // The write is committed — close the editor now. Everything below only affects
        // future downloads and the subtitle track, so it must not hold the UI open.
        handleCancelEditSegment();
        clearProcessedVideoCache(file.uuid);
        refreshCaptionTrack('segment edit');

        // Word counts and talk-time are derived from segment text; refresh them.
        handleAnalyticsRefreshNeeded();
      }
    } catch (error: unknown) {
      console.error('Error saving segment:', error);
      toastStore.error($t(segmentSaveErrorKey(getErrorStatus(error))));
    } finally {
      savingTranscript = false;
    }
  }

  function handleCancelEditSegment() {
    editingSegmentId = null;
    editingSegmentText = '';
    editingSegmentOriginalText = '';
  }

  function handleExportTranscript(event: CustomEvent<{ format: string }>) {
    if (!file || !file.transcript_segments) return;
    // CommentSection already holds this and reports it as it changes.
    exportDialogs.openExport(event.detail.format, commentCount > 0);
  }

  function handleSeekTo(event: any) {
    const time = event.detail.time || event.detail;
    seekToTime(time);
  }

  function seekToTime(time: number) {
    // Add 0.5 second padding before the target time for better context
    const paddedTime = Math.max(0, time - 0.5);

    // Use VideoPlayer component's seek function for all media types
    if (videoPlayerComponent && videoPlayerComponent.seekToTime) {
      videoPlayerComponent.seekToTime(paddedTime);
    } else {
      console.warn('VideoPlayer component not available for seeking');
    }
  }

  // `file.tags` is the update path: assigning a member of the reactive `file`
  // invalidates it, which re-renders TagsSection with the new tag objects.
  function handleTagsUpdated(event: CustomEvent<{ tags: Tag[] }>) {
    if (file) {
      file.tags = event.detail.tags;
    }
  }

  function handleCollectionsUpdated(event: any) {
    const { collections: updatedCollections } = event.detail;

    // Update collections array
    collections = updatedCollections;

    // Update file object if it exists
    if (file) {
      file.collections = updatedCollections;
      file = { ...file }; // Trigger reactivity
    }
  }

  function handleVideoRetry() {
    fetchFileDetails();
  }

  // Audio player event handlers for custom player
  function handleTimeUpdate(event: CustomEvent<{ currentTime: number }>) {
    currentTime = event.detail.currentTime;
    markActiveSegment(file?.transcript_segments, currentTime);
  }

  function handleWaveformSeek(event: CustomEvent) {
    const seekTime = event.detail.time;
    seekToTime(seekTime);
  }

  // Handle reprocess event from the SelectiveReprocessModal (header button). The modal has
  // already made the API call; this is the optimistic UI, and websocket notifications
  // carry the rest (no immediate refetch).
  async function handleReprocessFromModal(event: CustomEvent<{ fileId: string; stages?: string[] }>) {
    const { fileId: reprocessFileId, stages } = event.detail;

    try {
      reprocessing = true;

      // Reset notification processing state for reprocessing
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

  async function handleGenerateSummary() {
    if (!file?.uuid || !$isLLMAvailable) return;

    try {
      generatingSummary = true;
      // No page refresh: websocket notifications drive the status (and preserve edits).
      await startSummary(file.uuid);
    } catch (error: unknown) {
      console.error('Error generating summary:', error);
      toastStore.error(getErrorMessage(error, $t('fileDetail.failedToGenerateSummary')), 5000);
    } finally {
      generatingSummary = false;
    }
  }

  /** The summary modal's reprocess / regenerate-with-prompt actions. */
  async function restartSummary(options: SummarizeOptions, failureLog: string) {
    showSummaryModal = false;
    // Spinner on the button, and no summary on the file, until the websocket says otherwise.
    summaryGenerating = true;
    if (file) {
      file = { ...file, has_summary: false, summary_opensearch_id: null };
    }

    try {
      await startSummary(file.uuid, options);
    } catch (error) {
      console.error(failureLog, error);
      toastStore.error($t('fileDetail.failedToStartSummaryReprocess'), 5000);
      summaryGenerating = false;
    }
  }

  async function handleShowSummary() {
    if (!summaryData && file?.uuid) summaryData = await fetchSummaryData(file.uuid);
    if (summaryData) showSummaryModal = true;
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
      getVideoPlayerComponent: () => videoPlayerComponent,
      setFile: (f) => (file = f),
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
    stopSpeakerEvents = listenForSpeakerEvents({
      getFileId: () => fileId,
      getSpeakerList: () => speakerList,
      loadSpeakers,
      applySpeakerRename: speakerSave.applySpeakerRename,
      refetchSegmentsAfterMerge: segmentPager.refetchAfterMerge,
      refreshCaptionTrack,
    });

    if (id) {
      fileId = id;
    } else {
      console.error('FileDetail: No id parameter provided');
      const urlParams = new URLSearchParams(window.location.search);
      const pathParts = window.location.pathname.split('/');
      fileId = urlParams.get('id') || pathParts[pathParts.length - 1] || '';
    }

    if (fileId) {
      // Load file details
      fetchFileDetails(undefined, true).catch(err => {
        console.error('Error loading file details:', err);
      });

      // Load AI suggestions if available
      loadAISuggestions().catch(err => {
        console.error('Error loading AI suggestions:', err);
      });
    } else {
      pageErrorMessage = $t('fileDetail.invalidFileId');
      isLoading = false;
    }

    // Subscribe to WebSocket notifications for real-time updates
    wsUnsubscribe = websocketStore.subscribe(($ws) => handleNotifications($ws.notifications));
  });

  onDestroy(() => {
    // Cancel every deferred DOM callback FIRST; it also stops anything still in flight
    // (a late fetch resolution) from scheduling a new one after this teardown has run.
    pageTimers.destroy();
    downloadManager.cleanup();

    // Player cleanup is now handled by VideoPlayer component
    playerInitialized = false;

    mediaStream.stop();

    // Clean up WebSocket subscription
    if (wsUnsubscribe) {
      wsUnsubscribe();
    }

    stopSpeakerEvents?.();

    // Clear the transcript store when leaving the page
    transcriptStore.clear();
  });

  afterUpdate(() => {
    if (videoUrl && !playerInitialized && !isLoading && !videoElementChecked) {
      videoElementChecked = true;
      const videoElement = document.getElementById('player');
      if (videoElement) {
        // Video element found — initialize immediately. `initializePlayer` only
        // marks a flag once the element exists (already confirmed above); the
        // previous 100ms sleep just delayed the `?t=` seek that follows it for
        // no reason (#649).
        initializePlayer();
      } else {
        // Video element not found yet, will try again next update
        videoElementChecked = false;
      }
    }
  });

  // Handle ?t= timestamp seek from search results
  let hasSeenTimestamp = false;
  $: if (playerInitialized && videoPlayerComponent && !hasSeenTimestamp) {
    const urlParams = new URLSearchParams(window.location.search);
    const seekTime = urlParams.get('t');
    if (seekTime) {
      hasSeenTimestamp = true;
      const targetTime = parseFloat(seekTime);
      if (!isNaN(targetTime) && targetTime >= 0) {
        // Seek the player to the specified timestamp immediately. The 500ms
        // sleep here predated #647: `VideoPlayer.seekToTime` used to await
        // `loadedmetadata` internally, so this queued the call behind that
        // wait. It now issues the seek to the media element before metadata
        // arrives, so the sleep was dead weight stacked on top of #647's fix
        // (#649).
        if (videoPlayerComponent && typeof videoPlayerComponent.seekToTime === 'function') {
          videoPlayerComponent.seekToTime(targetTime);
        } else {
          currentTime = targetTime;
        }
        // Scroll the transcript segment into view
        scrollToSegmentAtTime(file?.transcript_segments, targetTime, scheduleTimeout);
      }
    }
  }

  // Handle ?view=summary[&section=N] — the deep link a `kind: "summary"` chat
  // citation points at (issue #464 amendment c). A summary citation labels
  // machine-generated prose about the whole recording, never a moment in it —
  // there is no timestamp to seek to, so this opens the summary modal instead
  // of the player. `section` is accepted (forward-compatible with the
  // citation's `digest_section`) but not yet used: `summary_data` has no
  // per-section structure to scroll to or highlight, unlike the transcript's
  // digest sections.
  let hasSeenSummaryView = false;
  $: if (!isLoading && file?.uuid && !hasSeenSummaryView) {
    const urlParams = new URLSearchParams(window.location.search);
    if (urlParams.get('view') === 'summary') {
      hasSeenSummaryView = true;
      handleShowSummary();
    }
  }

  // Reactive statement to re-initialize player if videoUrl changes
  $: if (videoUrl && !playerInitialized && !isLoading) {
    // Video URL available but no player, scheduling initialization
    scheduleTimeout(() => {
      if (!playerInitialized) {
        initializePlayer();
      }
    }, 200);
  }
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
        <a href="/" class="back-to-gallery-link">{$t('nav.backToGallery')}</a>
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
            {summaryGenerating}
            {generatingSummary}
            {reprocessing}
            on:viewTranscript={() => (showTranscriptModal = true)}
            on:showSummary={handleShowSummary}
            on:generateSummary={handleGenerateSummary}
            on:openReprocess={() => (showReprocessModal = true)}
          />
        </div>

        <VideoPlayer
          bind:this={videoPlayerComponent}
          {videoUrl}
          {streamContentType}
          {playbackMode}
          {file}
          errorMessage={videoErrorMessage}
          {showOriginal}
          on:retry={handleVideoRetry}
          on:timeupdate={handleTimeUpdate}
        />

        <!-- Waveform visualization -->
        {#if file && file.uuid && (file.content_type?.startsWith('audio/') || file.content_type?.startsWith('video/')) && file.status === 'completed'}
          <div class="waveform-section">
            <WaveformPlayer
              fileId={file.uuid}
              duration={file.duration_seconds || file.duration || 0}
              {currentTime}
              height={80}
              on:seek={handleWaveformSeek}
            />
          </div>
        {/if}

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
            on:speakersMerged={handleSpeakersMerged}
            on:saveSpeakerNames={speakerSave.saveAllSpeakerNames}
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
          <MetadataDisplay
            {file}
            bind:showMetadata
          />
          <ProcessingSection steps={file?.processing_steps ?? []} />
        {/if}
      </section>

      <!-- Right column: Transcript, then comments (issue #748 — comments moved here from
           the left column, below everything else in the transcript column). -->
      {#if redactionPending}
        <section class="transcript-column">
          <RedactionPendingPanel />
        </section>
      {:else if file && file.transcript_segments}
        <section class="transcript-column">
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
          on:editSegment={handleEditSegment}
          on:saveSegment={handleSaveSegment}
          on:cancelEditSegment={handleCancelEditSegment}
          on:speakerUpdate={handleSpeakerUpdate}
          on:speakerCreated={handleSpeakerCreated}
          on:speakerDeleted={handleSpeakerDeleted}
          on:analyticsRefreshNeeded={() => {
            // Fired after a committed segment-speaker change: the cues' names moved too.
            handleAnalyticsRefreshNeeded();
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

          <CommentSection
            fileId={file?.uuid ? String(file.uuid) : ''}
            {currentTime}
            on:seekTo={handleSeekTo}
            on:commentsChanged={(e) => (commentCount = e.detail.count)}
          />
        </section>
      {:else}
        <section class="transcript-column">
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

          <CommentSection
            fileId={file?.uuid ? String(file.uuid) : ''}
            {currentTime}
            on:seekTo={handleSeekTo}
            on:commentsChanged={(e) => (commentCount = e.detail.count)}
          />
        </section>
      {/if}
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
    on:updateProfile={() => speakerSave.decideProfile('update_profile')}
    on:createNewProfile={() => speakerSave.decideProfile('create_new_profile')}
    on:cancel={speakerSave.cancelProfileDecision}
  />
{/if}

<!-- Summary Modal -->
{#if file?.uuid}
  <SummaryModal
    bind:isOpen={showSummaryModal}
    fileId={file.uuid}
    fileName={file?.filename || 'Unknown File'}
    on:close={() => showSummaryModal = false}
    on:reprocessSummary={() =>
      restartSummary({ forceRegenerate: true }, 'Failed to start reprocess:')}
    on:regenerateWithPrompt={(event) =>
      restartSummary(
        { forceRegenerate: true, promptUuid: event.detail.promptUuid },
        'Failed to start regeneration with prompt:'
      )}
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
    padding: 2rem;
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
    color: var(--error-color);
    margin-bottom: 1rem;
  }

  .error-container button {
    background: var(--primary-color);
    color: white;
    border: none;
    padding: 10px 20px;
    border-radius: 6px;
    cursor: pointer;
    font-size: 14px;
    font-weight: 500;
  }

  .error-container button:hover {
    background: var(--primary-hover);
  }

  .back-to-gallery-link {
    color: var(--primary-on-surface);
    font-size: 14px;
    font-weight: 500;
    text-decoration: none;
  }

  .back-to-gallery-link:hover {
    text-decoration: underline;
  }


  .file-header {
    margin-bottom: 24px;
  }


  .main-content-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 32px;
    align-items: start;
  }

  .video-column {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }

  .video-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 0px;
    min-height: 32px;
  }

  .video-column h4 {
    margin: 0;
    font-size: 18px;
    font-weight: 600;
    color: var(--text-primary);
  }

  .transcript-column {
    display: flex;
    flex-direction: column;
    gap: 16px;
  }

  .waveform-section {
    width: 100%;
  }


  @media (max-width: 1024px) {
    .main-content-grid {
      grid-template-columns: 1fr;
      gap: 24px;
    }
  }

  @media (max-width: 768px) {
    div.file-detail-page {
      padding: 1rem;
    }

    .main-content-grid {
      gap: 20px;
    }

    .video-header {
      flex-wrap: wrap;
      gap: 0.5rem;
    }
  }

  /* Transcript segment highlighting styles */
  :global(.transcript-segment .segment-content) {
    border: 1px solid transparent;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0);
    transition: all 0.2s ease;
  }

  :global(.transcript-segment.active-segment .segment-content) {
    background-color: rgba(59, 130, 246, 0.12);
    border-color: rgba(59, 130, 246, 0.3);
    box-shadow: 0 1px 3px rgba(59, 130, 246, 0.2);
  }

  :global(.transcript-segment.active-segment .segment-content:hover) {
    background-color: rgba(59, 130, 246, 0.16);
    border-color: rgba(59, 130, 246, 0.4);
  }

  /* All Plyr styling is now handled in VideoPlayer.svelte */
</style>
