<script lang="ts">
  import { onMount, onDestroy, afterUpdate } from 'svelte';
  import { lockScroll, unlockScroll } from '$lib/scrollLock';
  import { writable, get } from 'svelte/store';
  import axiosInstance from '$lib/axios';
  import { formatDuration } from '$lib/utils/formatting';
  import { loadTxtPrefs, saveTxtPrefs } from '$lib/export/txtExportPrefs';
  import {
    buildExportContent,
    type ExportFormat,
    type ExportStrings,
  } from '$lib/export/transcriptExport';
  import { websocketStore } from '$stores/websocket';

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
  import ConfirmationModal from '$components/ConfirmationModal.svelte';
  import SummaryModal from '$components/SummaryModal.svelte';
  import TranscriptModal from '$components/TranscriptModal.svelte';
  import TxtExportOptionsModal from '$components/fileDetail/TxtExportOptionsModal.svelte';
  import { isLLMAvailable } from '$stores/llmStatus';
  import { authStore } from '$stores/auth';
  import { transcriptStore, processedTranscriptSegments } from '$stores/transcriptStore';
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
  let collections: any[] = [];

  // UI state
  let showMetadata = false;
  let isTagsExpanded = false;
  let isCollectionsExpanded = false;
  let isAnalyticsExpanded = false;
  let isEditingTranscript = false;
  let editedTranscript = '';
  let savingTranscript = false;
  let savingSpeakers = false;
  let editingSegmentId: string | number | null = null;
  let editingSegmentText = '';
  let isEditingSpeakers = false;
  let speakerList: any[] = [];
  let originalSpeakerNames: Map<string, string> = new Map(); // Track original names for change detection
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
  let pendingSpeakerUpdate = null;
  let profileUpdateMessage = '';
  let profileUpdateTitle = '';

  // Bulk speaker save confirmation state
  let speakerConfirmationQueue = [];
  let currentConfirmationIndex = 0;
  let bulkSaveInProgress = false;
  let bulkSaveDecisions = new Map();

  // Reactive store for file updates
  const reactiveFile = writable(null);


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
        // Update all transcript and processing-related fields while preserving UI state flags
        file.transcript_segments = response.data.transcript_segments || [];
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
        reactiveFile.set(file);

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
          if (response.data.transcript_segments.some((s: any) => s?.redactions?.length)) {
            redactionActive = true;
          }
        }
        reactiveFile.set(file);

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
   * Re-fetches the transcript from scratch with the appropriate redact flag.
   * Admin-forced categories always remain masked regardless of this toggle.
   */
  async function toggleShowOriginal(): Promise<void> {
    showOriginal = !showOriginal;
    await fetchFileDetails(fileId);
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
        // Append new segments to existing ones
        file.transcript_segments = [
          ...(file.transcript_segments || []),
          ...response.data.transcript_segments
        ];
        file = { ...file }; // Trigger reactivity
        reactiveFile.set(file);

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
      const neededCount = targetIndex - currentCount + buffer + 1;

      const response = await axiosInstance.get(`/files/${fileId}/segments`, {
        params: {
          segment_limit: neededCount,
          segment_offset: currentCount,
          ...(showOriginal ? { redact: false } : {})
        }
      });

      if (response.data && response.data.transcript_segments) {
        file.transcript_segments = [
          ...(file.transcript_segments || []),
          ...response.data.transcript_segments
        ];
        file = { ...file };
        reactiveFile.set(file);
        totalSegments = response.data.total_segments || totalSegments;

        if (file?.uuid && file.transcript_segments && speakerList) {
          transcriptStore.loadTranscriptData(file.uuid, file.transcript_segments, speakerList);
        }
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

      // Update transcript text for editing
      editedTranscript = transcriptData.map((seg: any) =>
        `${seg.display_timestamp || seg.formatted_timestamp || formatDuration(seg.start_time)} [${seg.speaker_label || seg.speaker?.name || 'Speaker'}]: ${seg.text}`
      ).join('\n');

      // Load speakers and update store after they're loaded
      loadSpeakers();
    } catch (error) {
      console.error('Error processing transcript:', error);
    }
  }

  // Speaker sorting is now handled by the backend

  /**
   * Load cross-media appearances for labeled speakers
   */
  async function loadCrossMediaDataForLabeledSpeakers(): Promise<void> {
    if (!speakerList || speakerList.length === 0) return;
    if (file?.diarization_disabled) return; // Skip cross-media for monologue files

    // Find speakers that need cross-media data (labeled speakers without individual matches)
    const speakersNeedingCrossMedia = speakerList.filter(speaker => speaker.needsCrossMediaCall);

    // Load cross-media data for all labeled speakers in parallel
    await Promise.allSettled(
      speakersNeedingCrossMedia.map(async (speaker) => {
        try {
          const response = await axiosInstance.get(`/speakers/${speaker.uuid}/cross-media`);
          speaker.cross_video_matches = response.data || [];
        } catch (error) {
          console.error(`Error loading cross-media data for speaker ${speaker.uuid}:`, error);
          speaker.cross_video_matches = [];
        }
      })
    );


    // Trigger reactivity by updating the speakerList reference
    speakerList = [...speakerList];

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

    try {
      // Load speakers from the backend API
      const response = await axiosInstance.get(`/speakers`, {
        params: { file_uuid: file.uuid }  // Use file_uuid parameter (file.uuid contains UUID)
      });

      if (response.data && Array.isArray(response.data)) {
        // Use pre-processed data directly from backend - no frontend business logic
        speakerList = response.data.map((speaker: any) => ({
            ...speaker,
            showMatches: false,  // Only UI state, not business logic
            showSuggestions: false  // Only UI state, not business logic
          }));

        // Store original speaker names for change detection (trimmed for consistent comparison)
        originalSpeakerNames = new Map(
          speakerList.map(speaker => [speaker.uuid, (speaker.display_name || '').trim()])
        );

        // Speakers are now pre-sorted by the backend

        // Load cross-media data for labeled speakers
        await loadCrossMediaDataForLabeledSpeakers();

        // Load data into the transcript store for reactive updates
        if (file?.uuid && file.transcript_segments) {
          transcriptStore.loadTranscriptData(file.uuid, file.transcript_segments, speakerList);
        }

      } else {
        // Fallback: extract from transcript data
        const transcriptData = file?.transcript_segments;
        if (transcriptData) {
          const speakers = new Map();
          transcriptData.forEach((segment: any) => {
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
        transcriptData.forEach((segment: any) => {
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

    const currentSegment = transcriptData.find((segment: any) => {
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
      // Silently refresh file data without showing loading state
      const response = await axiosInstance.get(`/files/${file.uuid}`);

      if (response.data && typeof response.data === 'object') {
        // Update file data (includes analytics and transcript segments)
        file = response.data;
        collections = response.data.collections || [];
        reactiveFile.set(file);

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
        reactiveFile.set(file);
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
      toastStore.error(validation.error);
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
      await performSpeakerUpdate(speakerId, newName, decision);

      // Reset modal state
      showSpeakerProfileConfirmation = false;
      pendingSpeakerUpdate = null;
      profileUpdateMessage = '';
      profileUpdateTitle = '';
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
        newName: speaker.display_name,
        speaker
      };
      profileUpdateTitle = $t('fileDetail.updateSpeakerProfileCounter', { current: currentConfirmationIndex + 1, total: speakerConfirmationQueue.length });
      profileUpdateMessage = $t('fileDetail.profileLinkedMessage', { displayName: speaker.display_name || speaker.name, profileName: speaker.profile.name });
      showSpeakerProfileConfirmation = true;
    }
  }

  // Perform the actual speaker update with the specified action
  async function performSpeakerUpdate(speakerId: number | string, newName: string, action: 'normal' | 'update_profile' | 'create_new_profile') {
    // Update the speaker in the speakerList and maintain sort order
    // IMPORTANT: Only update display_name, NEVER change name (original speaker ID for color consistency)
    speakerList = speakerList
      .map(speaker => {
        if (speaker.uuid === speakerId) {
          return { ...speaker, display_name: newName };
        }
        return speaker;
      });
      // Backend provides pre-sorted speakers

    // Update the transcript store FIRST - this will trigger reactive updates in TranscriptModal
    transcriptStore.updateSpeakerName(String(speakerId), newName);

    // Update transcript segment speaker names in file object (for other components)
    const transcriptData = file?.transcript_segments;
    if (transcriptData && Array.isArray(transcriptData)) {
      transcriptData.forEach(segment => {
        if (segment.speaker_id === speakerId) {
          // Update ALL speaker name fields that components might use
          segment.resolved_speaker_name = newName;
          if (segment.speaker) {
            segment.speaker.display_name = newName;
          } else {
            // Create speaker object if it doesn't exist
            segment.speaker = {
              id: speakerId,
              name: segment.speaker_label || `SPEAKER_${speakerId}`,
              display_name: newName
            };
          }
        }
      });

      // Update file data
      file.transcript_segments = transcriptData.map(segment => ({ ...segment }));
      file = { ...file }; // Trigger reactivity
      reactiveFile.set(file);
    }

    // Update subtitles in the video player with new speaker names
    if (videoPlayerComponent && videoPlayerComponent.updateSubtitles) {
      try {
        await videoPlayerComponent.updateSubtitles();
      } catch (error) {
        console.warn('Failed to update subtitles after speaker update:', error);
      }
    }

    // Persist to database with the action decision
    try {
      const speaker = speakerList.find(s => s.uuid === speakerId);
      if (speaker && speaker.uuid) {
        const payload: any = {
          display_name: newName
          // NEVER update 'name' field - it contains the original speaker ID for color consistency
        };

        // Add profile action if needed
        if (action !== 'normal') {
          payload.profile_action = action;
        }

        await axiosInstance.put(`/speakers/${speaker.uuid}`, payload);

        // Refresh speakers to get updated profile data from GET endpoint
        // This ensures speaker.profile.name is current before the next edit
        await loadSpeakers();

        // Show success feedback with appropriate message
        const successMessage = action === 'update_profile'
          ? $t('speakerProfile.updatedGlobally', { name: newName })
          : action === 'create_new_profile'
          ? $t('speakerProfile.newCreated', { name: newName })
          : $t('speakerProfile.renamed', { name: newName });

        toastStore.success(successMessage);
      }
    } catch (error: any) {
      console.error('Failed to update speaker name in database:', error);

      // Show user-friendly error with option to retry
      const errorMessage = error.response?.status === 404
        ? $t('speakerProfile.notFound')
        : error.response?.status === 403
        ? $t('speakerProfile.permissionDenied')
        : $t('speakerProfile.saveFailed');

      toastStore.error($t('speakerProfile.errorWithLocal', { error: errorMessage }));

      // Note: We don't revert frontend changes as they're useful even without backend persistence
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
        // Update the transcript store FIRST for reactivity
        transcriptStore.updateSegmentText(segmentUuid, editingSegmentText);

        // Update the specific segment in local data
        const transcriptData = file?.transcript_segments;
        if (transcriptData && file) {
          const segmentIndex = transcriptData.findIndex((s: any) => s.uuid === segmentUuid);

          if (segmentIndex !== -1) {
            // Create a new array with the updated segment, preserving speaker data
            const updatedSegments = [...transcriptData];
            const originalSegment = updatedSegments[segmentIndex];

            // CRITICAL: Merge response data but preserve original speaker information
            updatedSegments[segmentIndex] = {
              ...originalSegment, // Keep all original data (including speaker info)
              ...response.data,   // Apply backend updates
              // Explicitly preserve speaker-related fields that determine colors
              speaker_label: originalSegment.speaker_label,
              speaker_id: originalSegment.speaker_id,
              speaker: originalSegment.speaker,
              resolved_speaker_name: originalSegment.resolved_speaker_name
            };


            // Update file with new segments array
            file = {
              ...file,
              transcript_segments: updatedSegments
            };
            reactiveFile.set(file);


            // Clear cached processed videos so downloads will use updated transcript
            try {
              await axiosInstance.delete(`/files/${file.uuid}/cache`);
            } catch (error) {
              console.warn('Could not clear video cache:', error);
            }
          }
        }

        editingSegmentId = null;
        editingSegmentText = '';

        // Update subtitles in the video player
        if (videoPlayerComponent && videoPlayerComponent.updateSubtitles) {
          try {
            await videoPlayerComponent.updateSubtitles();
          } catch (error) {
            console.warn('Failed to update subtitles after segment edit:', error);
          }
        }
      }
    } catch (error: any) {
      console.error('Error saving segment:', error);

      // Show error as toast notification for consistency
      if (error.response?.status === 405) {
        toastStore.error($t('fileDetail.transcriptEditingNotSupported'));
      } else if (error.response?.status === 404) {
        toastStore.error($t('fileDetail.transcriptSegmentNotFound'));
      } else if (error.response?.status === 422) {
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

  async function handleSaveTranscript() {
    if (!editedTranscript || !file) return;

    try {
      savingTranscript = true;
      const response = await axiosInstance.put(`/files/${fileId}/transcript`, {
        transcript: editedTranscript
      });

      if (response.data) {
        // Refresh file data to get updated segments
        await fetchFileDetails(fileId);

        // The fetchFileDetails will reload the transcript store via processTranscriptData() and loadSpeakers()
        // so the transcript modal will automatically update

        isEditingTranscript = false;
      }
    } catch (error) {
      console.error('Error saving transcript:', error);
      toastStore.error($t('fileDetail.failedToSaveTranscript'));
    } finally {
      savingTranscript = false;
    }
  }


  async function handleExportTranscript(event: any) {
    const format = event.detail.format;
    let transcriptData = file?.transcript_segments;
    if (!file || !transcriptData) return;

    // Check if there are any comments for this file
    let hasComments = false;
    try {
      const endpoint = `/comments/files/${file.uuid}/comments`;
      const response = await axiosInstance.get(endpoint);
      const fetchedComments = response.data || [];
      hasComments = fetchedComments.length > 0;
    } catch (error) {
      console.error('Error checking for comments:', error);
      hasComments = false;
    }

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
    let fileComments: any[] = [];
    if (includeComments) {
      try {
        const endpoint = `/comments/files/${file.uuid}/comments`;
        const response = await axiosInstance.get(endpoint);
        fileComments = response.data || [];

        // Get current user data from auth store
        const userData = $authStore.user || {} as any;

        // Add current user data to each comment
        fileComments = fileComments.map((comment: any) => {
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
        fileComments.sort((a: any, b: any) => a.timestamp - b.timestamp);
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
      // Validate all speaker names first
      const speakersToUpdate = speakerList.filter(speaker =>
        speaker.uuid &&
        speaker.display_name &&
        speaker.display_name.trim() !== "" &&
        !speaker.display_name.startsWith('SPEAKER_')
      );

      // Validate each speaker name
      for (const speaker of speakersToUpdate) {
        const validation = validateSpeakerName(speaker.display_name, speaker.uuid);
        if (!validation.isValid) {
          toastStore.error(`${speaker.name}: ${validation.error}`);
          savingSpeakers = false;
          return;
        }
      }

      // Check for speakers that need profile confirmation (skip when diarization disabled)
      const speakersNeedingConfirmation = diarizationDisabled ? [] : speakersToUpdate.filter(speaker =>
        speaker.profile && speaker.profile.name !== speaker.display_name.trim()
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
      const speakersToUpdate = speakerList.filter(speaker =>
        speaker.uuid &&
        speaker.display_name &&
        speaker.display_name.trim() !== "" &&
        !speaker.display_name.startsWith('SPEAKER_')
      );

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
  async function performBulkSave(speakersToUpdate, decisions = new Map()) {
    // STEP 1: Optimistic UI updates - immediately update voice suggestions with new names
    const nameChanges = new Map(); // Track profile name changes for voice suggestions

    speakersToUpdate.forEach((speaker: any) => {
      const decision = decisions.get(speaker.uuid);
      const newName = speaker.display_name.trim();

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
    const updatePromises = speakersToUpdate.map(async (speaker: any) => {
      const decision = decisions.get(speaker.uuid);
      const payload: any = {
        display_name: speaker.display_name.trim(),
        name: speaker.name
      };

      // Add profile action if there's a decision for this speaker
      if (decision) {
        payload.profile_action = decision.decision;
      }

      return axiosInstance.put(`/speakers/${speaker.uuid}`, payload);
    });

    await Promise.all(updatePromises);

    // STEP 4: PostgreSQL updates complete - stop save button spinner immediately!
    savingSpeakers = false;
    isEditingSpeakers = false;
    toastStore.success($t('speakerProfile.savedSuccess'));

    // Reset original names to current values (no changes after save)
    originalSpeakerNames = new Map(
      speakerList.map(speaker => [speaker.uuid, (speaker.display_name || '').trim()])
    );

    // STEP 5: Update the transcript store for reactive updates (instant)
    speakerList.forEach((speaker: any) => {
      if (speaker.uuid && speaker.display_name && speaker.display_name.trim() !== "" && !speaker.display_name.startsWith('SPEAKER_')) {
        transcriptStore.updateSpeakerName(speaker.uuid, speaker.display_name.trim());
      }
    });

    // Update local transcript data with new display names
    const transcriptData = file?.transcript_segments;
    if (transcriptData) {
      const speakerMapping = new Map();
      speakerList.forEach((speaker: any) => {
        if (speaker.display_name && speaker.display_name.trim() !== "" && !speaker.display_name.startsWith('SPEAKER_')) {
          speakerMapping.set(speaker.name, speaker.display_name.trim());
        }
      });

      transcriptData.forEach((segment: any) => {
        const speakerName = segment.speaker_label || segment.speaker?.name;
        const newDisplayName = speakerMapping.get(speakerName);
        if (newDisplayName) {
          segment.resolved_speaker_name = newDisplayName;
          if (segment.speaker) {
            segment.speaker.display_name = newDisplayName;
          } else {
            segment.speaker = {
              id: segment.speaker_id,
              name: speakerName,
              display_name: newDisplayName
            };
          }
        }
      });

      file.transcript_segments = [...transcriptData];
      file = { ...file };
      reactiveFile.set(file);
    }

    // Update subtitles and clear cache (async, don't block)
    if (videoPlayerComponent && videoPlayerComponent.updateSubtitles) {
      videoPlayerComponent.updateSubtitles().catch(error => {
        console.warn('Failed to update subtitles after saving speaker names:', error);
      });
    }

    axiosInstance.delete(`/files/${file.uuid}/cache`).catch(error => {
      console.warn('Could not clear video cache:', error);
    });

    // Refresh speakers from the backend to sync local state
    speakerList.forEach((speaker: any) => {
      if (speaker.uuid && speaker.display_name && speaker.display_name.trim() !== "" && !speaker.display_name.startsWith('SPEAKER_')) {
        transcriptStore.updateSpeakerName(speaker.uuid, speaker.display_name.trim());
      }
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

  function handleTagsUpdated(event: any) {
    if (file) {
      file.tags = event.detail.tags;
      reactiveFile.set(file);
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
      reactiveFile.set(file);
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
    } catch (error: any) {
      console.error('Error generating summary:', error);
      const errorMessage = error.response?.data?.detail || $t('fileDetail.failedToGenerateSummary');

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
    } catch (error: any) {
      console.error('Error loading summary:', error);
      if (error.response?.status !== 404) {
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
    // Only refresh if this event is for the current file
    if (detail?.file_id && String(detail.file_id) === String(fileId)) {
      loadSpeakers();
    }
  }

  onMount(() => {
    // Listen for speaker-updated CustomEvents (gender detection, attribute changes)
    window.addEventListener('speaker-updated', handleSpeakerUpdatedEvent);

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

            // Handle transcription status updates
            if (latestNotification.type === 'transcription_status') {

              // Get status from notification (progressive notifications set it at root level)
              const notificationStatus = latestNotification.status || latestNotification.data?.status;
              const notificationProgress = latestNotification.progress?.percentage || latestNotification.data?.progress;


              // Update progress in real-time for processing updates
              if (notificationStatus === 'processing' && notificationProgress !== undefined) {
                if (file) {
                  file.progress = notificationProgress;
                  file.status = 'processing';
                  // Update the current processing step from the progressive notification
                  currentProcessingStep = latestNotification.currentStep || latestNotification.message || latestNotification.data?.message || $t('fileDetail.processingDefault');
                  file = { ...file }; // Trigger reactivity
                  reactiveFile.set(file);

                }
              } else if (notificationStatus === 'completed' || notificationStatus === 'success' || notificationStatus === 'complete' || notificationStatus === 'finished') {
                // Transcription completed - show completion and refresh
                if (file) {
                  file.progress = 100;
                  file.status = 'completed';
                  currentProcessingStep = $t('fileDetail.processingComplete');

                  // Show AI summary spinner only if LLM is available after transcription completion
                  if (llmAvailable) {
                    summaryGenerating = true;
                    generatingSummary = true;
                    // Keep reprocessing flag true until summary completes to maintain proper UI state
                  } else {
                    // No LLM available, ensure spinners are off and reset reprocessing flag
                    summaryGenerating = false;
                    generatingSummary = false;
                    reprocessing = false;
                  }

                  file = { ...file }; // Trigger reactivity
                  reactiveFile.set(file);
                }

                // Clear processing step and refresh transcript data after completion
                setTimeout(async () => {
                  currentProcessingStep = ''; // Clear processing step

                  // Only refresh the transcript data, not the entire file object to preserve spinner state
                  if (file?.uuid && (file.status === 'completed' || file.status === 'success')) {
                    await fetchTranscriptData();

                    // Refresh subtitles in the video player now that transcript is available
                    if (videoPlayerComponent && videoPlayerComponent.updateSubtitles) {
                      try {
                        await videoPlayerComponent.updateSubtitles();
                      } catch (error) {
                        console.warn('Failed to update subtitles:', error);
                      }
                    }
                  }
                }, 1000);
              } else if (notificationStatus === 'error' || notificationStatus === 'failed') {
                // Error state - refresh immediately
                currentProcessingStep = ''; // Clear processing step
                fetchFileDetails();
              }
            }

            // WebSocket notifications for file updates

            // Handle summarization status updates
            if (latestNotification.type === 'summarization_status') {
              // Only process notifications for the current file
              const notificationFileId = String(latestNotification.data?.file_id || '');
              const currentFileId = String(fileId || '');

              if (notificationFileId !== currentFileId) {
                // Skip notifications for other files
              } else {

              // Get status from notification (progressive notifications set it at root level)
              const status = latestNotification.status || latestNotification.data?.status;


              if (status === 'queued' || status === 'processing' || status === 'generating') {
                // Summary generation started - show spinner only if LLM is available
                if (llmAvailable) {
                  summaryGenerating = true;
                  generatingSummary = true;
                } else {
                  // LLM not available, ensure spinners are off
                  summaryGenerating = false;
                  generatingSummary = false;
                }

              } else if (status === 'completed' || status === 'success' || status === 'complete' || status === 'finished') {
                // Summary completed - stop spinners and update file
                summaryGenerating = false;
                generatingSummary = false;

                // Reset reprocessing flag when summary completes (final step of reprocessing)
                reprocessing = false;

                if (file) {
                  // Update summary-related fields from notification data
                  // Note: The notification contains a brief preview, not the full summary_data
                  const summaryPreview = latestNotification.data?.summary;
                  const summaryId = latestNotification.data?.summary_opensearch_id;

                  // Set a flag to indicate summary exists (full data fetched via API)
                  if (summaryPreview || summaryId) {
                    file.has_summary = true; // Summary now available
                  }
                  if (summaryId) {
                    file.summary_opensearch_id = summaryId;
                  }

                  // Force reactivity update by creating new object reference
                  file = { ...file };
                  reactiveFile.set(file);
                }
              } else if (status === 'failed' || status === 'error') {
                // Summary failed - stop spinners and show error
                summaryGenerating = false;
                generatingSummary = false;

                // Get error message from notification
                const errorMessage = latestNotification.data?.message || latestNotification.message || $t('fileDetail.failedToGenerateSummaryGeneric');
                const isLLMConfigError = errorMessage.toLowerCase().includes('llm service is not available') ||
                                       errorMessage.toLowerCase().includes('configure an llm provider') ||
                                       errorMessage.toLowerCase().includes('llm provider');

                if (!isLLMConfigError) {
                  toastStore.error(errorMessage, 5000);
                }

              }
              } // Close the else block for file ID matching
            }

            // Handle speaker update notifications (for real-time voice suggestion refresh)
            if (latestNotification.type === 'speaker_updated') {
              loadSpeakers();
            }

            // Handle speaker background processing complete notification
            if (latestNotification.type === 'speaker_processing_complete') {
              loadSpeakers();
              // Show toast if labels were auto-applied to other speakers
              const autoAppliedCount = latestNotification.data?.auto_applied_count || 0;
              const suggestedCount = latestNotification.data?.suggested_count || 0;
              if (autoAppliedCount > 0) {
                toastStore.info($t('speakerProfile.autoAppliedToOthers', { count: autoAppliedCount }));
              } else if (suggestedCount > 0) {
                toastStore.info($t('speakerProfile.suggestionsCreated', { count: suggestedCount }));
              }
            }

            // Handle topic extraction status updates (AI suggestions for tags/collections)
            if (latestNotification.type === 'topic_extraction_status') {
              const status = latestNotification.status || latestNotification.data?.status;
              const message = latestNotification.message || latestNotification.data?.message;

              if (status === 'processing') {
                // Update processing step to show what's happening
                currentProcessingStep = message || $t('fileDetail.analyzingTranscript');

              } else if (status === 'completed') {
                // Show completion message briefly before clearing
                currentProcessingStep = message || $t('fileDetail.aiSuggestionsComplete');

                // Reload AI suggestions when extraction completes
                // This will fetch the newly generated suggestions and update the UI dynamically
                loadAISuggestions().catch(err => {
                  console.error('Error reloading AI suggestions after extraction:', err);
                });

                // Clear processing step after a brief delay
                setTimeout(() => {
                  currentProcessingStep = '';
                }, 2000);

              } else if (status === 'failed' || status === 'not_configured') {
                // Clear processing step on failure
                currentProcessingStep = '';
              }
            }

            // Handle content-redaction status (chip + auto-refresh the transcript)
            if (latestNotification.type === 'redaction_status') {
              const rstatus = latestNotification.status || latestNotification.data?.status;
              redactionStatus = rstatus || redactionStatus;
              if (rstatus === 'done') {
                // Detection finished — load the now-available (masked) transcript.
                redactionPending = false;
                fetchFileDetails();
                const skipped = latestNotification.data?.skipped_detectors || [];
                if (skipped.length) {
                  toastStore.info(
                    $t('settings.contentRedaction.someDetectorsSkipped', {
                      detectors: skipped.join(', '),
                    })
                  );
                }
              } else if (rstatus === 'failed') {
                redactionPending = false;
                fetchFileDetails();
              } else {
                redactionPending = true;
              }
            }

            // Handle cache invalidation (e.g., auto-label applied tags/collections)
            if (latestNotification.type === 'cache_invalidate'
                && latestNotification.data?.scope === 'files'
                && latestNotification.data?.file_id === fileId) {
              fetchFileDetails();
              loadAISuggestions();
            }

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

  function scrollToSegmentAtTime(time: number) {
    const transcriptData = file?.transcript_segments;
    if (!transcriptData || !Array.isArray(transcriptData)) return;
    const segment = transcriptData.find((s: any) => time >= s.start_time && time <= s.end_time);
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
      <FileHeader {file} {currentProcessingStep} sharedPermission={myPermission} />

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
          <div class="header-buttons">
            <!-- View Full Transcript Button - LEFT of AI Summary -->
            {#if file && file.transcript_segments && file.transcript_segments.length > 0 && file.status !== 'processing'}
              <button
                class="view-transcript-btn"
                on:click={() => showTranscriptModal = true}
                title={$t('fileDetail.viewTranscript')}
              >
                <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor" class="transcript-icon">
                  <path d="M4 2a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2V4a2 2 0 0 0-2-2H4zm0 1h8a1 1 0 0 1 1 1v8a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1z"/>
                  <path d="M5 5h6v1H5V5zm0 2h6v1H5V7zm0 2h4v1H5V9z"/>
                </svg>
                {$t('fileDetail.transcript')}
              </button>
            {/if}
          <!-- Debug: Summary button state: hasSummary={!!(file?.has_summary || file?.summary_opensearch_id)}, summaryGenerating={summaryGenerating}, generatingSummary={generatingSummary}, fileStatus={file?.status} -->
          {#if file?.has_summary || file?.summary_opensearch_id}
            <button
              class="view-summary-btn"
              on:click={handleShowSummary}
              title={$t('fileDetail.viewSummaryTooltip')}
            >
              <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor" class="ai-icon">
                <path d="M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09zM18.259 8.715L18 9.75l-.259-1.035a3.375 3.375 0 00-2.455-2.456L14.25 6l1.036-.259a3.375 3.375 0 002.455-2.456L18 2.25l.259 1.035a3.375 3.375 0 002.456 2.456L21.75 6l-1.035.259a3.375 3.375 0 00-2.456 2.456zM16.894 20.567L16.5 21.75l-.394-1.183a2.25 2.25 0 00-1.423-1.423L13.5 18.75l1.183-.394a2.25 2.25 0 001.423-1.423L16.5 15.75l.394 1.183a2.25 2.25 0 001.423 1.423L19.5 18.75l-1.183.394a2.25 2.25 0 00-1.423 1.423z"/>
              </svg>
              {$t('fileDetail.summary')}
            </button>
          {:else if summaryGenerating || generatingSummary}
            <!-- Show generating state even when no summary exists yet -->
            <button
              class="generate-summary-btn"
              disabled
              title={$t('fileDetail.aiSummaryGenerating')}
            >
              <Spinner size="small" color="white" />
              <span>{$t('fileDetail.aiSummary')}</span>
            </button>
          {:else if file?.status === 'completed' && canEdit}
            <button
              class="generate-summary-btn"
              on:click={handleGenerateSummary}
              disabled={generatingSummary || summaryGenerating || !llmAvailable}
              title={!llmAvailable ? $t('fileDetail.aiNotAvailable') :
                     (generatingSummary || summaryGenerating) ? $t('fileDetail.aiSummaryGenerating') :
                     $t('fileDetail.generateSummaryTooltip')}
            >
              {#if generatingSummary || summaryGenerating}
                <div class="spinner-small"></div>
                <span>{$t('fileDetail.aiSummary')}</span>
              {:else}
                <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor" class="ai-icon">
                  <path d="M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09zM18.259 8.715L18 9.75l-.259-1.035a3.375 3.375 0 00-2.455-2.456L14.25 6l1.036-.259a3.375 3.375 0 002.455-2.456L18 2.25l.259 1.035a3.375 3.375 0 002.456 2.456L21.75 6l-1.035.259a3.375 3.375 0 00-2.456 2.456zM16.894 20.567L16.5 21.75l-.394-1.183a2.25 2.25 0 00-1.423-1.423L13.5 18.75l1.183-.394a2.25 2.25 0 001.423-1.423L16.5 15.75l.394 1.183a2.25 2.25 0 001.423 1.423L19.5 18.75l-1.183.394a2.25 2.25 0 00-1.423 1.423z"/>
                </svg>
                {$t('fileDetail.generateSummary')}
              {/if}
            </button>
          {/if}
          <!-- Reprocess Button (opens SelectiveReprocessModal) - editors/owners only -->
          {#if canEdit && file && (file.status === 'error' || file.status === 'completed' || file.status === 'failed')}
            <button
              class="reprocess-button-header"
              on:click={() => showReprocessModal = true}
              disabled={reprocessing}
              title={reprocessing ? $t('fileDetail.reprocessingTooltip') : $t('fileDetail.reprocessTooltip')}
            >
              <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <path d="M23 4v6h-6"></path>
                <path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"></path>
              </svg>
              {#if reprocessing}
                <Spinner size="small" />
              {/if}
            </button>
          {/if}
          </div>
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
        />
      </section>

      <!-- Right column: Transcript -->
      {#if redactionPending}
        <section class="transcript-column">
          <div class="redaction-pending">
            <div class="redaction-pending-chip">
              <span class="chip-dot"></span>
              {$t('settings.contentRedaction.processingChip')}
            </div>
            <p class="redaction-pending-msg">{$t('settings.contentRedaction.processingMessage')}</p>
          </div>
        </section>
      {:else if file && file.transcript_segments}
        <section class="transcript-column">
          <TranscriptDisplay
          {file}
          {currentTime}
          {isEditingTranscript}
          {editedTranscript}
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
          on:saveTranscript={handleSaveTranscript}
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
          {#if showRedactionToggle}
            <div class="redaction-footer">
              <span class="redaction-status">
                {showOriginal
                  ? $t('settings.contentRedaction.showingOriginal')
                  : $t('settings.contentRedaction.showingRedacted')}
              </span>
              <div class="redaction-bar-actions">
                {#if canViewOriginal}
                  <button
                    type="button"
                    class="redaction-link-btn"
                    on:click={triggerRedaction}
                    title={$t('settings.contentRedaction.rescanTooltip')}
                  >
                    {$t('settings.contentRedaction.rescan')}
                  </button>
                {/if}
                <button type="button" class="redaction-link-btn" on:click={toggleShowOriginal}>
                  {showOriginal
                    ? $t('settings.contentRedaction.showRedacted')
                    : $t('settings.contentRedaction.showOriginal')}
                </button>
              </div>
            </div>
          {:else if canViewOriginal}
            <div class="redaction-footer">
              <span class="redaction-status">{$t('settings.contentRedaction.notRedacted')}</span>
              <button
                type="button"
                class="redaction-link-btn"
                on:click={triggerRedaction}
                title={$t('settings.contentRedaction.rescanTooltip')}
              >
                {$t('settings.contentRedaction.runRedaction')}
              </button>
            </div>
          {/if}
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
  <!-- svelte-ignore a11y-no-static-element-interactions -->
  <div class="modal-overlay" on:wheel|stopPropagation on:touchmove|stopPropagation>
    <div class="modal-dialog">
      <div class="modal-content">
        <div class="modal-header">
          <h2 class="modal-title">{profileUpdateTitle}</h2>
          <button
            class="modal-close-btn"
            on:click={handleProfileConfirmationCancel}
            aria-label={$t('modal.closeDialog')}
          >
            ×
          </button>
        </div>

        <div class="modal-body">
          <p class="modal-message">{profileUpdateMessage}</p>
        </div>

        <div class="modal-footer">
          <button
            class="btn btn-primary"
            on:click={() => handleProfileConfirmation('update_profile')}
          >
            {$t('speakerProfile.updateGlobally')}
          </button>
          <button
            class="btn btn-secondary"
            on:click={() => handleProfileConfirmation('create_new_profile')}
          >
            {$t('speakerProfile.createNew')}
          </button>
          <button
            class="btn btn-cancel"
            on:click={handleProfileConfirmationCancel}
          >
            {$t('common.cancel')}
          </button>
        </div>
      </div>
    </div>
  </div>
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

  .header-buttons {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    flex-wrap: wrap;
    justify-content: flex-end;
  }

  .view-transcript-btn {
    background-color: var(--bg-primary);
    color: var(--text-primary);
    border: 1px solid var(--border-color);
    border-radius: 8px;
    padding: 0.6rem 1rem;
    font-size: 0.9rem;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s ease;
    display: flex;
    align-items: center;
    gap: 0.5rem;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.1);
    height: 40px;
    white-space: nowrap;
  }

  .view-transcript-btn:hover {
    background-color: var(--hover-bg);
    border-color: var(--primary-color);
    box-shadow: 0 2px 6px rgba(0, 0, 0, 0.15);
  }

  .view-transcript-btn:active {
    transform: scale(0.98);
  }

  .view-transcript-btn .transcript-icon {
    flex-shrink: 0;
    opacity: 0.8;
  }

  .reprocess-button-header {
    background-color: var(--bg-primary);
    color: var(--text-primary);
    border: 1px solid var(--border-color);
    border-radius: 8px;
    padding: 0.6rem;
    cursor: pointer;
    transition: all 0.2s ease;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 0.25rem;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.1);
    width: 40px;
    height: 40px;
  }

  .reprocess-button-header:hover:not(:disabled) {
    background-color: var(--hover-bg);
    border-color: var(--primary-color);
    box-shadow: 0 2px 6px rgba(0, 0, 0, 0.15);
  }

  .reprocess-button-header:active {
    transform: scale(0.98);
  }

  .reprocess-button-header:disabled {
    opacity: 0.6;
    cursor: not-allowed;
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

  .view-summary-btn {
    background-color: var(--bg-primary);
    color: var(--text-primary);
    border: 1px solid var(--border-color);
    border-radius: 8px;
    padding: 0.6rem 1rem;
    font-size: 0.9rem;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s ease;
    display: flex;
    align-items: center;
    gap: 0.5rem;
    box-shadow: 0 1px 3px rgba(0, 0, 0, 0.1);
    height: 40px;
    white-space: nowrap;
  }

  .view-summary-btn:hover {
    background-color: var(--hover-bg);
    border-color: var(--primary-color);
    box-shadow: 0 2px 6px rgba(0, 0, 0, 0.15);
  }

  .view-summary-btn:active {
    transform: scale(0.98);
  }

  .view-summary-btn .ai-icon {
    flex-shrink: 0;
    opacity: 0.8;
  }

  .generate-summary-btn {
    background-color: var(--primary-color, #3b82f6);
    color: white;
    border: none;
    border-radius: 6px;
    padding: 0.5rem 1rem;
    font-size: 0.9rem;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s ease;
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }

  .generate-summary-btn:hover:not(:disabled) {
    background-color: var(--primary-color-dark, #2563eb);
    transform: translateY(-1px);
  }

  .generate-summary-btn:disabled {
    opacity: 0.6;
    cursor: not-allowed;
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

    .header-buttons {
      width: 100%;
      justify-content: flex-start;
    }

    .view-transcript-btn,
    .view-summary-btn,
    .generate-summary-btn {
      font-size: 0.8rem;
      padding: 0.4rem 0.6rem;
      height: 36px;
    }
  }

  @media (max-width: 480px) {
    .view-transcript-btn,
    .view-summary-btn,
    .generate-summary-btn {
      font-size: 0.75rem;
      padding: 0.35rem 0.5rem;
      height: 32px;
      gap: 0.25rem;
    }

    .reprocess-button-header {
      width: 32px;
      height: 32px;
      padding: 0.4rem;
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

  /* Speaker Profile Confirmation Modal */
  .modal-overlay {
    position: fixed;
    top: 0;
    left: 0;
    right: 0;
    bottom: 0;
    background: rgba(0, 0, 0, 0.5);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 1300;
    padding: 1rem;
    overflow: hidden;
    overscroll-behavior: none;
  }

  .modal-dialog {
    background: var(--background-color);
    border: 1px solid var(--border-color);
    border-radius: 12px;
    max-width: 500px;
    width: 100%;
    overflow: hidden;
    box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.1), 0 10px 10px -5px rgba(0, 0, 0, 0.04);
    animation: slideIn 0.2s ease-out;
  }

  @keyframes slideIn {
    from {
      opacity: 0;
      transform: translateY(-20px) scale(0.95);
    }
    to {
      opacity: 1;
      transform: translateY(0) scale(1);
    }
  }

  .modal-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 1.5rem;
    border-bottom: 1px solid var(--border-color);
  }

  .modal-title {
    margin: 0;
    font-size: 1.25rem;
    font-weight: 600;
    color: var(--text-color);
    line-height: 1.4;
  }

  .modal-close-btn {
    background: none;
    border: none;
    cursor: pointer;
    padding: 0.5rem;
    color: var(--text-secondary);
    transition: color 0.2s ease;
    border-radius: 6px;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 1.5rem;
    line-height: 1;
  }

  .modal-close-btn:hover {
    color: var(--text-color);
    background: var(--button-hover);
  }

  .modal-body {
    padding: 1.5rem;
  }

  .modal-message {
    margin: 0;
    color: var(--text-secondary);
    line-height: 1.5;
    font-size: 0.95rem;
  }

  /* Content redaction: transcript show-original toggle bar + export note */
  .redaction-pending {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 0.75rem;
    padding: 3rem 1rem;
    text-align: center;
  }
  .redaction-pending-chip {
    display: inline-flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.4rem 0.9rem;
    border-radius: 999px;
    background: rgba(var(--primary-color-rgb), 0.12);
    color: var(--primary-color);
    font-size: 0.85rem;
    font-weight: 600;
  }
  .redaction-pending-chip .chip-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--primary-color);
    animation: redaction-pulse 1.2s ease-in-out infinite;
  }
  @keyframes redaction-pulse {
    0%, 100% { opacity: 0.4; }
    50% { opacity: 1; }
  }
  .redaction-pending-msg {
    color: var(--text-secondary);
    font-size: 0.85rem;
    max-width: 360px;
  }

  /* Compact redaction controls placed under the transcript (keeps columns top-aligned). */
  .redaction-footer {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.75rem;
    padding: 0.5rem 0.25rem 0;
    margin-top: 0.5rem;
    border-top: 1px solid var(--border-color);
    flex-wrap: wrap;
  }
  .redaction-status {
    font-size: 0.78rem;
    color: var(--text-muted);
  }
  .redaction-bar-actions {
    display: flex;
    gap: 0.9rem;
    align-items: center;
  }
  /* Small, unobtrusive link-style button. Hover shifts to the primary-hover color with
     a subtle tinted background (consistent with the app) — no underline. */
  .redaction-link-btn {
    background: none;
    border: none;
    padding: 0.15rem 0.35rem;
    border-radius: 4px;
    font-size: 0.78rem;
    font-weight: 500;
    color: var(--primary-color);
    cursor: pointer;
    white-space: nowrap;
    transition:
      color 0.12s ease,
      background-color 0.12s ease;
  }
  .redaction-link-btn:hover {
    color: var(--primary-hover);
    background-color: rgba(var(--primary-color-rgb), 0.1);
  }
  .modal-footer {
    display: flex;
    gap: 0.75rem;
    padding: 1rem 1.5rem 1.5rem;
    justify-content: flex-end;
    border-top: 1px solid var(--border-color);
    flex-wrap: wrap;
  }

  .btn {
    padding: 0.6rem 1.2rem;
    border: none;
    border-radius: 10px;
    font-size: 0.95rem;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s ease;
    min-width: 120px;
  }

  .btn-primary {
    background: #3b82f6;
    color: white;
    box-shadow: 0 2px 4px rgba(59, 130, 246, 0.2);
  }

  .btn-primary:hover {
    background: #2563eb;
    transform: translateY(-1px);
    box-shadow: 0 4px 8px rgba(59, 130, 246, 0.25);
  }

  .btn-primary:active {
    transform: translateY(0);
  }

  .btn-secondary {
    background: var(--success-color);
    color: white;
    box-shadow: 0 2px 4px rgba(16, 185, 129, 0.2);
  }

  .btn-secondary:hover {
    background: #059669; /* Darker green to match app pattern */
    transform: translateY(-1px);
    box-shadow: 0 4px 8px rgba(16, 185, 129, 0.3);
  }

  .btn-cancel {
    background: var(--card-background);
    color: var(--text-color);
    border: 1px solid var(--border-color);
    box-shadow: var(--card-shadow);
  }

  .btn-cancel:hover {
    background: var(--button-hover);
    border-color: var(--primary-color);
    transform: translateY(-1px);
  }

  /* Responsive design */
  @media (max-width: 480px) {
    .modal-dialog {
      margin: 1rem;
      max-width: none;
    }

    .modal-footer {
      flex-direction: column-reverse;
    }

    .btn {
      width: 100%;
    }
  }

  /* Dark mode adjustments */
  :global([data-theme='dark']) .modal-overlay {
    background: rgba(0, 0, 0, 0.7);
  }

  :global([data-theme='dark']) .modal-dialog {
    box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.3), 0 10px 10px -5px rgba(0, 0, 0, 0.2);
  }

</style>
