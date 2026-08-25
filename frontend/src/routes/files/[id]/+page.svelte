<script lang="ts">
  import { onMount, onDestroy, afterUpdate, tick } from 'svelte';
  import type { Segment, Speaker } from '$lib/types/speaker';
  import type { Collection } from '$lib/types/collection';
  import type { Comment } from '$lib/types/comment';
  import type { Tag } from '$lib/types/tag';
  import { lockScroll, unlockScroll } from '$lib/scrollLock';
  import { get } from 'svelte/store';
  import axiosInstance from '$lib/axios';
  import { formatDuration } from '$lib/utils/formatting';
  import { loadTxtPrefs, saveTxtPrefs } from '$lib/export/txtExportPrefs';
  import {
    buildExportContent,
    type ExportFormat,
    type ExportStrings,
  } from '$lib/export/transcriptExport';
  import { websocketStore } from '$stores/websocket';
  import { handleFileNotification } from '$lib/fileDetail/notificationHandler';
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
  import AnalyticsSection from '$components/AnalyticsSection.svelte';
  import TranscriptDisplay from '$components/TranscriptDisplay.svelte';
  import FileHeader from '$components/FileHeader.svelte';
  import TagsSection from '$components/TagsSection.svelte';
  import CommentSection from '$components/CommentSection.svelte';
  import CollectionsSection from '$components/CollectionsSection.svelte';
  import SelectiveReprocessModal from '$components/SelectiveReprocessModal.svelte';
  import { toastStore } from '$stores/toast';
  import { t } from '$stores/locale';
  import { getErrorMessage, getErrorStatus } from '$lib/utils/apiError';
  import ConfirmationModal from '$components/ConfirmationModal.svelte';
  import SummaryModal from '$components/SummaryModal.svelte';
  import TranscriptModal from '$components/TranscriptModal.svelte';
  import TxtExportOptionsModal from '$components/fileDetail/TxtExportOptionsModal.svelte';
  import FileActionButtons from '$components/fileDetail/FileActionButtons.svelte';
  import RedactionControls from '$components/fileDetail/RedactionControls.svelte';
  import RedactionPendingPanel from '$components/fileDetail/RedactionPendingPanel.svelte';
  import SpeakerProfileConfirmModal from '$components/fileDetail/SpeakerProfileConfirmModal.svelte';
  import { isLLMAvailable } from '$stores/llmStatus';
  import { authStore } from '$stores/auth';
  import { transcriptStore, processedTranscriptSegments, type SpeakerInfo } from '$stores/transcriptStore';
  import { getAISuggestions, type TagSuggestion, type CollectionSuggestion } from '$lib/api/suggestions';
  import { getAppBaseUrl } from '$lib/utils/url';
  import { getMediaStreamUrl, getCachedUrlInfo, createUrlRefresher, clearMediaUrlCache } from '$lib/api/mediaUrl';
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
  let pageErrorMessage = '';
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

  // UI state
  let showMetadata = false;
  let isTagsExpanded = false;
  let isCollectionsExpanded = false;
  let isAnalyticsExpanded = false;
  let savingTranscript = false;
  let savingSpeakers = false;
  let editingSegmentId: string | number | null = null;
  let editingSegmentText = '';
  let isEditingSpeakers = false;
  interface SpeakerItem extends SpeakerInfo {
    profile?: { uuid: string; name: string } | null;
    profile_suggestions?: Array<Record<string, any>>;
    [key: string]: any;
  }
  let speakerList: SpeakerItem[] = [];
  let originalSpeakerNames: Map<string, string> = new Map(); // Track original names for change detection
  // Generation counter for loadSpeakers. Renames, websocket events and the initial load
  // can all be in flight at once; a stale response must not overwrite a newer one.
  let speakerLoadSeq = 0;
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

  // Permission level for shared files (null = owner/full access)
  let myPermission: string | null = null;
  $: canEdit = !myPermission || myPermission === 'editor' || myPermission === 'owner';

  // Content redaction: owner/admin can reveal the original (non-admin-forced categories).
  let showOriginal = false;
  let redactionActive = false; // true once we've seen redacted spans on this file
  // When redaction is enabled but detection hasn't finished, the transcript is withheld.
  let redactionPending = false;
  let redactionStatus = ''; // pending | processing | done | failed
  $: canViewOriginal = myPermission === null || myPermission === 'owner';
  $: showRedactionToggle = canViewOriginal && (redactionActive || showOriginal);

  // LLM availability for summary functionality
  $: llmAvailable = $isLLMAvailable;

  // Diarization disabled flag — when true, suppress all speaker-specific UI
  $: diarizationDisabled = file?.diarization_disabled === true;

  // Detect changes in speaker names - depends on speaker display_name values
  $: speakerNamesChanged = speakerList.length > 0 && speakerList.some(speaker => {
    const originalName = originalSpeakerNames.get(speaker.uuid) || '';
    const currentName = (speaker.display_name || '').trim();
    return originalName !== currentName;
  });

  // Reset spinners when LLM becomes unavailable
  $: if (!llmAvailable && (summaryGenerating || generatingSummary)) {
    summaryGenerating = false;
    generatingSummary = false;
  }




  // Confirmation modal state
  let showExportConfirmation = false;
  let pendingExportFormat = '';

  // TXT export options modal state (localStorage persistence in $lib/export/txtExportPrefs)
  let showTxtExportOptions = false;
  let txtExportOptions = { includeTimestamps: true, includeSpeakers: true, includeComments: false, hasComments: false };

  // Speaker profile confirmation modal state
  let showSpeakerProfileConfirmation = false;

  // Scroll lock for the two custom .modal-overlay modals on this page
  let _prevTxtExport = false;
  let _prevSpeakerConfirm = false;
  $: {
    if (showTxtExportOptions !== _prevTxtExport) {
      showTxtExportOptions ? lockScroll() : unlockScroll();
      _prevTxtExport = showTxtExportOptions;
    }
  }
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

  async function fetchFileDetails(fileIdOrEvent?: string): Promise<void> {
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
      videoErrorMessage = '';

      const response = await axiosInstance.get(`/files/${targetFileId}`, {
        params: showOriginal ? { redact: false } : {},
      });

      if (response.data && typeof response.data === 'object') {
        file = response.data;
        collections = response.data.collections || [];
        myPermission = response.data.my_permission || null;
        // Content-redaction state: pending (transcript withheld) + whether masking applied.
        redactionPending = response.data.redaction_pending || false;
        redactionStatus = response.data.redaction_status || '';
        if (!showOriginal && Array.isArray(response.data.transcript_segments)) {
          if (response.data.transcript_segments.some((s: Segment) => s?.redactions?.length)) {
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
          setupVideoUrl(targetFileId);
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
      pageErrorMessage = $t('fileDetail.failedToLoadFile');
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
        if (!showOriginal && Array.isArray(response.data.transcript_segments)) {
          if (response.data.transcript_segments.some((s: Segment) => s?.redactions?.length)) {
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
      toastStore.error($t('settings.contentRedaction.redactTriggerFailed'));
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
      scrollToAndHighlight(segmentUuid);
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
      setTimeout(() => scrollToAndHighlight(segmentUuid), 300);
    } catch (error) {
      console.error('Error loading segments up to target:', error);
      toastStore.error($t('fileDetail.failedToLoadMoreSegments'));
    } finally {
      loadingMoreSegments = false;
    }
  }

  /**
   * Scroll to a segment by UUID and apply a highlight flash animation.
   */
  function scrollToAndHighlight(segmentUuid: string): void {
    const el = document.querySelector(`[data-segment-id="${segmentUuid}"]`);
    if (el) {
      el.scrollIntoView({ behavior: 'smooth', block: 'center' });
      el.classList.add('highlight-flash');
      setTimeout(() => el.classList.remove('highlight-flash'), 2000);
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

  // Speaker sorting is now handled by the backend

  /**
   * Load cross-media appearances ("appears in N other videos") for labeled speakers.
   *
   * Runs in the background: this is one request per already-labeled speaker, and it feeds
   * only the chips in the speaker editor. Awaiting it used to gate the transcript repaint
   * and the success toast behind N round trips, which is what made renaming feel frozen.
   *
   * `seq` is the generation of the `loadSpeakers` call that started this; results from a
   * superseded generation are dropped.
   */
  async function loadCrossMediaDataForLabeledSpeakers(seq: number): Promise<void> {
    if (!speakerList || speakerList.length === 0) return;
    if (file?.diarization_disabled) return; // Skip cross-media for monologue files

    // Find speakers that need cross-media data (labeled speakers without individual matches)
    const speakersNeedingCrossMedia = speakerList.filter(speaker => speaker.needsCrossMediaCall);
    if (speakersNeedingCrossMedia.length === 0) return;

    // Collect into a map rather than writing onto the captured speaker objects: by the
    // time these resolve, `loadSpeakers` may have replaced `speakerList` wholesale and
    // those writes would land on orphaned objects, silently losing the chips.
    const matchesByUuid = new Map<string, any[]>();

    await Promise.allSettled(
      speakersNeedingCrossMedia.map(async (speaker) => {
        try {
          const response = await axiosInstance.get(`/speakers/${speaker.uuid}/cross-media`);
          matchesByUuid.set(speaker.uuid, response.data || []);
        } catch (error) {
          console.error(`Error loading cross-media data for speaker ${speaker.uuid}:`, error);
          matchesByUuid.set(speaker.uuid, []);
        }
      })
    );

    if (seq !== speakerLoadSeq) return; // A newer load won — discard.

    speakerList = speakerList.map(speaker =>
      matchesByUuid.has(speaker.uuid)
        ? { ...speaker, cross_video_matches: matchesByUuid.get(speaker.uuid) }
        : speaker
    );

    // Update transcript store with the new cross-media data
    if (file?.uuid && file.transcript_segments) {
      transcriptStore.loadTranscriptData(file.uuid, file.transcript_segments, speakerList);
    }
  }

  /**
   * Load speakers for the current file
   */
  async function loadSpeakers(): Promise<void> {
    if (!file?.uuid) return;
    const seq = ++speakerLoadSeq;

    try {
      // Load speakers from the backend API
      const response = await axiosInstance.get(`/speakers`, {
        params: { file_uuid: file.uuid }  // Use file_uuid parameter (file.uuid contains UUID)
      });
      if (seq !== speakerLoadSeq) return; // Superseded by a newer load.

      if (response.data && Array.isArray(response.data)) {
        // A websocket event or a sibling rename can land while the user is typing in
        // another speaker's field, and this assignment replaces the whole list. Carry
        // unsaved edits across so they aren't silently wiped mid-keystroke.
        const unsavedNames = new Map<string, string>();
        for (const speaker of speakerList) {
          const baseline = originalSpeakerNames.get(speaker.uuid);
          const current = (speaker.display_name || '').trim();
          if (baseline !== undefined && baseline !== current) {
            unsavedNames.set(speaker.uuid, speaker.display_name || '');
          }
        }

        // Use pre-processed data directly from backend - no frontend business logic
        speakerList = response.data.map((speaker: Speaker) => ({
            ...speaker,
            verified: speaker.verified ?? false,
            display_name: unsavedNames.has(speaker.uuid)
              ? unsavedNames.get(speaker.uuid)
              : speaker.display_name,
            showMatches: false,  // Only UI state, not business logic
            showSuggestions: false  // Only UI state, not business logic
          }));

        // Store original speaker names for change detection (trimmed for consistent
        // comparison). Built from SERVER values, so a preserved edit above still reads
        // as unsaved.
        originalSpeakerNames = new Map(
          response.data.map((speaker: Speaker) => [speaker.uuid, (speaker.display_name || '').trim()])
        );

        // Speakers are now pre-sorted by the backend

        // Load data into the transcript store for reactive updates
        if (file?.uuid && file.transcript_segments) {
          transcriptStore.loadTranscriptData(file.uuid, file.transcript_segments, speakerList);
        }

        // Cross-media feeds a secondary panel only — fetch it after the render above,
        // and don't make callers wait for it.
        void loadCrossMediaDataForLabeledSpeakers(seq);

      } else {
        // Fallback: extract from transcript data
        const transcriptData = file?.transcript_segments;
        if (transcriptData) {
          const speakers = new Map();
          transcriptData.forEach((segment: Segment) => {
            const speakerLabel = segment.speaker_label || segment.speaker?.name || $t('fileDetail.unknownSpeaker');
            if (!speakers.has(speakerLabel)) {
              speakers.set(speakerLabel, {
                name: speakerLabel,
                display_name: segment.speaker?.display_name || speakerLabel
              });
            }
          });
          speakerList = Array.from(speakers.values());
          // Backend provides pre-sorted speakers

          // Load data into the transcript store for fallback case
          if (file?.uuid && file.transcript_segments) {
            transcriptStore.loadTranscriptData(file.uuid, file.transcript_segments, speakerList);
          }
        }
      }
    } catch (error) {
      console.error('Error loading speakers:', error);
      // Fallback: extract from transcript data
      const transcriptData = file?.transcript_segments;
      if (transcriptData) {
        const speakers = new Map();
        transcriptData.forEach((segment: Segment) => {
          const speakerLabel = segment.speaker_label || segment.speaker?.name || get(t)('fileDetail.unknownSpeaker');
          if (!speakers.has(speakerLabel)) {
            speakers.set(speakerLabel, {
              name: speakerLabel,
              display_name: segment.speaker?.display_name || speakerLabel
            });
          }
        });
        speakerList = Array.from(speakers.values());
        // Backend provides pre-sorted speakers

        // Load data into the transcript store for error fallback case
        if (file?.uuid && file.transcript_segments) {
          transcriptStore.loadTranscriptData(file.uuid, file.transcript_segments, speakerList);
        }
      }
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
  async function setupVideoUrl(fileId: string) {
    try {
      // Stop any existing URL refresher
      if (urlRefresher) {
        urlRefresher.stop();
        urlRefresher = null;
      }

      // Clear cached URL to ensure fresh presigned URL
      clearMediaUrlCache(fileId);

      // Get presigned URL from backend (authenticated, time-limited)
      videoUrl = await getMediaStreamUrl(fileId, 'video');

      // Set up automatic URL refresh for long videos, using the URL's real expiry
      // (MEDIA_URL_EXPIRE_SECONDS) rather than a hardcoded interval — avoids needlessly
      // re-fetching and re-setting the video src mid-playback.
      const info = getCachedUrlInfo(fileId, 'video');
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

  /**
   * Update current segment highlighting without auto-scrolling
   */
  function updateCurrentSegment(currentPlaybackTime: number): void {
    const transcriptData = file?.transcript_segments;
    if (!file || !transcriptData || !Array.isArray(transcriptData)) return;

    const allSegments = document.querySelectorAll('.transcript-segment');
    allSegments.forEach(segment => {
      segment.classList.remove('active-segment');
    });

    const currentSegment = transcriptData.find((segment: Segment) => {
      return currentPlaybackTime >= segment.start_time && currentPlaybackTime <= segment.end_time;
    });

    if (currentSegment) {
      const segmentElement = document.querySelector(`[data-segment-id="${currentSegment.uuid || currentSegment.id || `${currentSegment.start_time}-${currentSegment.end_time}`}"]`);
      if (segmentElement) {
        segmentElement.classList.add('active-segment');
        // Remove auto-scroll to allow manual scrolling
      }
    }
  }

  // Event handlers for components
  function handleSegmentClick(event: any) {
    const startTime = event.detail.startTime;
    seekToTime(startTime);
  }


  // Validate speaker name
  function validateSpeakerName(name: string, speakerId: string | number): { isValid: boolean; error?: string } {
    if (!name || typeof name !== 'string') {
      return { isValid: false, error: $t('speakerValidation.nameRequired') };
    }

    const trimmedName = name.trim();
    if (trimmedName.length === 0) {
      return { isValid: false, error: $t('speakerValidation.nameEmpty') };
    }

    if (trimmedName.length > 100) {
      return { isValid: false, error: $t('speakerValidation.nameTooLong') };
    }

    // Allow duplicate display names - users can label multiple speakers with the same name
    // and merge them later using the Speaker Merge feature when confident they're the same person
    return { isValid: true };
  }

  /**
   * Whether this speaker's display name differs from the last-saved baseline —
   * including a change TO empty, which is the supported way to clear a label back
   * to the raw diarizer name (`PUT /speakers/{uuid}` accepts `display_name: ""`;
   * `canonical_speaker_label` on the backend falls back to the raw `SPEAKER_XX`
   * name whenever `display_name` is empty/unset). Filtering on the CURRENT
   * value's shape — empty or `SPEAKER_`-prefixed — instead of on whether it
   * actually changed was the bug: it silently dropped exactly the "clear a
   * label" case from the save payload, while the bulk save still reported
   * "saved successfully" for it.
   */
  function speakerHasUnsavedChange(speaker: SpeakerItem): boolean {
    if (!speaker.uuid) return false;
    const originalName = originalSpeakerNames.get(speaker.uuid) ?? '';
    const currentName = (speaker.display_name ?? '').trim();
    return originalName !== currentName;
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
    const validation = validateSpeakerName(newName, speakerId);
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
    // Regenerating the WebVTT track is local work with no network call, and it reads the
    // props we just changed — so wait a tick, but don't make the caller wait on it.
    tick()
      .then(() => videoPlayerComponent?.updateSubtitles?.())
      .catch(error => console.warn('Failed to update subtitles after speaker rename:', error));
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

        // Clear cached processed videos so downloads use the updated transcript.
        axiosInstance.delete(`/files/${file.uuid}/cache`).catch((error: unknown) => {
          console.warn('Could not clear video cache:', error);
        });

        tick()
          .then(() => videoPlayerComponent?.updateSubtitles?.())
          .catch(error => console.warn('Failed to update subtitles after segment edit:', error));

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
  }


  async function handleExportTranscript(event: any) {
    const format = event.detail.format;
    let transcriptData = file?.transcript_segments;
    if (!file || !transcriptData) return;

    // CommentSection already holds this and reports it as it changes — refetching it here
    // just to set a boolean put a round trip between clicking a format and seeing the
    // options dialog.
    const hasComments = commentCount > 0;

    if (format === 'txt') {
      // Show TXT-specific options modal (timestamps, speakers, comments)
      const prefs = loadTxtPrefs();
      txtExportOptions = {
        ...prefs,
        includeComments: false,
        hasComments,
        // When diarization disabled, force speakers off
        ...(diarizationDisabled ? { includeSpeakers: false } : {})
      };
      showTxtExportOptions = true;
      return;
    }

    // For other formats: use the existing comments-only confirmation flow
    if (!hasComments) {
      pendingExportFormat = format;
      processExportWithComments(false);
      return;
    }

    // If comments exist, show confirmation modal
    pendingExportFormat = format;
    showExportConfirmation = true;
  }

  async function processExportWithComments(includeComments: boolean, txtOptions?: { includeTimestamps: boolean; includeSpeakers: boolean }) {
    const format = pendingExportFormat;
    let transcriptData = file?.transcript_segments;
    if (!file || !transcriptData) return;
    // Fetch comments if user wants to include them
    let fileComments: Comment[] = [];
    if (includeComments) {
      try {
        const endpoint = `/comments/files/${file.uuid}/comments`;
        const response = await axiosInstance.get(endpoint);
        fileComments = response.data || [];

        // Get current user data from auth store
        const userData = $authStore.user || {} as any;

        // Add current user data to each comment
        fileComments = fileComments.map((comment: Comment) => {
          // If the comment is from the current user, add their details
          if (!comment.user && comment.user_id === userData.uuid) {
            comment.user = {
              full_name: userData.full_name,
              username: userData.username,
              email: userData.email
            };
          } else if (!comment.user) {
            // For other users' comments that have no user object,
            // create a placeholder to avoid 'Anonymous'
            comment.user = {
              full_name: $t('fileDetail.adminUser'), // Default from browser info
              username: 'admin',
              email: 'admin@example.com'
            };
          }
          return comment;
        });

        // Sort comments by timestamp
        fileComments.sort((a: Comment, b: Comment) => a.timestamp - b.timestamp);
      } catch (error) {
        console.error('Error fetching comments for export:', error);
        // Continue with export even if comments can't be fetched
      }
    }

    try {
      const filename = file.filename.replace(/\.[^/.]+$/, '');

      // Resolve i18n strings here (Svelte store access stays in the page); the pure
      // serializer in $lib/export consumes them so it can remain store-free.
      const translations: ExportStrings = {
        speakerDefault: $t('fileDetail.speakerDefault'),
        userComment: $t('fileDetail.userComment'),
        commentType: $t('fileDetail.commentType'),
        csvHeaderDefault: $t('fileDetail.csvHeaderDefault'),
        csvHeaderWithComments: $t('fileDetail.csvHeaderWithComments'),
      };

      const content = buildExportContent(
        format as ExportFormat,
        transcriptData,
        speakerList,
        fileComments,
        {
          includeComments,
          txtOptions,
          filename,
          jsonMeta: { filename: file.filename, duration: file.duration },
          translations,
        }
      );

      const blob = new Blob([content], { type: 'text/plain' });
      const url = window.URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = `${filename}.${format}`;
      link.click();
      window.URL.revokeObjectURL(url);
      // Transcript exported successfully
    } catch (error) {
      console.error('Error exporting transcript:', error);
    }
  }

  // Confirmation modal handlers
  function handleExportConfirm() {
    processExportWithComments(true);
  }

  function handleExportCancel() {
    processExportWithComments(false);
  }

  function handleExportModalClose() {
    // Just close the modal without doing anything
    showExportConfirmation = false;
  }

  // TXT export options modal handler
  async function handleTxtExportConfirm() {
    showTxtExportOptions = false;
    saveTxtPrefs({
      includeTimestamps: txtExportOptions.includeTimestamps,
      includeSpeakers: txtExportOptions.includeSpeakers
    });
    pendingExportFormat = 'txt';
    await processExportWithComments(txtExportOptions.includeComments, {
      includeTimestamps: txtExportOptions.includeTimestamps,
      includeSpeakers: txtExportOptions.includeSpeakers
    });
  }

  async function handleSaveSpeakerNames() {
    if (!speakerList || speakerList.length === 0) return;

    savingSpeakers = true;

    try {
      // Speakers whose display name actually changed since the last load/save —
      // including a change to empty (see speakerHasUnsavedChange). The old filter
      // dropped both empty and SPEAKER_-prefixed CURRENT values regardless of
      // whether they had changed, so "clear a label" issued no PUT at all.
      const speakersToUpdate = speakerList.filter(speakerHasUnsavedChange);

      if (speakersToUpdate.length === 0) {
        savingSpeakers = false;
        return;
      }

      // Validate non-empty names only — an empty name is a deliberate clear
      // (see speakerHasUnsavedChange), not a validation failure.
      for (const speaker of speakersToUpdate) {
        const trimmed = (speaker.display_name ?? '').trim();
        if (trimmed === '') continue;
        const validation = validateSpeakerName(trimmed, speaker.uuid);
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
      const speakersToUpdate = speakerList.filter(speakerHasUnsavedChange);

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

    // Update subtitles and clear cache (async, don't block)
    tick()
      .then(() => videoPlayerComponent?.updateSubtitles?.())
      .catch(error => console.warn('Failed to update subtitles after saving speaker names:', error));

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
    updateCurrentSegment(currentTime);
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

      await axiosInstance.post(`/files/${file.uuid}/summarize`);

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
   * Eventual-consistency backstop for a speaker rename.
   *
   * Renaming a speaker kicks off a background task that projects the change into
   * OpenSearch and retroactively labels matching speakers in other files. This event is
   * how those downstream effects reach an open page. It was dispatched but never listened
   * for, so auto-applied labels only appeared after a manual reload.
   */
  function handleSpeakerProcessingCompleteEvent(e: Event) {
    const detail = (e as CustomEvent).detail;
    if (!detail?.media_file_id || String(detail.media_file_id) !== String(fileId)) return;

    // Repaint the renamed speaker directly: loadSpeakers() refreshes the speaker list but
    // touches neither the segments nor the transcript store.
    if (detail.speaker_uuid && detail.display_name) {
      const speakerUuid = String(detail.speaker_uuid);
      const speakerLabel = speakerList.find(s => s.uuid === speakerUuid)?.name;
      applySpeakerRename(speakerUuid, speakerLabel, String(detail.display_name));
    }

    // Picks up labels the task auto-applied to OTHER speakers in this file.
    loadSpeakers();

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
      fetchFileDetails().catch(err => {
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

    // Release any scroll locks held by the custom modals on this page
    if (_prevTxtExport) unlockScroll();
    if (_prevSpeakerConfirm) unlockScroll();
  });

  afterUpdate(() => {
    if (videoUrl && !playerInitialized && !isLoading && !videoElementChecked) {
      videoElementChecked = true;
      const videoElement = document.getElementById('player');
      if (videoElement) {
        // Video element found, initializing player
        setTimeout(() => initializePlayer(), 100); // Small delay to ensure element is fully rendered
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
        // Seek the player to the specified timestamp
        setTimeout(() => {
          if (videoPlayerComponent && typeof videoPlayerComponent.seekToTime === 'function') {
            videoPlayerComponent.seekToTime(targetTime);
          } else {
            currentTime = targetTime;
          }
          // Scroll the transcript segment into view
          scrollToSegmentAtTime(targetTime);
        }, 500);
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

  function scrollToSegmentAtTime(time: number) {
    const transcriptData = file?.transcript_segments;
    if (!transcriptData || !Array.isArray(transcriptData)) return;
    const segment = transcriptData.find((s: Segment) => time >= s.start_time && time <= s.end_time);
    if (!segment) return;
    const segId = segment.uuid || segment.id || `${segment.start_time}-${segment.end_time}`;
    // Wait for DOM to update with the active-segment class
    setTimeout(() => {
      const el = document.querySelector(`[data-segment-id="${segId}"]`);
      if (el) {
        el.scrollIntoView({ behavior: 'smooth', block: 'center' });
      }
    }, 300);
  }

  // Reactive statement to re-initialize player if videoUrl changes
  $: if (videoUrl && !playerInitialized && !isLoading) {
    // Video URL available but no player, scheduling initialization
    setTimeout(() => {
      if (!playerInitialized) {
        initializePlayer();
      }
    }, 200);
  }
</script>

<svelte:head>
  <title>{file?.filename || $t('fileDetail.loadingFile')}</title>
</svelte:head>

<div class="file-detail-page">
  {#if isLoading}
    <!-- Skeleton loader mirroring the final layout — perceived as ~20% faster
         than a spinner because users see structure + anticipated content -->
    <FileDetailSkeleton />
  {:else if pageErrorMessage}
    <div class="error-container">
      <p class="error-message">{pageErrorMessage}</p>
      <button
        on:click={() => fetchFileDetails()}
        title={$t('fileDetail.retryTooltip')}
      >{$t('fileDetail.tryAgain')}</button>
    </div>
  {:else if file}
    <div class="file-header">
      <FileHeader
        {file}
        {currentProcessingStep}
        sharedPermission={myPermission}
        on:titleUpdated={(e) => { if (file) file.title = e.detail.title; }}
      />

      <MetadataDisplay
        {file}
        bind:showMetadata
      />
    </div>

    <div class="main-content-grid">
      <!-- Left column: Video player, tags, analytics, and comments -->
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
          {file}
          {isPlayerBuffering}
          {loadProgress}
          errorMessage={videoErrorMessage}
          {speakerList}
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

        <TagsSection
          {file}
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


        <AnalyticsSection
          {file}
          bind:isAnalyticsExpanded
          {speakerList}
          transcriptStore={$transcriptStore}
          {diarizationDisabled}
        />

        <CommentSection
          fileId={file?.uuid ? String(file.uuid) : ''}
          {currentTime}
          on:seekTo={handleSeekTo}
          on:commentsChanged={(e) => (commentCount = e.detail.count)}
        />
      </section>

      <!-- Right column: Transcript -->
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
          {savingSpeakers}
          {speakerNamesChanged}
          {editingSegmentId}
          bind:editingSegmentText
          {isEditingSpeakers}
          {speakerList}
          {reprocessing}
          {diarizationDisabled}
          {totalSegments}
          {hasMoreSegments}
          {loadingMoreSegments}
          on:segmentClick={handleSegmentClick}
          on:editSegment={handleEditSegment}
          on:saveSegment={handleSaveSegment}
          on:cancelEditSegment={handleCancelEditSegment}
          on:exportTranscript={handleExportTranscript}
          on:saveSpeakerNames={handleSaveSpeakerNames}
          on:speakerUpdate={handleSpeakerUpdate}
          on:speakerNameChanged={handleSpeakerNameChanged}
          on:speakersMerged={handleSpeakersMerged}
          on:speakerCreated={handleSpeakerCreated}
          on:speakerDeleted={handleSpeakerDeleted}
          on:analyticsRefreshNeeded={handleAnalyticsRefreshNeeded}
          on:reprocess={handleReprocess}
          on:seekToPlayhead={handleSeekTo}
          on:loadMore={loadMoreSegments}
          on:loadUpTo={handleLoadUpTo}
        />
          <!-- Content-redaction controls: kept BELOW the transcript so the video and -->
          <!-- transcript columns stay top-aligned. -->
          <RedactionControls
            {showRedactionToggle}
            {canViewOriginal}
            {showOriginal}
            {redactionToggleBusy}
            on:rescan={triggerRedaction}
            on:toggleOriginal={toggleShowOriginal}
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

<!-- Export Confirmation Modal -->
<ConfirmationModal
  bind:isOpen={showExportConfirmation}
  title={$t('exportConfirm.title')}
  message={$t('exportConfirm.message')}
  confirmText={$t('exportConfirm.includeComments')}
  cancelText={$t('exportConfirm.exportWithout')}
  on:confirm={handleExportConfirm}
  on:cancel={handleExportCancel}
  on:close={handleExportModalClose}
/>

<!-- TXT Export Options Modal -->
<TxtExportOptionsModal
  bind:show={showTxtExportOptions}
  bind:includeTimestamps={txtExportOptions.includeTimestamps}
  bind:includeSpeakers={txtExportOptions.includeSpeakers}
  bind:includeComments={txtExportOptions.includeComments}
  hasComments={txtExportOptions.hasComments}
  {diarizationDisabled}
  {redactionActive}
  {showOriginal}
  {canViewOriginal}
  on:confirm={handleTxtExportConfirm}
  on:close={() => showTxtExportOptions = false}
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
        await axiosInstance.post(`/files/${file.uuid}/summarize`, {
          force_regenerate: true
        });

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
        await axiosInstance.post(`/files/${file.uuid}/summarize`, {
          force_regenerate: true,
          prompt_uuid: event.detail.promptUuid
        });
      } catch (error) {
        console.error('Failed to start regeneration with prompt:', error);
        toastStore.error($t('fileDetail.failedToStartSummaryReprocess'), 5000);
        summaryGenerating = false;
      }
    }}
  />
{/if}

<!-- Transcript Modal -->
{#if file?.uuid}
  <TranscriptModal
    bind:isOpen={showTranscriptModal}
    fileId={file.uuid}
    fileName={file?.filename || 'Unknown File'}
    {totalSpeakerSegments}
    {hasMoreSegments}
    {loadingMoreSegments}
    {diarizationDisabled}
    {showRedactionToggle}
    {showOriginal}
    {redactionToggleBusy}
    on:toggleRedaction={toggleShowOriginal}
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
