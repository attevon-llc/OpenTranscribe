<script lang="ts">
  import { onMount, onDestroy, afterUpdate, tick } from 'svelte';
  import type { TranscriptSegment } from '$lib/types/media';
  import type { Collection } from '$lib/types/collection';
  import type { Comment } from '$lib/types/comment';
  import type { Tag } from '$lib/types/tag';
  import { lockScroll, unlockScroll } from '$lib/scrollLock';
  import { guardUnsavedChanges } from '$lib/navigation/unsavedChangesGuard';
  import axiosInstance from '$lib/axios';
  import { formatDuration } from '$lib/utils/formatting';
  import { websocketStore } from '$stores/websocket';
  import { handleFileNotification } from '$lib/fileDetail/notificationHandler';
  import { createPageTimers } from '$lib/fileDetail/pageTimers';
  import { createSpeakerLoader } from '$lib/fileDetail/speakerLoader';
  import {
    hasUnsavedSpeakerNames,
    speakerHasUnsavedChange,
    validateSpeakerName,
    type SpeakerItem,
    type SpeakerNameBaseline,
  } from '$lib/fileDetail/speakerNames';
  import { markActiveSegment, scrollToAndHighlight, scrollToSegmentAtTime } from '$lib/fileDetail/segmentDom';
  import {
    appendSegmentPage,
    patchSegmentInFile,
    renameSpeakersInFile,
    MAX_SEGMENT_PAGE_SIZE
  } from '$lib/fileDetail/segmentSync';

  // Import new components
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
  import { authStore } from '$stores/auth';
  import { transcriptStore } from '$stores/transcriptStore';
  import { downloadStore } from '$stores/downloads';
  import { supportSessionGate } from '$stores/supportSession';
  import { apiCache, cacheKey, CacheTTL } from '$lib/apiCache';
  import { getAISuggestions, type TagSuggestion, type CollectionSuggestion } from '$lib/api/suggestions';
  import { requestSummary, topicExtractionNoticeKey, type SummarizeResponse } from '$lib/api/summarize';
  import { getAppBaseUrl } from '$lib/utils/url';
  import { getMediaStreamUrl, getCachedUrlInfo, createUrlRefresher, clearMediaUrlCache, type PlaybackMode } from '$lib/api/mediaUrl';
  import Spinner from '../../../components/ui/Spinner.svelte';
  import FileDetailSkeleton from '../../../components/FileDetailSkeleton.svelte';

  // No need for a global commentsForExport variable - we'll fetch when needed

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
  let apiBaseUrl = '';
  let videoPlayerComponent: any = null;
  let currentTime = 0;
  let duration = 0;
  let isLoading = true;
  let isPlayerBuffering = false;
  let loadProgress = 0;
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
  let speakerNamesChanged = false; // Track if any speaker names have been modified
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

  // Speaker profile confirmation modal state
  let showSpeakerProfileConfirmation = false;

  // Scroll lock for the speaker-profile dialog's custom .modal-overlay
  let _prevSpeakerConfirm = false;
  $: {
    if (showSpeakerProfileConfirmation !== _prevSpeakerConfirm) {
      showSpeakerProfileConfirmation ? lockScroll() : unlockScroll();
      _prevSpeakerConfirm = showSpeakerProfileConfirmation;
    }
  }
  type PendingSpeakerUpdate = {
    speakerId: number | string;
    newName: string;
    speaker: SpeakerItem;
  };
  let pendingSpeakerUpdate: PendingSpeakerUpdate | null = null;
  let profileUpdateMessage = '';
  let profileUpdateTitle = '';

  // Bulk speaker save confirmation state
  let speakerConfirmationQueue: SpeakerItem[] = [];
  let currentConfirmationIndex = 0;
  let bulkSaveInProgress = false;
  let bulkSaveDecisions = new Map();


  /**
   * Fetches file details from the API
   */
  /**
   * Fetch transcript and related data to update the page without overwriting file state
   */
  async function fetchTranscriptData(): Promise<void> {
    if (!fileId) {
      console.error('FileDetail: No file ID provided to fetchTranscriptData');
      return;
    }

    try {
      const response = await axiosInstance.get(`/files/${fileId}`);

      if (response.data && typeof response.data === 'object' && file) {
        // Update all transcript and processing-related fields while preserving UI state flags.
        // Both segment representations must be replaced together — the transcript renders
        // from `grouped_segments`, so a stale copy keeps showing the old transcript (#352).
        file.transcript_segments = response.data.transcript_segments || [];
        file.grouped_segments = response.data.grouped_segments || [];
        file.speakers = response.data.speakers || [];
        file.waveform_data = response.data.waveform_data;
        file.duration = response.data.duration;
        file.duration_seconds = response.data.duration_seconds;

        // Update metadata and processing info
        file.processed_at = response.data.processed_at;
        file.analytics = response.data.analytics;

        // Persist ASR provider/model so MetadataDisplay can show them after
        // transcription completes without requiring a full page refresh.
        if (response.data.asr_provider !== undefined) {
          file.asr_provider = response.data.asr_provider;
        }
        if (response.data.asr_model !== undefined) {
          file.asr_model = response.data.asr_model;
        }
        if (response.data.language !== undefined) {
          file.language = response.data.language;
        }
        if (response.data.diarization_disabled !== undefined) {
          file.diarization_disabled = response.data.diarization_disabled;
        }

        // Update collections if they changed
        collections = response.data.collections || [];

        // Update file object
        file = { ...file };

        // Process the new transcript data
        processTranscriptData();

        // Analytics are pre-computed by the backend and included in the API response

      }
    } catch (error) {
      console.error('Error fetching transcript data:', error);
    }
  }

  /**
   * @param fromPrefetch First load of the page only. Joins (or consumes) the gallery's hover
   *   prefetch of `GET /files/{id}` instead of issuing a second identical request; the entry
   *   is dropped once read so a later reload (reprocess, retry) always hits the server.
   */
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

      let response: { data: any };
      if (fromPrefetch && !showOriginal) {
        const key = cacheKey.fileDetail(targetFileId);
        response = {
          data: await apiCache.getOrFetch(
            key,
            () => axiosInstance.get(`/files/${targetFileId}`).then((r) => r.data),
            CacheTTL.FILES
          ),
        };
        apiCache.invalidate(key);
      } else {
        response = await axiosInstance.get(`/files/${targetFileId}`, {
          params: showOriginal ? { redact: false } : {},
        });
      }

      if (response.data && typeof response.data === 'object') {
        file = response.data;
        collections = response.data.collections || [];
        myPermission = response.data.my_permission ?? null;
        // Content-redaction state: pending (transcript withheld) + whether masking applied.
        redactionPending = response.data.redaction_pending || false;
        redactionStatus = response.data.redaction_status || '';
        redactionEnabled = response.data.redaction_enabled === true;
        if (!showOriginal && Array.isArray(response.data.transcript_segments)) {
          if (response.data.transcript_segments.some((s: TranscriptSegment) => s?.redactions?.length)) {
            redactionActive = true;
          }
        }

        // Track pagination metadata
        totalSegments = response.data.total_segments || 0;
        totalSpeakerSegments = response.data.total_speaker_segments || 0;
        segmentLimit = response.data.segment_limit || 500;
        segmentOffset = response.data.segment_offset || 0;

        // Set up video URL only if file might have media available
        if (file.status !== 'error' && file.status !== 'cancelled') {
          setupVideoUrl(targetFileId, fromPrefetch);
        }

        // Process transcript data from the file response
        processTranscriptData();

        // Analytics are pre-computed by the backend and included in the API response

        isLoading = false;
      } else {
        throw new Error('Invalid response format');
      }
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
        params: showOriginal ? { redact: false } : {},
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
        redactionPending = response.data.redaction_pending || false;
        redactionStatus = response.data.redaction_status || '';
        redactionEnabled = response.data.redaction_enabled === true;
        if (!showOriginal && Array.isArray(response.data.transcript_segments)) {
          if (response.data.transcript_segments.some((s: TranscriptSegment) => s?.redactions?.length)) {
            redactionActive = true;
          }
        }
        processTranscriptData();
      }
    } catch (error) {
      console.error('Error refreshing transcript:', error);
      toastStore.error($t('fileDetail.failedToLoadFile'));
    }
  }

  /**
   * Trigger (or re-run) content-redaction detection for THIS file. Useful for old files
   * processed before redaction was enabled, or to re-scan after changing settings.
   */
  async function triggerRedaction(): Promise<void> {
    if (!file?.uuid) return;
    try {
      const response = await axiosInstance.post('/files/management/bulk-action', {
        file_uuids: [file.uuid],
        action: 'redact'
      });
      const result = (response.data || [])[0];
      if (result && result.success) {
        redactionPending = true;
        redactionStatus = 'pending';
        toastStore.success($t('settings.contentRedaction.redactStarted'));
      } else {
        toastStore.error(result?.message || $t('settings.contentRedaction.redactTriggerFailed'));
      }
    } catch (err) {
      console.error('Trigger redaction error:', err);
      // The 409 "redaction is not enabled" carries the reason; show it, not a generic failure.
      const detail = (err as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
      toastStore.error(
        typeof detail === 'string' ? detail : $t('settings.contentRedaction.redactTriggerFailed')
      );
    }
  }

  /**
   * Load more transcript segments for large transcripts
   */
  async function loadMoreSegments(): Promise<void> {
    if (!fileId || loadingMoreSegments || !hasMoreSegments) return;

    try {
      loadingMoreSegments = true;
      const currentCount = file?.transcript_segments?.length || 0;
      const nextOffset = currentCount;

      const response = await axiosInstance.get(`/files/${fileId}/segments`, {
        params: {
          segment_limit: segmentLimit,
          segment_offset: nextOffset,
          ...(showOriginal ? { redact: false } : {})
        }
      });

      if (response.data && response.data.transcript_segments) {
        // Both representations advance together — the transcript renders from the
        // grouping, so appending segments alone loads rows that never display (#352).
        file = appendSegmentPage(file, response.data);

        // Update pagination state
        totalSegments = response.data.total_segments || totalSegments;

        // Update transcript store so TranscriptModal gets the new segments
        if (file?.uuid && file.transcript_segments && speakerList) {
          transcriptStore.loadTranscriptData(file.uuid, file.transcript_segments, speakerList);
        }
      }
    } catch (error) {
      console.error('Error loading more segments:', error);
      toastStore.error($t('fileDetail.failedToLoadMoreSegments'));
    } finally {
      loadingMoreSegments = false;
    }
  }

  /**
   * Load segments up to a target index for jump-to-timestamp navigation.
   * Makes a single API call to fetch all segments from current offset to target + buffer.
   */
  async function handleLoadUpTo(event: any): Promise<void> {
    const { targetIndex, segmentUuid, startTime } = event.detail;
    if (!fileId || loadingMoreSegments) return;

    const currentCount = file?.transcript_segments?.length || 0;
    if (targetIndex < currentCount) {
      // Already loaded - just scroll and highlight
      scrollToAndHighlight(segmentUuid, scheduleTimeout);
      return;
    }

    try {
      loadingMoreSegments = true;
      const buffer = 50;

      // Page in a loop: the endpoint caps a single page, so a jump to the end of a long
      // transcript can't be satisfied by one request. The guard bounds the loop if the
      // server stops making progress.
      for (let page = 0; page < 50; page++) {
        const loaded = file?.transcript_segments?.length || 0;
        if (loaded > targetIndex) break;

        const response = await axiosInstance.get(`/files/${fileId}/segments`, {
          params: {
            segment_limit: Math.min(targetIndex - loaded + buffer + 1, MAX_SEGMENT_PAGE_SIZE),
            segment_offset: loaded,
            ...(showOriginal ? { redact: false } : {})
          }
        });

        const fetched = response.data?.transcript_segments;
        if (!Array.isArray(fetched) || fetched.length === 0) break;

        file = appendSegmentPage(file, response.data);
        totalSegments = response.data.total_segments || totalSegments;

        if ((file?.transcript_segments?.length || 0) >= totalSegments) break;
      }

      if (file?.uuid && file.transcript_segments && speakerList) {
        transcriptStore.loadTranscriptData(file.uuid, file.transcript_segments, speakerList);
      }

      // Wait for DOM update then scroll to target segment
      scheduleTimeout(() => scrollToAndHighlight(segmentUuid, scheduleTimeout), 300);
    } catch (error) {
      console.error('Error loading segments up to target:', error);
      toastStore.error($t('fileDetail.failedToLoadMoreSegments'));
    } finally {
      loadingMoreSegments = false;
    }
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

  /**
   * Process transcript data from the main file response
   */
  function processTranscriptData() {
    // Use transcript_segments from backend (already sorted by backend)
    let transcriptData = file?.transcript_segments;

    if (!file || !transcriptData || !Array.isArray(transcriptData)) {
      return;
    }

    try {
      // Backend now provides pre-sorted transcript segments - no client-side sorting needed

      // Update the file with sorted data
      file.transcript_segments = transcriptData;

      // Load speakers and update store after they're loaded
      loadSpeakers();
    } catch (error) {
      console.error('Error processing transcript:', error);
    }
  }

  // URL refresher for long video playback
  let urlRefresher: { stop: () => void } | null = null;

  /**
   * Set up the video URL for streaming using secure presigned URLs.
   *
   * This follows AWS/GCS best practices:
   * - Short-lived presigned URLs (5 minutes default)
   * - Automatic refresh before expiration for long playback
   * - Cryptographically signed by MinIO
   */
  async function setupVideoUrl(fileId: string, keepCached = false) {
    try {
      // Stop any existing URL refresher
      if (urlRefresher) {
        urlRefresher.stop();
        urlRefresher = null;
      }

      // Clear cached URL to ensure fresh presigned URL, except on the first load, where the
      // only entry can be the gallery's hover prefetch (seconds old, and getMediaStreamUrl
      // already refuses one near expiry); clearing it made a second identical request.
      if (!keepCached) clearMediaUrlCache(fileId);

      // Get presigned URL from backend (authenticated, time-limited)
      const url = await getMediaStreamUrl(fileId, 'video');

      // What the URL serves can differ from the upload: a converted copy of audio no
      // browser decodes, or only the audio track of a video no browser shows. Set before
      // videoUrl so the player picks <audio> vs <video> from the right type.
      const info = getCachedUrlInfo(fileId, 'video');
      streamContentType = info?.contentType ?? '';
      playbackMode = info?.playback ?? null;
      videoUrl = url;

      // Set up automatic URL refresh for long videos, using the URL's real expiry
      // (MEDIA_URL_EXPIRE_SECONDS) rather than a hardcoded interval — avoids needlessly
      // re-fetching and re-setting the video src mid-playback.
      const expiresIn = info ? Math.max(60, Math.floor((info.expiresAt - Date.now()) / 1000)) : 300;
      urlRefresher = createUrlRefresher(
        fileId,
        (newUrl) => {
          videoUrl = newUrl;
        },
        expiresIn
      );

      // Reset video element check flag to prompt afterUpdate to try initialization
      videoElementChecked = false;
    } catch (error) {
      console.error('Failed to get video URL:', error);
      videoUrl = '';
      videoErrorMessage = 'Video not available for this file';
    }
  }





  /**
   * Refresh subtitle track when transcript changes (debounced)
   */

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
        params: showOriginal ? { redact: false } : {}
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

    try {
      // Fetch only analytics (lightweight, no full file detail)
      const response = await axiosInstance.get(`/files/${file.uuid}/analytics`);

      if (response.data && typeof response.data === 'object') {
        // Update analytics from refreshed response
        file.analytics = response.data.analytics;
      }
    } catch (error) {
      console.error('Error refreshing analytics after speaker change:', error);
      // Don't show error toast - analytics refresh is not critical to user workflow
    }
  }

  // Handle speaker name updates
  async function handleSpeakerUpdate(event: CustomEvent) {
    const { speakerId, newName } = event.detail;

    // Validate the speaker name
    const validation = validateSpeakerName(newName, $t);
    if (!validation.isValid) {
      toastStore.error(validation.error ?? $t('speakerValidation.nameRequired'));
      return;
    }

    // Find the speaker to check if they have a profile
    const speaker = speakerList.find(s => s.uuid === speakerId);

    // Check if this speaker has a profile and the name is changing
    if (speaker && speaker.profile && speaker.profile.name !== newName) {
      // Show confirmation modal for profile update decision
      pendingSpeakerUpdate = { speakerId, newName, speaker };
      profileUpdateTitle = $t('speakerProfile.updateTitle');
      profileUpdateMessage = $t('speakerProfile.linkedMessage', {
        speakerName: speaker.display_name || speaker.name,
        profileName: speaker.profile.name
      });
      showSpeakerProfileConfirmation = true;
      return;
    }

    // If no profile or name is the same, proceed with normal update
    await performSpeakerUpdate(speakerId, newName, 'normal');
  }

  // Handle speaker profile confirmation decision
  async function handleProfileConfirmation(decision: 'update_profile' | 'create_new_profile') {
    if (bulkSaveInProgress) {
      // Handle bulk save confirmation
      await handleBulkConfirmation(decision);
    } else if (pendingSpeakerUpdate) {
      // Handle individual speaker confirmation
      const { speakerId, newName } = pendingSpeakerUpdate;

      // Close BEFORE awaiting, as the bulk path already does. The optimistic rename runs
      // synchronously inside performSpeakerUpdate, so the dialog closes and the transcript
      // repaints in the same frame instead of the dialog sitting open through the request.
      showSpeakerProfileConfirmation = false;
      pendingSpeakerUpdate = null;
      profileUpdateMessage = '';
      profileUpdateTitle = '';

      await performSpeakerUpdate(speakerId, newName, decision);
    }
  }

  // Handle modal cancellation
  function handleProfileConfirmationCancel() {
    showSpeakerProfileConfirmation = false;
    pendingSpeakerUpdate = null;
    profileUpdateMessage = '';
    profileUpdateTitle = '';

    // Reset bulk save state if it was in progress
    if (bulkSaveInProgress) {
      bulkSaveInProgress = false;
      speakerConfirmationQueue = [];
      currentConfirmationIndex = 0;
      bulkSaveDecisions.clear();
      savingSpeakers = false;
    }
  }

  // Handle bulk save confirmation
  async function handleBulkConfirmation(decision: 'update_profile' | 'create_new_profile') {
    if (!pendingSpeakerUpdate || speakerConfirmationQueue.length === 0) return;

    const { speakerId, newName } = pendingSpeakerUpdate;

    // Store the decision for this speaker
    bulkSaveDecisions.set(speakerId, { decision, newName });

    // Move to next confirmation or finish
    currentConfirmationIndex++;

    if (currentConfirmationIndex < speakerConfirmationQueue.length) {
      // Show next confirmation
      showNextConfirmation();
    } else {
      // All confirmations done, proceed with bulk save
      showSpeakerProfileConfirmation = false;
      await performBulkSaveWithDecisions();
    }
  }

  // Show next confirmation in the queue
  function showNextConfirmation() {
    if (currentConfirmationIndex < speakerConfirmationQueue.length) {
      const speaker = speakerConfirmationQueue[currentConfirmationIndex];
      pendingSpeakerUpdate = {
        speakerId: speaker.uuid,
        newName: speaker.display_name ?? speaker.name,
        speaker
      };
      profileUpdateTitle = $t('fileDetail.updateSpeakerProfileCounter', { current: currentConfirmationIndex + 1, total: speakerConfirmationQueue.length });
      profileUpdateMessage = $t('fileDetail.profileLinkedMessage', { displayName: speaker.display_name || speaker.name, profileName: speaker.profile?.name ?? '' });
      showSpeakerProfileConfirmation = true;
    }
  }

  // Perform the actual speaker update with the specified action
  /**
   * Apply a speaker display-name change everywhere the page renders it.
   *
   * Used for the optimistic write, for rollback, and for the websocket backstop, so all
   * three stay in step. `speakerLabel` is the original `SPEAKER_XX` (`speaker.name`),
   * which is both a match fallback and the value speaker colours hash — it is never
   * rewritten, only read.
   */
  function applySpeakerRename(speakerUuid: string, speakerLabel: string | undefined, newName: string) {
    speakerList = speakerList.map(speaker =>
      speaker.uuid === speakerUuid ? { ...speaker, display_name: newName } : speaker
    );
    // Feeds TranscriptModal, which reads the store rather than `file`.
    transcriptStore.updateSpeakerName(speakerUuid, newName);
    file = renameSpeakersInFile(file, [
      { uuid: speakerUuid, label: speakerLabel, displayName: newName }
    ]);
    // The caption track is NOT refreshed here: it comes from the server, and this runs
    // before the rename is saved. Callers refresh it once the server has the new name.
  }

  /** Re-fetch the player's caption track (server-built) after a committed change. */
  function refreshCaptionTrack(reason: string): void {
    tick()
      .then(() => videoPlayerComponent?.updateSubtitles?.())
      .catch(error => console.warn(`Failed to update captions after ${reason}:`, error));
  }

  // Perform the actual speaker update with the specified action
  async function performSpeakerUpdate(speakerId: number | string, newName: string, action: 'normal' | 'update_profile' | 'create_new_profile') {
    const speakerUuid = String(speakerId);
    const speaker = speakerList.find(s => s.uuid === speakerId);
    if (!speaker?.uuid) return;

    // Captured before the optimistic write so a failed save can be undone.
    const previousName = speaker.display_name || '';
    const speakerLabel = speaker.name;

    applySpeakerRename(speakerUuid, speakerLabel, newName);

    // Persist to database with the action decision
    try {
      const payload: any = {
        display_name: newName
        // NEVER update 'name' field - it contains the original speaker ID for color consistency
      };

      // Add profile action if needed
      if (action !== 'normal') {
        payload.profile_action = action;
      }

      await axiosInstance.put(`/speakers/${speaker.uuid}`, payload);

      // Postgres has committed — confirm now rather than after the refresh below.
      const successMessage = action === 'update_profile'
        ? $t('speakerProfile.updatedGlobally', { name: newName })
        : action === 'create_new_profile'
        ? $t('speakerProfile.newCreated', { name: newName })
        : $t('speakerProfile.renamed', { name: newName });

      toastStore.success(successMessage);
      refreshCaptionTrack('speaker rename');

      // Move the change-detection baseline with it, or the unsaved-changes indicator
      // lights up for a name that just saved.
      originalSpeakerNames = new Map(originalSpeakerNames).set(speakerUuid, newName.trim());

      // Speaker labels appear in talk-time and turn-taking analytics.
      handleAnalyticsRefreshNeeded();

      // Refresh speakers to get updated profile data from GET endpoint
      // This ensures speaker.profile.name is current before the next edit
      await loadSpeakers();
    } catch (error: unknown) {
      console.error('Failed to update speaker name in database:', error);

      // Show user-friendly error with option to retry
      const status = getErrorStatus(error);
      const errorMessage = status === 404
        ? $t('speakerProfile.notFound')
        : status === 403
        ? $t('speakerProfile.permissionDenied')
        : $t('speakerProfile.saveFailed');

      // Undo the optimistic write. Leaving it would render the new name across the
      // transcript, subtitles, modal and exports as though it had saved.
      applySpeakerRename(speakerUuid, speakerLabel, previousName);
      toastStore.error($t('speakerProfile.saveFailedReverted', { error: errorMessage }));
    }
  }

  function handleEditSegment(event: any) {
    const segment = event.detail.segment;
    editingSegmentId = segment.uuid;
    editingSegmentText = segment.text;
    editingSegmentOriginalText = segment.text ?? '';
  }


  async function handleSaveSegment(event: any) {
    const segment = event.detail.segment;
    if (!segment || !editingSegmentText) return;

    try {
      savingTranscript = true;

      // Call backend API to update the specific segment
      const segmentUpdate = {
        text: editingSegmentText
      };

      const segmentUuid = segment.uuid;
      const response = await axiosInstance.put(`/files/${fileId}/transcript/segments/${segmentUuid}`, segmentUpdate);

      if (response.data) {
        // Update the transcript store FIRST for reactivity (feeds TranscriptModal)
        transcriptStore.updateSegmentText(segmentUuid, editingSegmentText);

        // Patch only `text`. The old code merged the whole response and then restored the
        // speaker fields it had just clobbered; a targeted patch can't clobber them.
        file = patchSegmentInFile(file, segmentUuid, { text: response.data.text ?? editingSegmentText });

        // The write is committed — close the editor now. Everything below only affects
        // future downloads and the subtitle track, so it must not hold the UI open.
        editingSegmentId = null;
        editingSegmentText = '';
        editingSegmentOriginalText = '';

        // Clear cached processed videos so downloads use the updated transcript.
        axiosInstance.delete(`/files/${file.uuid}/cache`).catch((error: unknown) => {
          console.warn('Could not clear video cache:', error);
        });

        refreshCaptionTrack('segment edit');

        // Word counts and talk-time are derived from segment text; refresh them.
        handleAnalyticsRefreshNeeded();
      }
    } catch (error: unknown) {
      console.error('Error saving segment:', error);

      // Show error as toast notification for consistency
      const status = getErrorStatus(error);
      if (status === 405) {
        toastStore.error($t('fileDetail.transcriptEditingNotSupported'));
      } else if (status === 404) {
        toastStore.error($t('fileDetail.transcriptSegmentNotFound'));
      } else if (status === 422) {
        toastStore.error($t('fileDetail.invalidSegmentData'));
      } else {
        toastStore.error($t('fileDetail.failedToSaveSegment'));
      }
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

  async function handleSaveSpeakerNames() {
    if (!speakerList || speakerList.length === 0) return;

    savingSpeakers = true;

    try {
      // Speakers whose display name actually changed since the last load/save —
      // including a change to empty (see speakerHasUnsavedChange). The old filter
      // dropped both empty and SPEAKER_-prefixed CURRENT values regardless of
      // whether they had changed, so "clear a label" issued no PUT at all.
      const speakersToUpdate = speakerList.filter((speaker) => speakerHasUnsavedChange(speaker, originalSpeakerNames));

      if (speakersToUpdate.length === 0) {
        savingSpeakers = false;
        return;
      }

      // Validate non-empty names only — an empty name is a deliberate clear
      // (see speakerHasUnsavedChange), not a validation failure.
      for (const speaker of speakersToUpdate) {
        const trimmed = (speaker.display_name ?? '').trim();
        if (trimmed === '') continue;
        const validation = validateSpeakerName(trimmed, $t);
        if (!validation.isValid) {
          toastStore.error(`${speaker.name}: ${validation.error}`);
          savingSpeakers = false;
          return;
        }
      }

      // Check for speakers that need profile confirmation (skip when diarization
      // disabled, and skip a clear — there is no new name to reconcile against
      // the linked profile).
      const speakersNeedingConfirmation = diarizationDisabled ? [] : speakersToUpdate.filter(speaker =>
        speaker.profile &&
        (speaker.display_name ?? '').trim() !== '' &&
        speaker.profile.name !== (speaker.display_name ?? '').trim()
      );

      if (speakersNeedingConfirmation.length > 0) {
        // Start bulk confirmation process
        speakerConfirmationQueue = speakersNeedingConfirmation;
        currentConfirmationIndex = 0;
        bulkSaveInProgress = true;
        bulkSaveDecisions.clear();

        // Start with first confirmation
        showNextConfirmation();
        return;
      }

      // No confirmations needed, proceed with regular save
      await performBulkSave(speakersToUpdate);

    } catch (error) {
      console.error('Error saving speaker names:', error);
      toastStore.error($t('speakerProfile.saveAllFailed'));
      savingSpeakers = false;
    }
  }

  // Perform bulk save with confirmation decisions
  async function performBulkSaveWithDecisions() {
    try {
      const speakersToUpdate = speakerList.filter((speaker) => speakerHasUnsavedChange(speaker, originalSpeakerNames));

      await performBulkSave(speakersToUpdate, bulkSaveDecisions);

      // Reset bulk save state
      bulkSaveInProgress = false;
      speakerConfirmationQueue = [];
      currentConfirmationIndex = 0;
      bulkSaveDecisions.clear();

    } catch (error) {
      console.error('Error in bulk save with decisions:', error);
      toastStore.error($t('speakerProfile.saveAllFailed'));
      savingSpeakers = false;

      // Reset bulk save state
      bulkSaveInProgress = false;
      speakerConfirmationQueue = [];
      currentConfirmationIndex = 0;
      bulkSaveDecisions.clear();
    }
  }

  // Perform the actual bulk save operation
  async function performBulkSave(speakersToUpdate: SpeakerItem[], decisions = new Map()) {
    // STEP 1: Optimistic UI updates - immediately update voice suggestions with new names
    const nameChanges = new Map(); // Track profile name changes for voice suggestions

    speakersToUpdate.forEach((speaker: SpeakerItem) => {
      const decision = decisions.get(speaker.uuid);
      const newName = (speaker.display_name ?? '').trim();

      // If updating a profile globally, track the name change
      if (decision && decision.decision === 'update_profile' && speaker.profile) {
        nameChanges.set(speaker.profile.uuid, { oldName: speaker.profile.name, newName });
      }
    });

    // Optimistically update profile suggestions for all speakers
    if (nameChanges.size > 0) {
      speakerList = speakerList.map(s => {
        if (s.profile_suggestions && s.profile_suggestions.length > 0) {
          s.profile_suggestions = s.profile_suggestions.map((suggestion: any) => {
            for (const [profileId, change] of nameChanges) {
              if (suggestion.name === change.oldName && suggestion.suggestion_type === 'profile') {
                return { ...suggestion, name: change.newName };
              }
            }
            return suggestion;
          });
        }
        return s;
      });
    }

    // STEP 2: Update speakers in the backend with decisions
    // Backend returns immediately after saving to PostgreSQL - heavy processing happens in background
    // `allSettled`, not `all`: with `all` a single rejection discarded the outcome of every
    // other speaker, so the page could neither confirm the ones that saved nor undo the
    // one that didn't.
    const results = await Promise.allSettled(
      speakersToUpdate.map((speaker: SpeakerItem) => {
        const decision = decisions.get(speaker.uuid);
        const payload: any = {
          display_name: (speaker.display_name ?? '').trim(),
          name: speaker.name
        };

        // Add profile action if there's a decision for this speaker
        if (decision) {
          payload.profile_action = decision.decision;
        }

        return axiosInstance.put(`/speakers/${speaker.uuid}`, payload);
      })
    );

    const failed = speakersToUpdate.filter((_, i) => results[i].status === 'rejected');
    const failedUuids = new Set(failed.map(speaker => speaker.uuid));
    results.forEach((result, i) => {
      if (result.status === 'rejected') {
        console.error(`Failed to save speaker ${speakersToUpdate[i].uuid}:`, result.reason);
      }
    });

    // Restore the names that did not save, so nothing renders as though it persisted.
    if (failed.length > 0) {
      speakerList = speakerList.map(speaker =>
        failedUuids.has(speaker.uuid)
          ? { ...speaker, display_name: originalSpeakerNames.get(speaker.uuid) ?? speaker.display_name }
          : speaker
      );
    }

    // STEP 4: PostgreSQL updates complete - stop save button spinner immediately!
    savingSpeakers = false;
    isEditingSpeakers = false;
    if (failed.length > 0) {
      toastStore.error($t('speakerProfile.bulkSavePartialFailure', { count: failed.length }));
    } else if (speakersToUpdate.length > 0) {
      // Only announce success when something was actually attempted and persisted.
      // With an empty `speakersToUpdate` (nothing changed) this used to still fire.
      toastStore.success($t('speakerProfile.savedSuccess'));
    }

    // Reset original names to current values (no changes after save)
    originalSpeakerNames = new Map(
      speakerList.map(speaker => [speaker.uuid, (speaker.display_name || '').trim()])
    );

    // STEP 5: Apply the saved names everywhere the page renders them (instant).
    // Every speaker in `speakersToUpdate` that did NOT fail was just persisted —
    // including a clear (an empty `display_name`). `renameSpeakersInFile` /
    // `transcriptStore.updateSpeakerName` write whatever string they're given
    // VERBATIM into `resolved_speaker_name`, and an empty one renders downstream as
    // "Unknown Speaker" rather than reverting to the raw label — so a cleared
    // speaker's raw `speaker.name` (SPEAKER_XX) is what gets applied here, never ''.
    const renames = speakersToUpdate
      .filter((speaker: SpeakerItem) => speaker.uuid && !failedUuids.has(speaker.uuid))
      .map((speaker: SpeakerItem) => {
        const trimmed = (speaker.display_name ?? '').trim();
        return {
          uuid: speaker.uuid,
          label: speaker.name,
          displayName: trimmed === '' ? (speaker.name ?? '') : trimmed
        };
      });

    renames.forEach(rename => transcriptStore.updateSpeakerName(rename.uuid, rename.displayName));
    file = renameSpeakersInFile(file, renames);

    // Update captions and clear cache (async, don't block)
    refreshCaptionTrack('saving speaker names');

    axiosInstance.delete(`/files/${file.uuid}/cache`).catch(error => {
      console.warn('Could not clear video cache:', error);
    });
  }

  function handleSeekTo(event: any) {
    const time = event.detail.time || event.detail;
    seekToTime(time);
  }


  // Speaker verification event handlers

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
  function handleTimeUpdate(event: CustomEvent) {
    currentTime = event.detail.currentTime;
    duration = event.detail.duration;
    markActiveSegment(file?.transcript_segments, currentTime);
  }

  function handlePlay(_event: CustomEvent) {
    // Handle play event if needed
  }

  function handlePause(_event: CustomEvent) {
    // Handle pause event if needed
  }

  function handleLoadedMetadata(event: CustomEvent) {
    duration = event.detail.duration;
  }

  function handleWaveformSeek(event: CustomEvent) {
    const seekTime = event.detail.time;
    seekToTime(seekTime);
  }

  async function handleReprocess(event: CustomEvent) {
    const { fileId: reprocessFileId, stages } = event.detail;

    try {
      reprocessing = true;

      // Reset notification processing state for reprocessing
      lastProcessedNotificationState = '';

      // Only set processing state for destructive stages that change file status
      const isDestructive = !stages || stages.includes('transcription') || stages.includes('rediarize');

      if (file) {
        if (isDestructive) {
          file.status = 'processing';
          file.progress = 0;
        }

        // Only clear transcript data if full transcription is being rerun
        if (!stages || stages.includes('transcription')) {
          file.transcript_segments = [];
          file.has_summary = false;
          file.summary_opensearch_id = null;
        }

        // Set summary generating state if summarization is in the stages
        if (stages && stages.includes('summarization') && llmAvailable) {
          summaryGenerating = true;
          generatingSummary = true;
        }

        file = file; // Trigger reactivity
      }

      // API call is already made by the SelectiveReprocessModal
      // Just handle the optimistic UI update here

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

  // Handle reprocess event from the SelectiveReprocessModal (header button)
  async function handleReprocessFromModal(event: CustomEvent) {
    const { fileId: reprocessFileId, stages } = event.detail;

    try {
      reprocessing = true;

      // Reset notification processing state for reprocessing
      lastProcessedNotificationState = '';

      // Only set processing state for destructive stages that change file status
      const isDestructive = stages?.includes('transcription') || stages?.includes('rediarize');

      if (file) {
        if (isDestructive) {
          file.status = 'processing';
          file.progress = 0;
        }

        // Only clear transcript data if full transcription is being rerun
        if (stages && stages.includes('transcription')) {
          file.transcript_segments = [];
          file.has_summary = false;
          file.summary_opensearch_id = null;
        }

        // Set summary generating state if summarization is in the stages
        if (stages && stages.includes('summarization') && llmAvailable) {
          summaryGenerating = true;
          generatingSummary = true;
        }

        file = file; // Trigger reactivity
      }

      // Don't immediately fetch - let WebSocket notifications handle updates

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


  /** Say what happened to tag/collection suggestions queued alongside a summary, if anything. */
  function announceTopicExtraction(res: SummarizeResponse): void {
    const key = topicExtractionNoticeKey(res.topic_extraction);
    if (key) toastStore.info($t(key), 6000);
  }

  /**
   * Generate summary for the transcript
   */
  async function handleGenerateSummary() {
    if (!file?.uuid) return;

    // Check if LLM is available
    if (!$isLLMAvailable) {
      return;
    }

    try {
      generatingSummary = true;

      announceTopicExtraction(await requestSummary(file.uuid));

      // Don't refresh page - let WebSocket notifications handle status updates
      // This preserves user's editing state

      // The WebSocket will update summaryGenerating = true when processing starts
    } catch (error: unknown) {
      console.error('Error generating summary:', error);
      const errorMessage = getErrorMessage(error, $t('fileDetail.failedToGenerateSummary'));

      toastStore.error(errorMessage, 5000);
    } finally {
      generatingSummary = false;
    }
  }

  /**
   * Load summary data from the backend
   */
  async function loadSummary() {
    if (!file?.uuid) return;

    try {
      const response = await axiosInstance.get(`/files/${file.uuid}/summary`);
      summaryData = response.data.summary_data;
    } catch (error: unknown) {
      console.error('Error loading summary:', error);
      if (getErrorStatus(error) !== 404) {
        toastStore.error($t('fileDetail.failedToLoadSummary'), 5000);
      }
    }
  }

  /**
   * Show the summary modal
   */
  function handleShowSummary() {
    if (summaryData) {
      showSummaryModal = true;
    } else {
      loadSummary().then(() => {
        if (summaryData) {
          showSummaryModal = true;
        }
      });
    }
  }

  // WebSocket subscription for real-time updates
  let wsUnsubscribe: () => void;

  // Component mount logic
  // Handler for speaker-updated CustomEvent (dispatched by websocket.ts, never enters store)
  function handleSpeakerUpdatedEvent(e: Event) {
    const detail = (e as CustomEvent).detail;
    // Publishers disagree on the field name: speaker_attribute_task sends `file_id`,
    // the speaker endpoints send `media_file_id`. Accept both.
    const eventFileId = detail?.file_id ?? detail?.media_file_id;
    // Only refresh if this event is for the current file
    if (eventFileId && String(eventFileId) === String(fileId)) {
      loadSpeakers();
    }
  }

  /**
   * Re-fetch segments after a speaker merge (issue #603).
   *
   * A merge deletes the SOURCE speaker's Postgres row and re-labels its segments under
   * the TARGET speaker. `speaker_processing_complete` for a merge carries only the target
   * uuid, so `applySpeakerRename` — which patches segments that still hold a KNOWN uuid —
   * has no old-to-new mapping and can't find the segments still holding the now-dead
   * source uuid client-side. Merges are rare enough that refetching is the honest fix:
   * reset to empty and replay the same paging path `loadMoreSegments`/`handleLoadUpTo`
   * use (`appendSegmentPage`, which skips already-seen uuids — starting from empty is
   * what turns that into a full replace), preserving however many segments were already
   * loaded rather than force-loading the rest of a not-yet-fully-loaded transcript.
   */
  async function refetchSegmentsAfterMerge(): Promise<void> {
    if (!fileId || !file) return;

    const targetCount = file.transcript_segments?.length || 0;
    file = { ...file, transcript_segments: [], grouped_segments: [] };
    if (targetCount === 0) return;

    try {
      for (let offset = 0; offset < targetCount; offset += segmentLimit) {
        const response = await axiosInstance.get(`/files/${fileId}/segments`, {
          params: {
            segment_limit: Math.min(segmentLimit, targetCount - offset),
            segment_offset: offset,
            ...(showOriginal ? { redact: false } : {})
          }
        });
        if (!response.data?.transcript_segments) break;
        file = appendSegmentPage(file, response.data);
      }

      if (file?.uuid && file.transcript_segments && speakerList) {
        transcriptStore.loadTranscriptData(file.uuid, file.transcript_segments, speakerList);
      }
    } catch (error) {
      console.error('Error refetching segments after speaker merge:', error);
    }
  }

  /**
   * Eventual-consistency backstop for a speaker rename or merge.
   *
   * Renaming a speaker kicks off a background task that projects the change into
   * OpenSearch and retroactively labels matching speakers in other files. This event is
   * how those downstream effects reach an open page. It was dispatched but never listened
   * for, so auto-applied labels only appeared after a manual reload.
   */
  function handleSpeakerProcessingCompleteEvent(e: Event) {
    const detail = (e as CustomEvent).detail;
    if (!detail?.media_file_id || String(detail.media_file_id) !== String(fileId)) return;

    if (detail.reason === 'speaker_merged') {
      // The merged-away speaker's uuid is gone from Postgres; there is no rename mapping
      // to patch segments in place with, so refetch rather than trying to guess. See
      // `refetchSegmentsAfterMerge`'s docstring.
      refetchSegmentsAfterMerge();
    } else if (detail.speaker_uuid && detail.display_name) {
      // Repaint the renamed speaker directly: loadSpeakers() refreshes the speaker list but
      // touches neither the segments nor the transcript store.
      const speakerUuid = String(detail.speaker_uuid);
      const speakerLabel = speakerList.find(s => s.uuid === speakerUuid)?.name;
      applySpeakerRename(speakerUuid, speakerLabel, String(detail.display_name));
    }

    // Picks up labels the task auto-applied to OTHER speakers in this file.
    loadSpeakers();
    refreshCaptionTrack('speaker processing');

    const autoApplied = detail.auto_applied_count || 0;
    const suggested = detail.suggested_count || 0;
    if (autoApplied > 0) {
      toastStore.info($t('speakerProfile.autoAppliedToOthers', { count: autoApplied }));
    } else if (suggested > 0) {
      toastStore.info($t('speakerProfile.suggestionsCreated', { count: suggested }));
    }
  }

  onMount(() => {
    // Listen for speaker-updated CustomEvents (gender detection, attribute changes)
    window.addEventListener('speaker-updated', handleSpeakerUpdatedEvent);
    window.addEventListener('speaker-processing-complete', handleSpeakerProcessingCompleteEvent);

    // Use dynamic URL based on current location (works with reverse proxy)
    apiBaseUrl = getAppBaseUrl();

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

    // LLM status monitoring is now handled by the Settings component and reactive store

    // Subscribe to WebSocket notifications for real-time updates
    wsUnsubscribe = websocketStore.subscribe(($ws) => {


      if ($ws.notifications.length > 0) {

        // Find the most recently updated notification for the current file
        const currentFileNotifications = $ws.notifications.filter(n => {
          const notificationFileId = String(n.data?.file_id || '');
          const currentFileId = String(fileId);
          return notificationFileId === currentFileId;
        });

        if (currentFileNotifications.length === 0) {
          return;
        }

        // Sort by timestamp (most recent first)
        currentFileNotifications.sort((a, b) => {
          const aTime = a.timestamp;
          const bTime = b.timestamp;
          return new Date(bTime).getTime() - new Date(aTime).getTime();
        });

        const latestNotification = currentFileNotifications[0];


        // Create a unique state signature to detect content changes, not just ID changes
        const notificationState = `${latestNotification.id}_${latestNotification.status}_${latestNotification.progress?.percentage}_${latestNotification.currentStep}_${latestNotification.timestamp}`;

        // Only process if the notification content has changed (not just new ID)
        if (notificationState !== lastProcessedNotificationState) {
          lastProcessedNotificationState = notificationState;

          // Check if this notification is for our current file
          // Skip if fileId is not set yet (component still initializing)
          if (!fileId) {
            return;
          }

          // Convert both to strings for comparison since notification sends file_id as string
          const notificationFileId = String(latestNotification.data?.file_id);
          const currentFileId = String(fileId);


          if (notificationFileId === currentFileId && notificationFileId !== 'undefined' && currentFileId !== 'undefined') {

            handleFileNotification(latestNotification, {
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

          } else {
          }
        } else {
        }
      } else {
      }
    });
  });

  onDestroy(() => {
    // Cancel every deferred DOM callback FIRST; it also stops anything still in flight
    // (a late fetch resolution) from scheduling a new one after this teardown has run.
    pageTimers.destroy();
    downloadManager.cleanup();

    // Player cleanup is now handled by VideoPlayer component
    playerInitialized = false;

    // Stop URL refresher for presigned URLs
    if (urlRefresher) {
      urlRefresher.stop();
      urlRefresher = null;
    }

    // LLM status cleanup is handled by the Settings component

    // Clean up WebSocket subscription
    if (wsUnsubscribe) {
      wsUnsubscribe();
    }

    // Clean up CustomEvent listeners
    window.removeEventListener('speaker-updated', handleSpeakerUpdatedEvent);
    window.removeEventListener('speaker-processing-complete', handleSpeakerProcessingCompleteEvent);

    // Clear the transcript store when leaving the page
    transcriptStore.clear();

    // Release the scroll lock held by the speaker-profile dialog
    if (_prevSpeakerConfirm) unlockScroll();
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
          {isPlayerBuffering}
          {loadProgress}
          errorMessage={videoErrorMessage}
          {showOriginal}
          on:retry={handleVideoRetry}
          on:timeupdate={handleTimeUpdate}
          on:play={handlePlay}
          on:pause={handlePause}
          on:loadedmetadata={handleLoadedMetadata}
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
            on:saveSpeakerNames={handleSaveSpeakerNames}
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
{#if showSpeakerProfileConfirmation}
  <SpeakerProfileConfirmModal
    title={profileUpdateTitle}
    message={profileUpdateMessage}
    on:updateProfile={() => handleProfileConfirmation('update_profile')}
    on:createNewProfile={() => handleProfileConfirmation('create_new_profile')}
    on:cancel={handleProfileConfirmationCancel}
  />
{/if}

<!-- Summary Modal -->
{#if file?.uuid}
  <SummaryModal
    bind:isOpen={showSummaryModal}
    fileId={file.uuid}
    fileName={file?.filename || 'Unknown File'}
    on:close={() => showSummaryModal = false}
    on:reprocessSummary={async (_event) => {
      // 1. Close modal immediately
      showSummaryModal = false;

      // 2. Update button to show spinner state
      summaryGenerating = true;

      // 3. Clear the summary from file object to trigger "generating" button state
      if (file) {
        file.has_summary = false;
        file.summary_opensearch_id = null;
        file = { ...file }; // Trigger reactivity
      }

      // 4. Trigger the API call for reprocessing
      try {
        announceTopicExtraction(await requestSummary(file.uuid, { forceRegenerate: true }));

        // WebSocket will handle the rest of the status updates
      } catch (error) {
        console.error('Failed to start reprocess:', error);
        toastStore.error($t('fileDetail.failedToStartSummaryReprocess'), 5000);
        summaryGenerating = false;
      }
    }}
    on:regenerateWithPrompt={async (event) => {
      showSummaryModal = false;
      summaryGenerating = true;

      if (file) {
        file.has_summary = false;
        file.summary_opensearch_id = null;
        file = { ...file };
      }

      try {
        announceTopicExtraction(
          await requestSummary(file.uuid, {
            forceRegenerate: true,
            promptUuid: event.detail.promptUuid,
          })
        );
      } catch (error) {
        console.error('Failed to start regeneration with prompt:', error);
        toastStore.error($t('fileDetail.failedToStartSummaryReprocess'), 5000);
        summaryGenerating = false;
      }
    }}
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
