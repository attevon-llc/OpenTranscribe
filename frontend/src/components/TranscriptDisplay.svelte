<script lang="ts">
  import { createEventDispatcher, onMount, onDestroy } from 'svelte';
  import { slide } from 'svelte/transition';
  import { getSpeakerColor } from '$lib/utils/speakerColors';
  import ScrollbarIndicator from './ScrollbarIndicator.svelte';
  import TranscriptSearch from './TranscriptSearch.svelte';
  import SpeakerMerge from './SpeakerMerge.svelte';
  import SegmentSpeakerDropdown from './SegmentSpeakerDropdown.svelte';
  import { type TranscriptSegment } from '$lib/utils/scrollbarCalculations';
  import { downloadStore } from '$stores/downloads';
  import { toastStore } from '$stores/toast';
  import { highlightTextWithMatches, type SearchMatch } from '$lib/utils/searchHighlight';
  import { sanitizeHighlightHtml } from '$lib/utils/sanitizeHtml';
  import { updateSegmentSpeaker } from '$lib/api/transcripts';
  import axiosInstance from '$lib/axios';
  import { t } from '$stores/locale';
  import { translateSpeakerLabel } from '$lib/i18n';
  import Spinner from './ui/Spinner.svelte';

  // Helper function to translate speaker input placeholder
  function translatePlaceholder(placeholder: string | undefined, speakerName: string): string {
    if (!placeholder) return translateSpeakerLabel(speakerName);

    // Handle "Label SPEAKER_XX" pattern
    if (placeholder.startsWith('Label ')) {
      const label = placeholder.substring(6); // Remove "Label " prefix
      return $t('transcript.labelPlaceholder', { speaker: translateSpeakerLabel(label) });
    }

    // Handle "Suggested: Name" pattern
    if (placeholder.startsWith('Suggested: ')) {
      const name = placeholder.substring(11); // Remove "Suggested: " prefix
      return $t('transcript.suggestedPlaceholder', { name });
    }

    // Return as-is if it's a direct name suggestion
    return placeholder;
  }

  export let file: any = null;
  export let isEditingTranscript: boolean = false;
  export let editedTranscript: string = '';
  export let savingTranscript: boolean = false;
  export let savingSpeakers: boolean = false;

  export let speakerNamesChanged: boolean = false; // Track if speaker names have unsaved changes
  export let editingSegmentId: string | number | null = null;
  export let editingSegmentText: string = '';
  export let isEditingSpeakers: boolean = false;
  export let speakerList: any[] = [];
  export let reprocessing: boolean = false;
  export let currentTime: number = 0;
  export let diarizationDisabled: boolean = false;

  // Export dropdown state (click-based for mobile touch support)
  let showExportDropdown = false;

  function toggleExportDropdown(event: MouseEvent) {
    event.stopPropagation();
    showExportDropdown = !showExportDropdown;
  }

  function closeExportDropdown() {
    showExportDropdown = false;
  }

  let showDownloadDropdown = false;

  function toggleDownloadDropdown(event: MouseEvent) {
    event.stopPropagation();
    showDownloadDropdown = !showDownloadDropdown;
  }

  function closeDownloadDropdown() {
    showDownloadDropdown = false;
  }

  function selectDownload(mode: DownloadMode) {
    closeDownloadDropdown();
    downloadMedia(mode);
  }

  // Create a reactive key based on speakerList to force re-renders of speaker sections
  // This ensures Edit Speakers and Merge Speakers sections update when speakers change
  $: speakerListKey = speakerList.map(s => s.uuid).join('-');

  // Pagination props
  export let totalSegments: number = 0;
  export let hasMoreSegments: boolean = false;
  export let loadingMoreSegments: boolean = false;

  // Infinite scroll sentinel element
  let infiniteScrollSentinel: HTMLElement | null = null;
  let infiniteScrollObserver: IntersectionObserver | null = null;

  // Scroll progress tracking (reading progress bar)
  let scrollProgress: number = 0;
  let transcriptDisplayElement: HTMLElement | null = null;

  // Calculate loaded segments info
  $: loadedSegments = file?.transcript_segments?.length || 0;

  // Handle scroll to update progress bar based on segment index (not scroll position)
  // This ensures progress is relative to total transcript size, not just loaded segments
  function handleTranscriptScroll(event: Event) {
    const target = event.target as HTMLElement;
    if (!target || totalSegments === 0) return;

    // Find all segment elements with data-seg-index attribute
    const segmentElements = target.querySelectorAll('[data-seg-index]');
    if (segmentElements.length === 0) {
      scrollProgress = 0;
      return;
    }

    // Find the first visible segment (at top of viewport)
    const viewportTop = target.scrollTop;
    let firstVisibleSegmentIndex = 0;

    for (const el of Array.from(segmentElements)) {
      const segmentTop = (el as HTMLElement).offsetTop - target.offsetTop;
      if (segmentTop >= viewportTop) {
        const dataIndex = el.getAttribute('data-seg-index');
        firstVisibleSegmentIndex = dataIndex ? parseInt(dataIndex, 10) : 0;
        break;
      }
    }

    // Calculate progress as percentage of total segments
    scrollProgress = Math.round((firstVisibleSegmentIndex / totalSegments) * 100);
  }

  // Set up infinite scroll observer
  onMount(() => {
    if (typeof IntersectionObserver !== 'undefined') {
      infiniteScrollObserver = new IntersectionObserver(
        (entries) => {
          const entry = entries[0];
          if (entry?.isIntersecting && hasMoreSegments && !loadingMoreSegments) {
            dispatch('loadMore');
          }
        },
        { rootMargin: '200px' } // Trigger 200px before reaching the sentinel
      );
    }
  });

  onDestroy(() => {
    if (infiniteScrollObserver) {
      infiniteScrollObserver.disconnect();
      infiniteScrollObserver = null;
    }
  });

  // Observe the sentinel element when it's available
  $: if (infiniteScrollSentinel && infiniteScrollObserver) {
    infiniteScrollObserver.observe(infiniteScrollSentinel);
  }

  // Reference reprocessing to suppress warning (will be tree-shaken in production)
  $: { reprocessing; }

  const dispatch = createEventDispatcher();

  // Download state management
  let downloadState = $downloadStore;
  $: downloadState = $downloadStore;
  $: currentDownload = downloadState[file?.uuid];
  $: isDownloading = currentDownload && ['preparing', 'processing', 'downloading'].includes(currentDownload.status);
  $: isVideoFile = file?.content_type?.startsWith('video/') ?? false;
  $: canEmbedSubtitles = isVideoFile && file?.status === 'completed';

  // Scrollbar indicator state
  let transcriptContainer: HTMLElement | null = null;
  let scrollbarIndicatorEnabled: boolean = true;

  // Reactive transcript segments for scrollbar calculations
  $: transcriptSegments = (file?.transcript_segments || []) as TranscriptSegment[];

  // Interface for grouped segments (including overlap groups)
  interface GroupedSegment {
    isOverlapGroup: boolean;
    overlapGroupId?: string;
    startTime: number;
    endTime: number;
    segments: any[];
    startSegmentIndex: number;  // Track starting index in original array for reading progress
  }

  // Group transcript segments, keeping overlap groups together
  $: groupedTranscriptSegments = (() => {
    const segments = file?.transcript_segments || [];
    if (!segments.length) return [];

    const groups: GroupedSegment[] = [];
    let i = 0;

    while (i < segments.length) {
      const segment = segments[i];

      if (segment.overlap_group_id) {
        // Collect all segments with the same overlap group ID
        const overlapGroupId = segment.overlap_group_id;
        const overlapSegments = [segment];
        let j = i + 1;

        while (j < segments.length && segments[j].overlap_group_id === overlapGroupId) {
          overlapSegments.push(segments[j]);
          j++;
        }

        if (overlapSegments.length > 1) {
          // Create an overlap group
          const groupStartTime = Math.min(...overlapSegments.map(s => s.start_time));
          const groupEndTime = Math.max(...overlapSegments.map(s => s.end_time));

          groups.push({
            isOverlapGroup: true,
            overlapGroupId,
            startTime: groupStartTime,
            endTime: groupEndTime,
            segments: overlapSegments,
            startSegmentIndex: i,  // Track starting index for reading progress
          });

          i = j;
        } else {
          // Single segment with overlap flag, treat as regular
          groups.push({
            isOverlapGroup: false,
            startTime: segment.start_time,
            endTime: segment.end_time,
            segments: [segment],
            startSegmentIndex: i,  // Track starting index for reading progress
          });
          i++;
        }
      } else {
        // Regular segment
        groups.push({
          isOverlapGroup: false,
          startTime: segment.start_time,
          endTime: segment.end_time,
          segments: [segment],
          startSegmentIndex: i,  // Track starting index for reading progress
        });
        i++;
      }
    }

    return groups;
  })();

  // Helper to get original segment index for search highlighting
  function getOriginalSegmentIndex(segment: any): number {
    return file?.transcript_segments?.indexOf(segment) ?? -1;
  }

  // Search functionality state
  let searchMatches: SearchMatch[] = [];
  let currentMatchIndex = -1;
  let searchQuery = '';


  // Handle scrollbar indicator click to seek to playhead
  function handleSeekToPlayhead(event: CustomEvent) {
    const { currentTime: seekTime, targetSegment } = event.detail;

    if (targetSegment) {
      // Scroll to the current segment
      const segmentElement = document.querySelector(`[data-segment-id="${targetSegment.uuid}"]`);
      if (segmentElement) {
        segmentElement.scrollIntoView({
          behavior: 'smooth',
          block: 'center',
          inline: 'nearest'
        });
      }
    }

    // Also dispatch to parent for potential video seeking
    dispatch('seekToPlayhead', { time: seekTime, segment: targetSegment });
  }

  // Check if scrollbar indicator should be enabled
  $: {
    scrollbarIndicatorEnabled = !!(
      transcriptSegments &&
      transcriptSegments.length > 10 && // Only show for transcripts with substantial content
      currentTime >= 0 &&
      !isEditingTranscript // Hide during transcript editing
    );
  }


  function formatSimpleTimestamp(seconds: number): string {
    const minutes = Math.floor(seconds / 60);
    const remainingSeconds = Math.floor(seconds % 60);
    return `${minutes}:${remainingSeconds.toString().padStart(2, '0')}`;
  }

  function handleSegmentClick(startTime: number) {
    dispatch('segmentClick', { startTime });
  }

  function handleSpeakerTimestampClick(startTime: number, segmentUuid: string, segmentIndex: number) {
    // Seek the media player to this timestamp
    dispatch('segmentClick', { startTime });

    // Check if the target segment is already in the DOM
    const segmentElement = document.querySelector(`[data-segment-id="${segmentUuid}"]`);
    if (segmentElement) {
      segmentElement.scrollIntoView({ behavior: 'smooth', block: 'center' });
      // Add highlight flash animation
      segmentElement.classList.add('highlight-flash');
      setTimeout(() => segmentElement.classList.remove('highlight-flash'), 2000);
    } else {
      // Segment not loaded yet - dispatch loadUpTo event for targeted loading
      dispatch('loadUpTo', { targetIndex: segmentIndex, segmentUuid, startTime });
    }
  }

  function editSegment(segment: any) {
    dispatch('editSegment', { segment });
  }

  function saveSegment(segment: any) {
    dispatch('saveSegment', { segment });
  }

  function cancelEditSegment() {
    dispatch('cancelEditSegment');
  }

  function saveTranscript() {
    dispatch('saveTranscript');
  }

  function cancelEditTranscript() {
    isEditingTranscript = false;
  }

  function exportTranscript(format: string) {
    dispatch('exportTranscript', { format });
  }

  function toggleSpeakerEditor() {
    isEditingSpeakers = !isEditingSpeakers;
  }

  function saveSpeakerNames() {
    dispatch('saveSpeakerNames');
  }

  function handleSpeakersMerged() {
    // Dispatch event to parent to refresh speakers and transcript data
    dispatch('speakersMerged');
  }

  // Helper function to check if speaker has cross-video matches to display
  function hasCrossVideoMatches(speaker: any): boolean {
    if (!speaker.cross_video_matches || speaker.cross_video_matches.length === 0) {
      return false;
    }
    // Only labeled speakers have cross-video matches (appears in X videos)
    return !!(speaker.display_name && speaker.display_name.trim() !== '' && !speaker.display_name.startsWith('SPEAKER_'));
  }

  // Search event handlers
  function handleSearchResults(event: CustomEvent) {
    const { matches, currentMatch, query } = event.detail;
    searchMatches = matches;
    currentMatchIndex = currentMatch - 1; // Convert to 0-based index
    searchQuery = query;
  }

  function handleNavigateToMatch(event: CustomEvent) {
    const { match, segment, autoSeek } = event.detail;

    // Only seek if explicitly requested (e.g., user clicks on a segment)
    // Don't auto-seek when just navigating through search results
    if (autoSeek && match.type === 'text') {
      handleSegmentClick(segment.start_time);
    }

    // The scrolling and highlighting is handled by the search component
  }

  // Handle segment speaker change
  let updatingSegments = new Set<string>();

  async function handleSegmentSpeakerChange(event: CustomEvent) {
    const { segmentUuid, speakerUuid } = event.detail;

    // Prevent duplicate requests
    if (updatingSegments.has(segmentUuid)) {
      return;
    }

    updatingSegments.add(segmentUuid);

    // Find the segment in our local state
    const segmentIndex = file.transcript_segments.findIndex(
      (s: any) => s.uuid === segmentUuid
    );

    if (segmentIndex === -1) {
      toastStore.error($t('transcript.segmentNotFound'));
      updatingSegments.delete(segmentUuid);
      return;
    }

    // Store original speaker for rollback and orphan detection
    const originalSpeaker = file.transcript_segments[segmentIndex].speaker;

    // Optimistic update - find the new speaker from our speaker list
    const newSpeaker = speakerUuid
      ? speakerList.find((s: any) => s.uuid === speakerUuid)
      : null;

    file.transcript_segments[segmentIndex].speaker = newSpeaker;
    file.transcript_segments = [...file.transcript_segments]; // Trigger reactivity

    try {
      // Make API call
      const updatedSegment = await updateSegmentSpeaker(segmentUuid, speakerUuid);

      // Update with server response
      file.transcript_segments[segmentIndex] = updatedSegment;
      file.transcript_segments = [...file.transcript_segments]; // Trigger reactivity

      // Check if the old speaker is now orphaned (no remaining segments)
      // The backend auto-deletes orphaned speakers, so we need to sync the frontend
      const originalSpeakerUuid = originalSpeaker?.uuid;
      if (originalSpeaker && originalSpeakerUuid) {
        const oldSpeakerStillUsed = file.transcript_segments.some(
          (s: any) => s.speaker?.uuid === originalSpeakerUuid
        );

        if (!oldSpeakerStillUsed) {
          // Notify parent that a speaker was deleted - parent will update speakerList
          // which flows back down to this component and its children (SpeakerMerge, etc.)
          dispatch('speakerDeleted', { speakerUuid: originalSpeakerUuid });
        }
      }

      // Notify parent to refresh analytics (backend refreshed them, frontend needs to fetch)
      dispatch('analyticsRefreshNeeded');

      toastStore.success($t('transcript.speakerAssignmentUpdated'));
    } catch (error: any) {
      console.error('Error updating segment speaker:', error);

      // Rollback on error
      file.transcript_segments[segmentIndex].speaker = originalSpeaker;
      file.transcript_segments = [...file.transcript_segments]; // Trigger reactivity

      toastStore.error(
        error.response?.data?.detail || $t('transcript.failedToUpdateSpeaker')
      );
    } finally {
      updatingSegments.delete(segmentUuid);
    }
  }

  // Handle new speaker creation from dropdown
  function handleSpeakerCreated(event: CustomEvent) {
    const { speaker } = event.detail;
    if (speaker) {
      // Notify parent to refresh speakers - parent will reload from backend
      // which flows back down to this component and its children
      dispatch('speakerCreated', { speaker });
    }
  }

  // Server-side download modes (mirror backend prepare-download).
  type DownloadMode =
    | 'video_subtitles'
    | 'video_original'
    | 'audio_mp3'
    | 'audio_wav'
    | 'audio_original';

  const DOWNLOAD_TYPE_BY_MODE: Record<DownloadMode, 'video_with_subtitles' | 'original_video' | 'audio'> = {
    video_subtitles: 'video_with_subtitles',
    video_original: 'original_video',
    audio_mp3: 'audio',
    audio_wav: 'audio',
    audio_original: 'audio',
  };

  // Open SSE streams keyed by fileId so we can clean them up on completion/unmount.
  const downloadStreams = new Map<string, EventSource>();
  const DOWNLOAD_STREAM_TIMEOUT_MS = 5 * 60 * 1000;

  async function downloadMedia(mode: DownloadMode) {
    if (!file || !file.uuid) {
      toastStore.error($t('transcript.fileNotAvailable'));
      return;
    }

    const fileId = file.uuid.toString();
    const filename = file.filename;

    if (isDownloading) {
      toastStore.warning($t('transcript.downloadAlreadyProcessing', { filename }));
      return;
    }

    // Track the download (shows the button spinner) before asking the server to
    // prepare it. The API returns either a ready presigned URL (passthrough /
    // cache hit) or queues ffmpeg work; in the latter case the result + URL are
    // pushed to us over an SSE stream (no polling).
    const canStart = downloadStore.startDownload(fileId, filename, DOWNLOAD_TYPE_BY_MODE[mode]);
    if (!canStart) return;

    try {
      downloadStore.updateStatus(fileId, 'processing');
      const { data } = await axiosInstance.post(
        `/files/${fileId}/prepare-download`,
        null,
        { params: { mode } }
      );

      if (data.status === 'ready' && data.url) {
        // Browser streams straight from object storage — never buffered in memory.
        triggerAnchorDownload(data.url, data.filename ?? '');
        downloadStore.updateStatus(fileId, 'completed');
      } else {
        openDownloadStream(fileId, mode);
      }
    } catch (error: any) {
      console.error('Download error:', error);
      const detail = error?.response?.data?.detail;
      downloadStore.updateStatus(
        fileId,
        'error',
        undefined,
        detail || $t('transcript.downloadFailed')
      );
    }
  }

  // Subscribe to the server-pushed download stream. EventSource auto-reconnects on
  // transient drops, and the backend re-checks readiness on each connect, so the
  // file is still delivered even if the connection blips while ffmpeg runs.
  function openDownloadStream(fileId: string, mode: DownloadMode) {
    closeDownloadStream(fileId);
    const es = new EventSource(`/api/files/${fileId}/download-stream?mode=${mode}`);
    downloadStreams.set(fileId, es);

    const timeout = setTimeout(() => {
      closeDownloadStream(fileId);
      downloadStore.updateStatus(fileId, 'error', undefined, $t('transcript.downloadFailed'));
    }, DOWNLOAD_STREAM_TIMEOUT_MS);

    es.addEventListener('progress', (e: MessageEvent) => {
      try {
        const d = JSON.parse(e.data);
        downloadStore.updateStatus(fileId, 'processing', d.progress);
      } catch {
        downloadStore.updateStatus(fileId, 'processing');
      }
    });

    es.addEventListener('ready', (e: MessageEvent) => {
      clearTimeout(timeout);
      closeDownloadStream(fileId);
      try {
        const d = JSON.parse(e.data);
        triggerAnchorDownload(d.url, d.filename ?? '');
        downloadStore.updateStatus(fileId, 'completed');
      } catch {
        downloadStore.updateStatus(fileId, 'error', undefined, $t('transcript.downloadFailed'));
      }
    });

    es.addEventListener('error', (e: MessageEvent) => {
      // A server-sent `error` event carries a real failure (has data); a native
      // transport error has no data and EventSource will auto-reconnect.
      if (e?.data) {
        clearTimeout(timeout);
        closeDownloadStream(fileId);
        let msg = $t('transcript.downloadFailed');
        try { msg = JSON.parse(e.data).message || msg; } catch {}
        downloadStore.updateStatus(fileId, 'error', undefined, msg);
      }
    });
  }

  function closeDownloadStream(fileId: string) {
    const es = downloadStreams.get(fileId);
    if (es) {
      es.close();
      downloadStreams.delete(fileId);
    }
  }

  onDestroy(() => {
    downloadStreams.forEach((es) => es.close());
    downloadStreams.clear();
  });

  function triggerAnchorDownload(href: string, filename: string) {
    const link = document.createElement('a');
    link.href = href;
    if (filename) link.download = filename;
    link.style.display = 'none';
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  }

</script>

<!-- Close dropdowns on outside click -->
<svelte:window on:click={() => { closeExportDropdown(); closeDownloadDropdown(); }} />

<section class="transcript-column">
  <div class="transcript-header">
    <!-- Search component moved to header -->
    <TranscriptSearch
      {transcriptSegments}
      {speakerList}
      disabled={!file?.transcript_segments?.length}
      on:searchResults={handleSearchResults}
      on:navigateToMatch={handleNavigateToMatch}
    />
  </div>

  {#if file.transcript_segments && file.transcript_segments.length > 0}
    {#if isEditingTranscript}
      <textarea bind:value={editedTranscript} rows="20" class="transcript-textarea"></textarea>
      <div class="edit-actions">
        <button
          on:click={saveTranscript}
          disabled={savingTranscript}
          title={$t('transcript.saveChangesTitle')}
        >
          {savingTranscript ? $t('common.saving') : $t('transcript.saveTranscript')}
        </button>
        <button
          class="cancel-button"
          on:click={cancelEditTranscript}
          title={$t('transcript.cancelEditingTitle')}
        >{$t('common.cancel')}</button>
      </div>
    {:else}
      <div bind:this={transcriptContainer} class="transcript-display-container">
        <!-- Reading progress bar at top -->
        {#if totalSegments > 0}
          <div class="reading-progress-bar">
            <div
              class="reading-progress-fill"
              style="width: {scrollProgress}%"
            ></div>
          </div>
        {/if}
        <div
          class="transcript-display"
          bind:this={transcriptDisplayElement}
          on:scroll={handleTranscriptScroll}
        >
        {#each groupedTranscriptSegments as group}
          {#if group.isOverlapGroup}
            <!-- Overlap Group Container -->
            <div class="{diarizationDisabled ? '' : 'overlap-group'}" data-overlap-group-id="{group.overlapGroupId}" data-seg-index={group.startSegmentIndex}>
              {#if !diarizationDisabled}
              <div class="overlap-indicator">
                <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"></path>
                  <circle cx="9" cy="7" r="4"></circle>
                  <path d="M23 21v-2a4 4 0 0 0-3-3.87"></path>
                  <path d="M16 3.13a4 4 0 0 1 0 7.75"></path>
                </svg>
                <span class="overlap-label">{$t('transcript.overlapIndicator', { count: group.segments.length })}</span>
                <span class="overlap-time">{formatSimpleTimestamp(group.startTime)} - {formatSimpleTimestamp(group.endTime)}</span>
              </div>
              <div class="overlap-connector"></div>
              {/if}
              {#each group.segments as segment, segIdx}
                <div
                  class="transcript-segment{diarizationDisabled ? '' : ' in-overlap'}"
                  class:first-in-overlap={!diarizationDisabled && segIdx === 0}
                  class:last-in-overlap={!diarizationDisabled && segIdx === group.segments.length - 1}
                  data-segment-id="{segment.uuid}"
                >
                  {#if editingSegmentId === segment.uuid}
                    <div class="segment-edit-container">
                      <div class="segment-time">{segment.display_timestamp || segment.formatted_timestamp || formatSimpleTimestamp(segment.start_time)}</div>
                      {#if !diarizationDisabled}
                      <div class="segment-speaker">{translateSpeakerLabel(segment.speaker?.display_name || segment.speaker?.name || segment.speaker_label || $t('fileDetail.unknownSpeaker'))}</div>
                      {/if}
                      <div class="segment-edit-input">
                        <textarea bind:value={editingSegmentText} rows="3" class="segment-textarea"></textarea>
                        <div class="segment-edit-actions">
                          <button
                            class="cancel-button"
                            on:click={cancelEditSegment}
                            title={$t('transcript.cancelSegmentTitle')}
                          >{$t('common.cancel')}</button>
                          <button
                            class="save-button"
                            on:click={() => saveSegment(segment)}
                            disabled={savingTranscript}
                            title={$t('transcript.saveSegmentTitle')}
                          >
                            {savingTranscript ? $t('common.saving') : $t('common.save')}
                          </button>
                        </div>
                      </div>
                    </div>
                  {:else}
                    <div class="segment-row">
                      <button
                        class="segment-content{diarizationDisabled ? ' monologue' : ''}"
                        on:click={() => handleSegmentClick(segment.start_time)}
                        on:keydown={(e) => e.key === 'Enter' && handleSegmentClick(segment.start_time)}
                        title={$t('transcript.jumpToSegment')}
                      >
                        <div class="segment-time">{segment.display_timestamp || segment.formatted_timestamp || formatSimpleTimestamp(segment.start_time)}</div>
                        {#if !diarizationDisabled}
                        <div
                          class="segment-speaker-wrapper"
                          role="button"
                          tabindex="0"
                          on:click|stopPropagation
                          on:keydown={(e) => e.key === 'Enter' && e.stopPropagation()}
                        >
                          <SegmentSpeakerDropdown
                            {segment}
                            speakers={speakerList}
                            mediaFileUuid={file?.uuid?.toString() || ''}
                            on:change={handleSegmentSpeakerChange}
                            on:speakerCreated={handleSpeakerCreated}
                          />
                        </div>
                        {/if}
                        <div class="segment-text">
                          {@html sanitizeHighlightHtml(highlightTextWithMatches(
                            segment.text,
                            searchQuery,
                            getOriginalSegmentIndex(segment),
                            searchMatches,
                            currentMatchIndex
                          ))}
                        </div>
                      </button>
                      <button
                        class="edit-button"
                        on:click|stopPropagation={() => editSegment(segment)}
                        title={$t('transcript.editSegment')}
                      >
                        {$t('common.edit')}
                      </button>
                    </div>
                  {/if}
                </div>
              {/each}
            </div>
          {:else}
            <!-- Regular Segment -->
            {@const segment = group.segments[0]}
            <div
              class="transcript-segment"
              data-segment-id="{segment.uuid}"
              data-seg-index={group.startSegmentIndex}
            >
              {#if editingSegmentId === segment.uuid}
                <div class="segment-edit-container">
                  <div class="segment-time">{segment.display_timestamp || segment.formatted_timestamp || formatSimpleTimestamp(segment.start_time)}</div>
                  {#if !diarizationDisabled}
                  <div class="segment-speaker">{translateSpeakerLabel(segment.speaker?.display_name || segment.speaker?.name || segment.speaker_label || $t('fileDetail.unknownSpeaker'))}</div>
                  {/if}
                  <div class="segment-edit-input">
                    <textarea bind:value={editingSegmentText} rows="3" class="segment-textarea"></textarea>
                    <div class="segment-edit-actions">
                      <button
                        class="cancel-button"
                        on:click={cancelEditSegment}
                        title={$t('transcript.cancelSegmentTitle')}
                      >{$t('common.cancel')}</button>
                      <button
                        class="save-button"
                        on:click={() => saveSegment(segment)}
                        disabled={savingTranscript}
                        title={$t('transcript.saveSegmentTitle')}
                      >
                        {savingTranscript ? $t('common.saving') : $t('common.save')}
                      </button>
                    </div>
                  </div>
                </div>
              {:else}
                <div class="segment-row">
                  <button
                    class="segment-content{diarizationDisabled ? ' monologue' : ''}"
                    on:click={() => handleSegmentClick(segment.start_time)}
                    on:keydown={(e) => e.key === 'Enter' && handleSegmentClick(segment.start_time)}
                    title={$t('transcript.jumpToSegment')}
                  >
                    <div class="segment-time">{segment.display_timestamp || segment.formatted_timestamp || formatSimpleTimestamp(segment.start_time)}</div>
                    {#if !diarizationDisabled}
                    <div
                      class="segment-speaker-wrapper"
                      role="button"
                      tabindex="0"
                      on:click|stopPropagation
                      on:keydown={(e) => e.key === 'Enter' && e.stopPropagation()}
                    >
                      <SegmentSpeakerDropdown
                        {segment}
                        speakers={speakerList}
                        mediaFileUuid={file?.uuid?.toString() || ''}
                        on:change={handleSegmentSpeakerChange}
                        on:speakerCreated={handleSpeakerCreated}
                      />
                    </div>
                    {/if}
                    <div class="segment-text">
                      {@html sanitizeHighlightHtml(highlightTextWithMatches(
                        segment.text,
                        searchQuery,
                        getOriginalSegmentIndex(segment),
                        searchMatches,
                        currentMatchIndex
                      ))}
                      {#if segment.confidence !== undefined && segment.confidence !== null && segment.confidence < 0.7}
                        <span class="low-confidence-dot" title={$t('transcript.segmentLowConfidence') + ': ' + Math.round(segment.confidence * 100) + '%'}>●</span>
                      {/if}
                    </div>
                  </button>
                  <button
                    class="edit-button"
                    on:click|stopPropagation={() => editSegment(segment)}
                    title={$t('transcript.editSegment')}
                  >
                    {$t('common.edit')}
                  </button>
                </div>
              {/if}
            </div>
          {/if}
        {/each}

        <!-- Infinite scroll sentinel and loading indicator -->
        {#if hasMoreSegments}
          <div
            class="infinite-scroll-sentinel"
            bind:this={infiniteScrollSentinel}
          >
            {#if loadingMoreSegments}
              <div class="loading-more-indicator">
                <Spinner size="small" />
                <span>{$t('transcript.loadingMoreSegments')}</span>
              </div>
            {/if}
          </div>
        {/if}
        </div>

        <!-- Segments loaded info -->
        {#if totalSegments > 0 && loadedSegments < totalSegments}
          <div class="segments-loaded-info">
            <span class="segments-count">{$t('transcript.segmentsLoaded', { loaded: loadedSegments, total: totalSegments })}</span>
            {#if loadingMoreSegments}
              <span class="loading-indicator">
                <Spinner size="small" />
                {$t('common.loading')}
              </span>
            {/if}
          </div>
        {/if}

        <!-- Scrollbar Position Indicator - Inside transcript-display-container for proper positioning -->
        {#if scrollbarIndicatorEnabled}
          <ScrollbarIndicator
            {currentTime}
            {transcriptSegments}
            containerElement={transcriptContainer?.querySelector('.transcript-display')}
            disabled={isEditingTranscript || !file?.transcript_segments?.length}
            on:seekToPlayhead={handleSeekToPlayhead}
          />
        {/if}
      </div>


      <div class="transcript-actions">
        <div class="export-dropdown" class:open={showExportDropdown}>
          <button
            class="export-transcript-button"
            title={$t('transcript.exportTitle')}
            on:click={toggleExportDropdown}
          >
            <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
              <polyline points="7 10 12 15 17 10"></polyline>
              <line x1="12" y1="15" x2="12" y2="3"></line>
            </svg>
            {$t('transcript.export')}
            <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <rect x="4" y="4" width="16" height="16" rx="2" ry="2"></rect>
              <line x1="9" y1="9" x2="15" y2="9"></line>
              <line x1="9" y1="13" x2="15" y2="13"></line>
              <line x1="9" y1="17" x2="11" y2="17"></line>
            </svg>
          </button>
          {#if showExportDropdown}
            <!-- svelte-ignore a11y-click-events-have-key-events -->
            <!-- svelte-ignore a11y-no-static-element-interactions -->
            <div class="export-dropdown-content" on:click|stopPropagation>
              <button
                on:click={() => { exportTranscript('txt'); closeExportDropdown(); }}
                title={$t('transcript.exportTextTitle')}
              >{$t('transcript.exportText')}</button>
              <button
                on:click={() => { exportTranscript('json'); closeExportDropdown(); }}
                title={$t('transcript.exportJsonTitle')}
              >{$t('transcript.exportJson')}</button>
              <button
                on:click={() => { exportTranscript('csv'); closeExportDropdown(); }}
                title={$t('transcript.exportCsvTitle')}
              >{$t('transcript.exportCsv')}</button>
              <button
                on:click={() => { exportTranscript('srt'); closeExportDropdown(); }}
                title={$t('transcript.exportSrtTitle')}
              >{$t('transcript.exportSrt')}</button>
              <button
                on:click={() => { exportTranscript('vtt'); closeExportDropdown(); }}
                title={$t('transcript.exportVttTitle')}
              >{$t('transcript.exportVtt')}</button>
            </div>
          {/if}
        </div>

        {#if !diarizationDisabled}
        <button
          class="edit-speakers-button"
          on:click={toggleSpeakerEditor}
          title={$t('transcript.editSpeakersTitle')}
        >
          <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M12 20h9"></path>
            <path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"></path>
          </svg>
          {isEditingSpeakers ? $t('transcript.hideSpeakerEditor') : $t('transcript.editSpeakers')}
          <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"></path>
            <circle cx="12" cy="7" r="4"></circle>
          </svg>
        </button>
        {/if}

        {#if file && file.download_url}
          <div class="export-dropdown download-dropdown" class:open={showDownloadDropdown}>
            <button
              class="action-button download-button"
              class:downloading={isDownloading}
              class:processing={currentDownload?.status === 'processing'}
              disabled={isDownloading}
              on:click={toggleDownloadDropdown}
              title={isDownloading ? $t('transcript.processing') : $t('transcript.download')}
            >
              {#if isDownloading}
                <svg class="spinner" xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M21 12a9 9 0 11-6.219-8.56"/>
                </svg>
                {currentDownload?.status === 'preparing' ? $t('transcript.preparing') : $t('transcript.processing')}
              {:else}
                <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
                  <polyline points="7 10 12 15 17 10"></polyline>
                  <line x1="12" y1="15" x2="12" y2="3"></line>
                </svg>
                {$t('transcript.download')}
                <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <polyline points="6 9 12 15 18 9"></polyline>
                </svg>
              {/if}
            </button>
            {#if showDownloadDropdown && !isDownloading}
              <!-- svelte-ignore a11y-click-events-have-key-events -->
              <!-- svelte-ignore a11y-no-static-element-interactions -->
              <div class="export-dropdown-content" on:click|stopPropagation>
                {#if canEmbedSubtitles}
                  <button on:click={() => selectDownload('video_subtitles')}>{$t('transcript.downloadVideoSubtitles')}</button>
                {/if}
                {#if isVideoFile}
                  <button on:click={() => selectDownload('video_original')}>{$t('transcript.downloadOriginalVideo')}</button>
                {/if}
                <div class="download-dropdown-label">{$t('transcript.downloadAudio')}</div>
                <button on:click={() => selectDownload('audio_mp3')}>{$t('transcript.downloadAudioMp3')}</button>
                <button on:click={() => selectDownload('audio_wav')}>{$t('transcript.downloadAudioWav')}</button>
                <button on:click={() => selectDownload('audio_original')}>{$t('transcript.downloadAudioOriginal')}</button>
              </div>
            {/if}
          </div>
        {/if}

      </div>

      {#if isEditingSpeakers && !diarizationDisabled}
        <div class="speaker-editor-container" transition:slide={{ duration: 200 }}>
          <div class="speaker-editor-header">
            <h4>
              {$t('transcript.editSpeakerNames')}
              {#if speakerNamesChanged}
                <span class="unsaved-indicator" title={$t('transcript.unsavedChanges')}>•</span>
              {/if}
            </h4>

            <!-- Confidence Legend - Compact Info Icon -->
            <div class="legend-info-container">
              <span class="legend-title">{$t('transcript.colorLegend')}</span>
              <div class="legend-info-wrapper">
                <button class="legend-info-icon" title={$t('transcript.clickToSeeConfidenceColors')}>
                  <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <circle cx="12" cy="12" r="10"></circle>
                    <line x1="12" y1="16" x2="12" y2="12"></line>
                    <line x1="12" y1="8" x2="12.01" y2="8"></line>
                  </svg>
                </button>
                <div class="legend-tooltip">
                  <div class="legend-item">
                    <span class="legend-color" style="background-color: var(--success-color);"></span>
                    {$t('transcript.highConfidence')}
                  </div>
                  <div class="legend-item">
                    <span class="legend-color" style="background-color: var(--warning-color);"></span>
                    {$t('transcript.mediumConfidence')}
                  </div>
                  <div class="legend-item">
                    <span class="legend-color" style="background-color: var(--error-color);"></span>
                    {$t('transcript.lowConfidence')}
                  </div>
                </div>
              </div>
            </div>
          </div>

          <!-- Speaker Merge UI and Edit Speakers - keyed by speakerListKey to force re-render when speakers change -->
          {#key speakerListKey}
          {#if speakerList && speakerList.length > 1}
            <SpeakerMerge
              speakers={speakerList}
              transcriptSegments={file?.transcript_segments || []}
              on:merged={handleSpeakersMerged}
            />
          {/if}

          {#if speakerList && speakerList.length > 0}
            <div class="speaker-list">
              {#each speakerList as speaker}
                <div class="speaker-item">
                  <div class="speaker-header">
                    <span
                      class="speaker-original"
                      style="background-color: {getSpeakerColor(speaker.name).bg}; border-color: {getSpeakerColor(speaker.name).border}; --speaker-light: {getSpeakerColor(speaker.name).textLight}; --speaker-dark: {getSpeakerColor(speaker.name).textDark};"
                    >
                      {translateSpeakerLabel(speaker.name)}
                    </span>
                    {#if speaker.predicted_gender && speaker.predicted_gender !== 'unknown'}
                      <span
                        class="gender-badge"
                        title="AI predicted gender: {speaker.predicted_gender} ({Math.round((speaker.attribute_confidence?.gender ?? 0) * 100)}% confidence)"
                      >
                        {#if speaker.predicted_gender === 'male'}
                          <svg class="gender-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="10" cy="14" r="7"/><line x1="15" y1="9" x2="21" y2="3"/><polyline points="15 3 21 3 21 9"/></svg>
                          Male
                        {:else}
                          <svg class="gender-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="9" r="7"/><line x1="12" y1="16" x2="12" y2="23"/><line x1="9" y1="20" x2="15" y2="20"/></svg>
                          Female
                        {/if}
                      </span>
                    {/if}
                    <div class="speaker-input-wrapper">
                    <input
                      type="text"
                      bind:value={speaker.display_name}
                      placeholder={translatePlaceholder(speaker.input_placeholder, speaker.name)}
                      title={$t('transcript.enterSpeakerName', { speaker: translateSpeakerLabel(speaker.name) })}
                      class:suggested-high={speaker.is_high_confidence}
                      class:suggested-medium={speaker.is_medium_confidence}
                      data-speaker-id={speaker.uuid}
                      on:input={() => {
                        // Dispatch event to notify parent of speaker name change
                        dispatch('speakerNameChanged', { speakerId: speaker.uuid, newName: speaker.display_name });
                      }}
                      on:focus={() => {
                        if (speaker.is_high_confidence && speaker.suggested_name) {
                          speaker.display_name = speaker.suggested_name;
                          // Dispatch event after auto-fill
                          dispatch('speakerNameChanged', { speakerId: speaker.uuid, newName: speaker.display_name });
                        }
                      }}
                    />
                    {#if speaker.show_profile_badge}
                      <div class="speaker-profile-badge" title={$t('transcript.speakerHasProfile')}>
                        <svg class="profile-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                          <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"></path>
                          <circle cx="12" cy="7" r="4"></circle>
                        </svg>
                        <span class="profile-text">{$t('transcript.profile')}</span>
                      </div>
                    {/if}
                    </div>
                  </div>

                  <!-- Speaker Timestamp Links -->
                  {#if speaker.segment_timestamps && speaker.segment_timestamps.length > 0}
                    <div class="speaker-timestamps">
                      {#each speaker.segment_timestamps as ts}
                        <button
                          class="timestamp-link"
                          title={$t('transcript.jumpToTimestampTitle', { time: formatSimpleTimestamp(ts.start_time) })}
                          on:click={() => handleSpeakerTimestampClick(ts.start_time, ts.uuid, ts.segment_index)}
                        >
                          {formatSimpleTimestamp(ts.start_time)}
                        </button>
                      {/each}
                      {#if speaker.segment_count > speaker.segment_timestamps.length}
                        <span class="timestamp-more">
                          {speaker.segment_count - speaker.segment_timestamps.length === 1
                            ? $t('transcript.moreSegmentsSingular')
                            : $t('transcript.moreSegments', { count: speaker.segment_count - speaker.segment_timestamps.length })}
                        </span>
                      {/if}
                    </div>
                  {/if}

                  <div class="speaker-content-below">
                    <!-- Unified Suggestions Section -->
                    {#if speaker.show_suggestions_section}
                      <div class="suggestions-section">
                        <button
                          class="suggestions-toggle"
                          on:click={() => speaker.showSuggestions = !speaker.showSuggestions}
                          title={$t('transcript.viewAvailableSuggestions')}
                        >
                          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class:rotated={speaker.showSuggestions}>
                            <polyline points="6 9 12 15 18 9"></polyline>
                          </svg>
                          {speaker.total_suggestions !== 1 ? $t('transcript.suggestionsCountPlural', { count: speaker.total_suggestions }) : $t('transcript.suggestionsCount', { count: speaker.total_suggestions })}
                          {#if !speaker.display_name}
                            <span class="expand-hint">{$t('transcript.clickToExpand')}</span>
                          {/if}
                        </button>

                        {#if speaker.showSuggestions}
                          <div class="suggestions-dropdown" transition:slide={{ duration: 200 }}>
                            <!-- Horizontal chip layout -->
                            <div class="suggestion-chips-container">
                              {#if speaker.metadata_hints && speaker.metadata_hints.length > 0}
                                <div class="chip-row">
                                  <span class="chip-label metadata-label">{$t('transcript.metadataHints')}</span>
                                  <div class="chips-wrap">
                                    {#each speaker.metadata_hints.slice(0, 4) as hint}
                                      <button
                                        class="suggestion-chip metadata-chip"
                                        on:click={() => {
                                          speaker.display_name = hint.name;
                                          dispatch('speakerNameChanged', { speakerId: speaker.uuid, newName: hint.name });
                                        }}
                                        title="{$t('transcript.metadataHintTooltip')} ({hint.source})"
                                      >
                                        <svg class="source-icon" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                                          <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path>
                                          <polyline points="14 2 14 8 20 8"></polyline>
                                        </svg>
                                        {hint.name}
                                        {#if hint.role && hint.role !== 'unknown'}
                                          <span class="chip-role">{hint.role}</span>
                                        {/if}
                                        <span class="chip-confidence">{Math.round(hint.confidence * 100)}%</span>
                                      </button>
                                    {/each}
                                  </div>
                                </div>
                              {/if}

                              {#if speaker.has_llm_suggestion}
                                <div class="chip-row">
                                  <span class="chip-label">{$t('transcript.aiSuggestion')}</span>
                                  <button
                                    class="suggestion-chip llm-chip"
                                    class:high-confidence={speaker.confidence >= 0.75}
                                    class:medium-confidence={speaker.confidence >= 0.5 && speaker.confidence < 0.75}
                                    class:low-confidence={speaker.confidence < 0.5}
                                    on:click={() => { speaker.display_name = speaker.suggested_name; }}
                                    title={speaker.suggestion_source === 'llm_analysis' ? $t('transcript.aiSuggestedBasedOnContent') : speaker.suggestion_source === 'profile_embedding' ? $t('transcript.aiSuggestedBasedOnProfile') : $t('transcript.aiSuggestedBasedOnSimilarity')}
                                  >
                                    {#if speaker.suggestion_source === 'llm_analysis'}
                                      <svg class="source-icon" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                                        <path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/>
                                        <path d="M19 10v2a7 7 0 0 1-14 0v-2"/>
                                        <line x1="12" y1="19" x2="12" y2="23"/>
                                        <line x1="8" y1="23" x2="16" y2="23"/>
                                      </svg>
                                    {:else if speaker.suggestion_source === 'profile_embedding'}
                                      <svg class="source-icon" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                                        <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/>
                                        <circle cx="9" cy="7" r="4"/>
                                        <path d="M23 21v-2a4 4 0 0 0-3-3.87"/>
                                        <path d="M16 3.13a4 4 0 0 1 0 7.75"/>
                                      </svg>
                                    {:else}
                                      <svg class="source-icon" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                                        <path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/>
                                        <path d="M19 10v2a7 7 0 0 1-14 0v-2"/>
                                        <line x1="12" y1="19" x2="12" y2="23"/>
                                        <line x1="8" y1="23" x2="16" y2="23"/>
                                      </svg>
                                    {/if}
                                    {#if speaker.gender_alignment === 'match'}
                                      <span class="alignment-dot match" title="Gender matches metadata hint"></span>
                                    {:else if speaker.gender_alignment === 'mismatch'}
                                      <span class="alignment-dot mismatch" title="Gender conflicts with metadata"></span>
                                    {/if}
                                    {speaker.suggested_name}
                                    <span class="chip-confidence">{Math.round(speaker.confidence * 100)}%</span>
                                  </button>
                                </div>
                              {/if}

                              {#if speaker.profile_suggestions && speaker.profile_suggestions.length > 0}
                                <div class="chip-row">
                                  <span class="chip-label profile-label">{$t('transcript.profileMatch')}</span>
                                  <div class="chips-wrap">
                                    {#each speaker.profile_suggestions.slice(0, 4) as suggestion}
                                      <button
                                        class="suggestion-chip profile-chip"
                                        class:high-confidence={suggestion.confidence >= 0.75}
                                        class:medium-confidence={suggestion.confidence >= 0.5 && suggestion.confidence < 0.75}
                                        class:low-confidence={suggestion.confidence < 0.5}
                                        on:click={() => {
                                          speaker.display_name = suggestion.name;
                                          dispatch('speakerUpdate', { speakerId: speaker.uuid, newName: suggestion.name });
                                        }}
                                        title="{suggestion.reason}"
                                      >
                                        <svg class="source-icon" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                                          <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/>
                                          <circle cx="12" cy="7" r="4"/>
                                        </svg>
                                        {suggestion.name}
                                        <span class="chip-confidence">{suggestion.confidence_percentage}</span>
                                      </button>
                                    {/each}
                                  </div>
                                </div>
                              {/if}
                            </div>
                          </div>
                        {/if}
                      </div>
                    {/if}

                    <!-- Cross-video speaker detection - Below text input -->
                    {#if hasCrossVideoMatches(speaker)}
                      <div class="cross-video-compact">
                        <div class="compact-header" role="button" tabindex="0" on:click={() => speaker.showMatches = !speaker.showMatches} on:keydown={(e) => e.key === 'Enter' && (speaker.showMatches = !speaker.showMatches)}>
                          <span class="compact-text">
                            {speaker.cross_video_matches.length !== 1 ? $t('transcript.speakerAppearsInVideosPlural', { name: speaker.display_name, count: speaker.cross_video_matches.length }) : $t('transcript.speakerAppearsInVideos', { name: speaker.display_name, count: speaker.cross_video_matches.length })}
                          </span>
                          <div class="compact-controls">
                            <button
                              class="info-btn-consistent"
                              title={$t('transcript.clickForDetails')}
                              on:click|stopPropagation={() => speaker.showMatches = !speaker.showMatches}
                            >
                              <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                                <circle cx="12" cy="12" r="10"></circle>
                                <line x1="12" y1="16" x2="12" y2="12"></line>
                                <line x1="12" y1="8" x2="12.01" y2="8"></line>
                              </svg>
                            </button>
                            <button
                              class="dropdown-arrow"
                              title={$t('transcript.showHideMatches')}
                              on:click|stopPropagation={() => speaker.showMatches = !speaker.showMatches}
                            >
                              <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" class:rotated={speaker.showMatches}>
                                <polyline points="6 9 12 15 18 9"></polyline>
                              </svg>
                            </button>
                          </div>
                        </div>

                        {#if speaker.showMatches}
                          <div class="compact-dropdown" transition:slide={{ duration: 200 }}>
                            {#if speaker.cross_video_matches && speaker.cross_video_matches.length > 0}
                              {@const visibleMatches = speaker.cross_video_matches.slice(0, 3)}
                              {@const remainingMatches = speaker.cross_video_matches.slice(3, 8)}
                              {@const remainingCount = speaker.cross_video_matches.length - 3}

                              <div class="matches-help">
                                {$t('transcript.filesWhereAppears', { name: speaker.display_name })}
                              </div>
                              <div class="compact-matches">
                                <div class="matches-scroll-container">
                                {#each visibleMatches as match}
                                  <div class="compact-match" title={match.title || match.media_file_title || $t('transcript.unknownVideo')}>
                                    <span class="match-text">{((match.title || match.media_file_title || $t('transcript.unknownVideo')).length > 35 ? (match.title || match.media_file_title || $t('transcript.unknownVideo')).substring(0, 35) + '...' : (match.title || match.media_file_title || $t('transcript.unknownVideo')))}</span>
                                    <span class="match-confidence">
                                      {#if match.same_speaker}
                                        {$t('transcript.currentVideo')}
                                      {:else if match.confidence}
                                        ✓ {Math.round(match.confidence * 100)}%
                                      {:else}
                                        {$t('transcript.profileMatch')}
                                      {/if}
                                    </span>
                                  </div>
                                {/each}
                              </div>

                                {#if remainingCount > 0}
                                  <div class="more-matches-compact hover-container">
                                    <span class="more-matches-text">{$t('transcript.moreMatches', { count: remainingCount })}</span>
                                    <div class="hover-popup">
                                      {#each remainingMatches as match}
                                        <div class="popup-match">
                                          <span class="popup-match-text">{match.title || match.media_file_title || $t('transcript.unknownVideo')}</span>
                                          <span class="popup-match-confidence">
                                            {#if match.same_speaker}
                                              {$t('transcript.currentVideo')}
                                            {:else if match.confidence}
                                              ✓ {Math.round(match.confidence * 100)}%
                                            {:else}
                                              {$t('transcript.profileMatch')}
                                            {/if}
                                          </span>
                                        </div>
                                      {/each}
                                    </div>
                                  </div>
                                {/if}
                              </div>
                            {/if}
                          </div>
                        {/if}
                      </div>
                    {/if}
                  </div>
                </div>
              {/each}
              <button
                class="save-speakers-button"
                on:click={saveSpeakerNames}
                disabled={savingSpeakers || !speakerNamesChanged}
                title={speakerNamesChanged ? $t('transcript.saveSpeakerNamesTitle') : $t('transcript.noChangesToSave')}
              >
                {#if savingSpeakers}
                  <svg class="spinner" xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <path d="M21 12a9 9 0 11-6.219-8.56"/>
                  </svg>
                  {$t('common.saving')}
                {:else}
                  {$t('transcript.saveSpeakerNames')}
                {/if}
              </button>
            </div>
          {:else}
            <p>{$t('transcript.noSpeakersFound')}</p>
          {/if}
          {/key}
        </div>
      {/if}
    {/if}
  {:else if file.status === 'completed'}
    <p>{$t('transcript.noTranscriptAvailable')}</p>
  {:else if file.status === 'processing'}
    <p>{$t('transcript.transcriptGenerating')}</p>
  {:else}
    <p>{$t('transcript.transcriptNotAvailable')}</p>
  {/if}
</section>

<style>
  /* Reading progress bar - horizontal bar at top showing scroll position */
  .reading-progress-bar {
    position: sticky;
    top: 0;
    left: 0;
    right: 0;
    height: 3px;
    background: var(--border-color);
    z-index: 10;
    border-radius: 0;
  }

  .reading-progress-fill {
    height: 100%;
    background: #3b82f6;
    transition: width 0.1s ease-out;
    border-radius: 0;
  }

  /* Infinite scroll styles */
  .infinite-scroll-sentinel {
    min-height: 1px;
    padding: 8px 0;
  }

  .loading-more-indicator {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 8px;
    padding: 16px;
    color: var(--text-muted);
    font-size: 14px;
  }

  /* Segments loaded info bar */
  .segments-loaded-info {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 12px;
    padding: 8px 16px;
    background: var(--surface-secondary);
    border-top: 1px solid var(--border-color);
    font-size: 13px;
    color: var(--text-muted);
  }

  .segments-count {
    font-weight: 500;
  }

  .loading-indicator {
    display: flex;
    align-items: center;
    gap: 6px;
  }

  .transcript-column {
    flex: 1;
    min-width: 0;
    position: relative; /* Enable positioning for external indicator */
  }

  .transcript-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 6px;
    min-height: 32px;
  }

  .transcript-column h4 {
    margin: 0;
    font-size: 18px;
    font-weight: 600;
    color: var(--text-primary);
  }


  .transcript-textarea {
    width: 100%;
    padding: 16px;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    background: var(--surface-color);
    color: var(--text-primary);
    font-family: monospace;
    font-size: 14px;
    line-height: 1.5;
    resize: vertical;
    min-height: 400px;
  }

  .edit-actions {
    display: flex;
    gap: 12px;
    margin-top: 12px;
  }

  .edit-actions button {
    padding: 8px 16px;
    border-radius: 6px;
    font-size: 14px;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s ease;
  }

  .edit-actions button:first-child {
    background: #3b82f6;
    color: white;
    border: none;
  }

  .edit-actions button:first-child:hover:not(:disabled) {
    background: #2563eb;
  }

  .edit-actions button:first-child:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .cancel-button {
    background: #6b7280;
    color: white;
    border: 1px solid #6b7280;
    padding: 0.6rem 1.2rem;
    border-radius: 10px;
    cursor: pointer;
    font-weight: 500;
    transition: all 0.2s ease;
    font-size: 0.95rem;
    box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1);
  }

  .cancel-button:hover {
    background: #4b5563;
    border-color: #4b5563;
    transform: scale(1.02);
    box-shadow: 0 4px 8px rgba(75, 85, 99, 0.25);
  }

  .cancel-button:active {
    transform: scale(1);
    box-shadow: 0 2px 4px rgba(75, 85, 99, 0.2);
  }

  .transcript-display-container {
    position: relative;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    background: var(--surface-color);
  }

  .transcript-display {
    max-height: 600px;
    overflow-y: auto;
    overflow-x: hidden;
    position: relative;
  }

  .transcript-segment {
    border-bottom: 1px solid var(--border-light);
  }

  .transcript-segment:last-child {
    border-bottom: none;
  }

  .segment-row {
    display: flex;
    align-items: stretch;
    min-width: 0; /* Allow flex container to shrink */
    width: 100%;
  }

  .segment-content {
    flex: 1 1 0; /* Allow shrinking below content size */
    display: grid;
    grid-template-columns: auto auto minmax(0, 1fr); /* minmax(0, 1fr) allows column to shrink */
    gap: 12px;
    align-items: start;
    padding: 8px 12px;
    background: none;
    border: none;
    text-align: left;
    cursor: pointer;
    transition: all 0.2s ease;
    border-radius: 4px;
    margin: 2px 4px;
    min-width: 0; /* Allow grid to shrink */
    max-width: 100%;
    overflow: hidden;
  }

  .segment-content.monologue {
    grid-template-columns: auto minmax(0, 1fr);
  }

  .segment-speaker-wrapper {
    display: flex;
    align-items: center;
    min-width: fit-content;
  }

  .segment-content:hover {
    background: rgba(59, 130, 246, 0.08);
  }

  .segment-time {
    font-size: 12px;
    font-weight: 600;
    color: var(--primary-color);
    font-family: monospace;
    white-space: nowrap;
    min-width: fit-content;
  }

  .segment-speaker {
    font-size: 12px;
    font-weight: 700;
    padding: 2px 8px;
    border-radius: 12px;
    white-space: nowrap;
    min-width: fit-content;
    max-width: 150px;
    overflow: hidden;
    text-overflow: ellipsis;
    border: 1px solid;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    transition: all 0.2s ease;
    color: var(--speaker-light);
  }

  /* Dark mode speaker colors */
  :global([data-theme='dark']) .segment-speaker {
    color: var(--speaker-dark);
  }

  .segment-text {
    font-size: 14px;
    color: var(--text-primary);
    line-height: 1.4;
    position: relative;
    padding-left: 12px;
    word-wrap: break-word;
    overflow-wrap: break-word;
    word-break: break-word;
    min-width: 0; /* Allow text to shrink in grid layout */
  }

  .segment-text::before {
    content: '';
    position: absolute;
    left: 0;
    top: 2px;
    width: 2px;
    height: 20px;
    background: rgba(107, 114, 128, 0.3);
    border-radius: 1px;
  }

  .low-confidence-dot {
    color: var(--warning-color, #f59e0b);
    font-size: 0.6rem;
    vertical-align: super;
    margin-left: 2px;
    cursor: help;
  }

  .edit-button {
    padding: 8px 12px;
    background: none;
    border: none;
    color: #3b82f6;
    cursor: pointer;
    font-size: 12px;
    font-weight: 400;
    transition: all 0.2s ease;
    text-decoration: underline;
    text-decoration-color: transparent;
  }

  .edit-button:hover {
    color: #1d4ed8;
    text-decoration-color: #1d4ed8;
  }

  .segment-edit-container {
    padding: 16px;
    background: var(--background-secondary);
    border-left: 3px solid var(--primary-color);
  }

  .segment-edit-input {
    margin-top: 8px;
  }

  .segment-textarea {
    width: 100%;
    padding: 8px 12px;
    border: 1px solid var(--border-color);
    border-radius: 4px;
    background: var(--surface-color);
    color: var(--text-primary);
    font-size: 14px;
    line-height: 1.4;
    resize: vertical;
  }

  .segment-edit-actions {
    display: flex;
    justify-content: flex-end;
    gap: 0.5rem;
    margin-top: 8px;
  }

  .segment-edit-actions .save-button,
  .segment-edit-actions .cancel-button {
    padding: 0.4rem 0.8rem;
    border-radius: 10px;
    font-size: 0.85rem;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s ease;
    border: none;
    min-width: 60px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
  }

  .segment-edit-actions .save-button {
    background: #3b82f6;
    color: white;
    box-shadow: 0 2px 4px rgba(59, 130, 246, 0.2);
  }

  .segment-edit-actions .save-button:hover:not(:disabled) {
    background: #2563eb;
    transform: scale(1.02);
    box-shadow: 0 4px 8px rgba(59, 130, 246, 0.25);
  }

  .segment-edit-actions .save-button:active:not(:disabled) {
    transform: scale(1);
  }

  .segment-edit-actions .save-button:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }

  .segment-edit-actions .cancel-button {
    background: #6b7280;
    color: white;
    border: 1px solid #6b7280;
    box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1);
  }

  .segment-edit-actions .cancel-button:hover {
    background: #4b5563;
    border-color: #4b5563;
    transform: scale(1.02);
    box-shadow: 0 4px 8px rgba(75, 85, 99, 0.25);
  }

  .segment-edit-actions .cancel-button:active {
    transform: scale(1);
    box-shadow: 0 2px 4px rgba(75, 85, 99, 0.2);
  }

  .transcript-actions {
    display: flex;
    gap: 12px;
    margin-top: 16px;
    flex-wrap: wrap;
  }

  .export-dropdown {
    position: relative;
    display: inline-block;
  }

  .export-transcript-button,
  .edit-speakers-button,
  .action-button {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 10px 16px;
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: 6px;
    color: var(--text-primary);
    text-decoration: none;
    font-size: 14px;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s ease;
  }

  .export-transcript-button:hover,
  .edit-speakers-button:hover,
  .action-button:hover {
    background: var(--surface-hover);
    border-color: var(--border-hover);
  }

  /* Unsaved changes indicator (yellow dot) - matches AI Prompts modal */
  .unsaved-indicator {
    color: #f59e0b; /* Amber/yellow warning color */
    font-size: 1.2em;
    margin-left: 0.5rem;
    animation: pulse 2s infinite;
  }

  @keyframes pulse {
    0%, 100% {
      opacity: 1;
    }
    50% {
      opacity: 0.5;
    }
  }

  .export-dropdown-content {
    position: absolute;
    top: 100%;
    left: 0;
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: 6px;
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.1);
    z-index: 100;
    min-width: 200px;
  }

  .export-dropdown-content button {
    display: block;
    width: 100%;
    padding: 10px 16px;
    background: none;
    border: none;
    text-align: left;
    color: var(--text-primary);
    font-size: 14px;
    cursor: pointer;
    transition: background-color 0.2s ease;
  }

  .export-dropdown-content button:hover {
    background: var(--surface-hover);
  }

  .export-dropdown-content button:first-child {
    border-radius: 6px 6px 0 0;
  }

  .export-dropdown-content button:last-child {
    border-radius: 0 0 6px 6px;
  }

  /* The Download dropdown is the rightmost action — align it to the right edge. */
  .download-dropdown .export-dropdown-content {
    left: auto;
    right: 0;
  }

  .download-dropdown-label {
    padding: 8px 16px 4px;
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--text-secondary);
    border-top: 1px solid var(--border-color);
  }

  .speaker-editor-container {
    margin-top: 20px;
    padding: 20px;
    background: var(--background-secondary);
    border-radius: 8px;
    border: 1px solid var(--border-color);
  }

  .speaker-editor-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 1rem;
  }

  .legend-info-container {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    font-size: 0.9rem;
  }

  .legend-title {
    font-weight: 600;
    color: var(--text-primary);
  }

  .legend-info-wrapper {
    position: relative;
    display: inline-block;
  }

  .legend-info-icon {
    background: none;
    border: none;
    color: var(--primary-color);
    cursor: pointer;
    padding: 2px;
    border-radius: 50%;
    transition: all 0.2s ease;
  }

  .legend-info-icon:hover {
    background-color: var(--surface-hover);
  }

  .legend-info-wrapper:hover .legend-tooltip {
    display: block;
  }

  .legend-tooltip {
    display: none;
    position: absolute;
    top: 100%;
    left: 50%;
    transform: translateX(-50%);
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: 6px;
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
    z-index: 1000;
    padding: 0.75rem;
    min-width: 200px;
    margin-top: 4px;
  }

  .legend-item {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    color: var(--text-color-secondary);
    margin-bottom: 0.5rem;
    font-size: 0.8rem;
  }

  .legend-item:last-child {
    margin-bottom: 0;
  }

  .legend-color {
    width: 12px;
    height: 12px;
    border-radius: 3px;
    display: inline-block;
  }

  .speaker-editor-header h4 {
    margin: 0;
    font-size: 16px;
    font-weight: 600;
    color: var(--text-primary);
  }

  .speaker-list {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }

  .speaker-item {
    padding: 16px;
    background: var(--surface-color);
    border-radius: 8px;
    border: 1px solid var(--border-color);
    margin-bottom: 4px;
    display: flex;
    flex-direction: column;
  }

  .speaker-header {
    display: flex;
    align-items: center;
    gap: 12px;
    margin-bottom: 12px;
  }

  .speaker-timestamps {
    display: flex;
    align-items: center;
    gap: 6px;
    margin-bottom: 8px;
    flex-wrap: wrap;
  }

  .timestamp-link {
    font-family: 'SF Mono', 'Fira Code', 'Fira Mono', 'Roboto Mono', monospace;
    font-size: 0.7rem;
    font-weight: 500;
    padding: 2px 8px;
    border-radius: 10px;
    border: 1px solid var(--border-color);
    background: var(--surface-color);
    color: var(--primary-color);
    cursor: pointer;
    transition: all 0.15s ease;
    line-height: 1.4;
  }

  .timestamp-link:hover {
    background: #3b82f6;
    color: white;
    border-color: var(--primary-color);
    transform: scale(1.02);
    box-shadow: 0 2px 4px rgba(0, 0, 0, 0.1);
  }

  .timestamp-more {
    font-size: 0.65rem;
    color: var(--text-secondary-color);
    font-style: italic;
    padding-top: 1px;
  }

  :global(.highlight-flash) {
    animation: highlightFlash 2s ease-out;
  }

  @keyframes highlightFlash {
    0% { background-color: rgba(59, 130, 246, 0.3); }
    100% { background-color: transparent; }
  }

  .speaker-content-below {
    margin-left: 0;
    padding-left: 0;
  }

  /* Matches .segment-speaker styling from transcript segments */
  .speaker-original {
    font-size: 12px;
    font-weight: 700;
    padding: 2px 8px;
    border-radius: 12px;
    white-space: nowrap;
    min-width: fit-content;
    max-width: 150px;
    overflow: hidden;
    text-overflow: ellipsis;
    border: 1px solid;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    transition: all 0.2s ease;
    color: var(--speaker-light);
    flex-shrink: 0;
  }

  /* Dark mode speaker-original colors */
  :global([data-theme='dark']) .speaker-original {
    color: var(--speaker-dark);
  }

  .speaker-input-wrapper {
    flex: 1;
    position: relative;
    min-width: 0; /* Allow flex shrinking */
    display: flex;
    align-items: center;
    gap: 0.75rem;
  }

  .speaker-item input {
    flex: 1;
    padding: 8px 12px;
    border: 1px solid var(--border-color);
    border-radius: 4px;
    background: var(--surface-color);
    color: var(--text-primary);
    font-size: 14px;
  }


  .speaker-item input.suggested-high {
    border-color: var(--success-color);
    border-width: 2px;
  }

  .speaker-item input.suggested-medium {
    border-color: var(--warning-color);
    border-width: 2px;
  }


  /* Embedding Suggestion Interface */

  .gender-badge {
    display: inline-flex;
    align-items: center;
    gap: 3px;
    font-size: 0.65rem;
    font-weight: 500;
    padding: 0.15rem 0.4rem;
    border-radius: 999px;
    flex-shrink: 0;
    cursor: default;
    white-space: nowrap;
    background: rgba(128, 128, 128, 0.1);
    color: var(--text-secondary, #777);
    border: 1px solid rgba(128, 128, 128, 0.25);
  }

  .gender-svg {
    width: 12px;
    height: 12px;
    flex-shrink: 0;
  }

  :global([data-theme='dark']) .gender-badge {
    background: rgba(255, 255, 255, 0.07);
    color: var(--text-secondary, #888);
    border-color: rgba(255, 255, 255, 0.12);
  }

  .speaker-profile-badge {
    background: #f59e0b;
    color: white;
    font-size: 0.6875rem;
    font-weight: 600;
    padding: 0.25rem 0.5rem;
    border-radius: 0.375rem;
    display: inline-flex;
    align-items: center;
    gap: 0.375rem;
    white-space: nowrap;
    box-shadow: 0 1px 2px rgba(0, 0, 0, 0.1);
    flex-shrink: 0; /* Don't shrink the badge */
  }

  .profile-icon {
    width: 14px;
    height: 14px;
    stroke-width: 2.5;
  }

  .profile-text {
    line-height: 1;
    font-weight: 600;
  }




  .match-confidence {
    font-size: 0.75rem;
    font-weight: normal;
  }

  .save-speakers-button {
    margin-top: 16px;
    padding: 0.6rem 1.2rem;
    background: #3b82f6;
    color: white;
    border: none;
    border-radius: 10px;
    font-size: 0.95rem;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s ease;
    box-shadow: 0 2px 4px rgba(59, 130, 246, 0.2);
  }

  .save-speakers-button:hover {
    background: #2563eb;
    transform: scale(1.02);
    box-shadow: 0 4px 8px rgba(59, 130, 246, 0.25);
  }

  .save-speakers-button:active {
    transform: scale(1);
  }

  .save-speakers-button:disabled {
    background: #94a3b8;
    cursor: not-allowed;
    transform: none;
    box-shadow: none;
  }

  .save-speakers-button:disabled:hover {
    background: #94a3b8;
    transform: none;
    box-shadow: none;
  }

  .save-speakers-button .spinner {
    margin-right: 0.5rem;
  }

  @media (max-width: 768px) {
    /* Stack: [time | speaker] on row 1, [text spanning full width] on row 2 */
    .segment-content {
      grid-template-columns: auto 1fr;
      grid-template-rows: auto auto;
      gap: 4px 8px;
      padding: 8px;
    }

    .segment-content.monologue {
      grid-template-columns: auto 1fr;
    }

    /* Time stays in column 1 */
    .segment-time {
      font-size: 11px;
      grid-column: 1;
      grid-row: 1;
    }

    /* Speaker stays in column 2 */
    .segment-speaker-wrapper {
      grid-column: 2;
      grid-row: 1;
    }

    .segment-speaker {
      font-size: 10px;
      padding: 1px 6px;
      white-space: normal;
      max-width: none;
      text-align: center;
      line-height: 1.3;
    }

    /* Text spans full width on row 2 */
    .segment-text {
      grid-column: 1 / -1;
      grid-row: 2;
      padding-left: 0;
      font-size: 13px;
    }

    .segment-text::before {
      display: none;
    }

    .transcript-actions {
      flex-direction: column;
    }

    .speaker-item {
      flex-direction: column;
      align-items: stretch;
    }

    .speaker-header {
      flex-wrap: wrap;
      gap: 6px;
    }

    .speaker-original {
      font-size: 11px;
      padding: 2px 6px;
      min-width: auto;
    }

    .gender-badge {
      font-size: 0.6rem;
    }

    .speaker-input-wrapper {
      flex: 1 1 100%;
      width: 100%;
    }

    .speaker-profile-badge {
      font-size: 0.6rem;
      padding: 0.15rem 0.35rem;
    }
  }

  /* Enhanced download button styles */
  .download-button:disabled {
    opacity: 0.7;
    cursor: not-allowed;
  }

  .download-button.downloading {
    background: #3b82f6;
    color: white;
    border-color: var(--primary-color);
  }

  .download-button.processing {
    background: var(--warning-color, #f59e0b);
    color: white;
    border-color: var(--warning-color, #f59e0b);
  }

  .spinner {
    animation: spin 1s linear infinite;
  }

  @keyframes spin {
    from {
      transform: rotate(0deg);
    }
    to {
      transform: rotate(360deg);
    }
  }

  /* Compact cross-video UI styles */
  .cross-video-compact {
    margin-top: 0.3rem;
    padding: 0.5rem;
    background-color: var(--background-main);
    border: 1px solid var(--border-color);
    border-radius: 4px;
    font-size: 0.75rem;
  }

  .compact-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    cursor: pointer;
    user-select: none;
  }

  .compact-text {
    color: var(--text-color);
    flex: 1;
    font-size: 0.7rem;
  }

  .compact-controls {
    display: flex;
    align-items: center;
    gap: 0.25rem;
  }

  .info-btn-consistent, .dropdown-arrow {
    background: none;
    border: none;
    cursor: pointer;
    padding: 0.15rem;
    border-radius: 3px;
    color: var(--text-color-secondary);
    transition: background-color 0.2s ease;
    display: flex;
    align-items: center;
    justify-content: center;
  }

  .info-btn-consistent:hover, .dropdown-arrow:hover {
    background-color: var(--border-color-soft);
  }

  .dropdown-arrow svg {
    transition: transform 0.2s ease;
  }

  .dropdown-arrow svg.rotated {
    transform: rotate(180deg);
  }

  .compact-dropdown {
    margin-top: 0.5rem;
    padding-top: 0.5rem;
    border-top: 1px solid var(--border-color-soft);
  }

  .matches-help {
    font-size: 0.65rem;
    color: var(--text-color-secondary);
    margin-bottom: 0.3rem;
    font-style: italic;
  }

  .compact-matches {
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
  }

  .compact-match {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0.15rem 0.35rem;
    background-color: var(--background-alt);
    border: 1px solid var(--border-color-soft);
    border-radius: 3px;
    font-size: 0.65rem;
    cursor: help;
    transition: background-color 0.2s ease;
  }

  .compact-match:hover {
    background-color: var(--background-main);
    border-color: var(--border-color);
  }

  .match-text {
    flex: 1;
    color: var(--text-color);
  }

  .match-confidence {
    font-weight: 500;
    font-size: 0.65rem;
    color: var(--success-color);
  }

  .more-matches-compact {
    padding: 0.2rem 0.4rem;
    text-align: center;
    font-size: 0.65rem;
    color: var(--text-color-secondary);
    font-style: italic;
  }

  .hover-container {
    position: relative;
    display: inline-block;
  }

  .more-matches-text {
    cursor: pointer;
    color: var(--primary-color);
    font-weight: 500;
  }

  .more-matches-text:hover {
    text-decoration: underline;
  }

  .hover-popup {
    display: none;
    position: absolute;
    top: 100%;
    left: 50%;
    transform: translateX(-50%);
    background: var(--card-background);
    border: 1px solid var(--border-color);
    border-radius: 8px;
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.15);
    z-index: 1000;
    min-width: 250px;
    max-width: 350px;
    padding: 0.5rem;
    margin-top: 0.25rem;
  }

  .hover-container:hover .hover-popup {
    display: block;
  }

  .popup-match {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 0.3rem 0.5rem;
    border-radius: 4px;
    margin-bottom: 0.2rem;
    background: var(--surface-color);
  }

  .popup-match:last-child {
    margin-bottom: 0;
  }

  .popup-match-text {
    flex: 1;
    font-size: 0.75rem;
    color: var(--text-color);
    margin-right: 0.5rem;
  }

  .popup-match-confidence {
    font-size: 0.65rem;
    font-weight: 500;
    color: var(--success-color);
    white-space: nowrap;
  }

  /* Scrollable container for large match sets */
  .matches-scroll-container {
    max-height: 200px;
    overflow-y: auto;
    border: 1px solid var(--border-color-soft);
    border-radius: 3px;
    padding: 0.2rem;
  }

  .matches-scroll-container::-webkit-scrollbar {
    width: 6px;
  }

  .matches-scroll-container::-webkit-scrollbar-track {
    background: var(--background-alt);
    border-radius: 3px;
  }

  .matches-scroll-container::-webkit-scrollbar-thumb {
    background: var(--border-color);
    border-radius: 3px;
  }

  .matches-scroll-container::-webkit-scrollbar-thumb:hover {
    background: var(--text-color-secondary);
  }


  .suggestions-toggle {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    padding: 0.3rem 0.5rem;
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: 4px;
    color: var(--text-secondary-color);
    font-size: 0.75rem;
    cursor: pointer;
    transition: all 0.2s ease;
    width: 100%;
    text-align: left;
  }

  .suggestions-toggle:hover {
    background: var(--surface-hover);
    color: var(--text-primary);
  }

  .suggestions-toggle svg {
    transition: transform 0.2s ease;
    flex-shrink: 0;
  }

  .suggestions-toggle svg.rotated {
    transform: rotate(180deg);
  }

  .suggestions-dropdown {
    margin-top: 0.3rem;
    border: 1px solid var(--border-color);
    border-radius: 4px;
    background: var(--surface-color);
    padding: 0.4rem;
  }


  /* Unified Suggestions Section */
  .suggestions-section {
    margin-top: 0.4rem;
  }

  .expand-hint {
    font-size: 0.65rem;
    color: var(--text-secondary-color);
    font-style: italic;
    margin-left: 0.3rem;
  }

  /* Chip-based layout */
  .suggestion-chips-container {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }

  .chip-row {
    display: flex;
    align-items: flex-start;
    gap: 0.5rem;
  }

  .chip-label {
    font-size: 0.7rem;
    font-weight: 600;
    color: var(--text-secondary-color);
    min-width: 35px;
    padding-top: 0.25rem;
    flex-shrink: 0;
  }

  .chips-wrap {
    display: flex;
    flex-wrap: wrap;
    gap: 0.3rem;
    align-items: center;
    flex: 1;
  }

  .suggestion-chip {
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    padding: 0.25rem 0.5rem;
    border: none;
    border-radius: 16px;
    font-size: 0.7rem;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s ease;
    color: white;
    white-space: nowrap;
  }

  .suggestion-chip:hover {
    transform: scale(1.02);
    box-shadow: 0 2px 4px rgba(0, 0, 0, 0.15);
  }

  .suggestion-chip.llm-chip {
    background: #3b82f6;
  }

  .suggestion-chip.llm-chip:hover {
    background: #2563eb;
  }


  /* Profile chip — consistent #f59e0b amber */
  .suggestion-chip.profile-chip.high-confidence,
  .suggestion-chip.profile-chip.medium-confidence,
  .suggestion-chip.profile-chip.low-confidence {
    background: #f59e0b;
  }

  .suggestion-chip.profile-chip.high-confidence:hover,
  .suggestion-chip.profile-chip.medium-confidence:hover,
  .suggestion-chip.profile-chip.low-confidence:hover {
    background: #d97706;
  }

  .chip-label.profile-label {
    color: #f59e0b;
    font-weight: 600;
  }

  .chip-confidence {
    font-size: 0.65rem;
    background: rgba(255, 255, 255, 0.25);
    padding: 0.1rem 0.3rem;
    border-radius: 8px;
    font-weight: 600;
  }

  .source-icon {
    width: 12px;
    height: 12px;
    margin-right: 0.25rem;
    opacity: 0.9;
    stroke: white;
    fill: none;
  }

  .metadata-chip {
    background: rgba(100, 149, 237, 0.1);
    color: var(--text-color, #e0e0e0);
    border: 1px solid rgba(100, 149, 237, 0.3);
  }

  .metadata-chip:hover {
    background: rgba(100, 149, 237, 0.2);
  }

  .metadata-label {
    color: var(--text-secondary, #888);
  }

  .chip-role {
    font-size: 0.65rem;
    opacity: 0.7;
    text-transform: capitalize;
    font-style: italic;
  }

  .alignment-dot {
    display: inline-block;
    width: 6px;
    height: 6px;
    border-radius: 50%;
    flex-shrink: 0;
  }

  .alignment-dot.match {
    background: var(--success-color, #51cf66);
  }

  .alignment-dot.mismatch {
    background: var(--warning-color, #ffc107);
  }


  .spinner {
    animation: rotate 2s linear infinite;
    width: 20px;
    height: 20px;
  }

  @keyframes rotate {
    100% {
      transform: rotate(360deg);
    }
  }

  /* Overlap Group Styles */
  .overlap-group {
    position: relative;
    margin: 8px 0;
    padding-left: 12px;
    border-left: 3px solid var(--primary-color);
    background: linear-gradient(90deg, rgba(59, 130, 246, 0.05) 0%, transparent 100%);
    border-radius: 0 8px 8px 0;
  }

  .overlap-indicator {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 8px 12px;
    background: rgba(59, 130, 246, 0.1);
    border-radius: 0 6px 0 0;
    font-size: 12px;
    color: var(--primary-color);
    font-weight: 600;
  }

  .overlap-indicator svg {
    color: var(--primary-color);
    opacity: 0.8;
  }

  .overlap-label {
    flex: 1;
  }

  .overlap-time {
    font-family: monospace;
    font-size: 11px;
    color: var(--text-secondary);
    font-weight: 500;
  }

  .overlap-connector {
    position: absolute;
    left: 6px;
    top: 40px;
    bottom: 8px;
    width: 2px;
    background: #3b82f6;
    opacity: 0.3;
    border-radius: 1px;
  }

  .transcript-segment.in-overlap {
    margin-left: 4px;
    border-left: none;
  }

  .transcript-segment.in-overlap .segment-content {
    background: transparent;
  }

  .transcript-segment.in-overlap:hover .segment-content {
    background: rgba(59, 130, 246, 0.1);
  }

  .transcript-segment.first-in-overlap {
    margin-top: 4px;
  }

  .transcript-segment.last-in-overlap {
    margin-bottom: 4px;
  }

  /* Dark mode adjustments for overlap groups */
  :global([data-theme='dark']) .overlap-group {
    background: linear-gradient(90deg, rgba(59, 130, 246, 0.08) 0%, transparent 100%);
  }

  :global([data-theme='dark']) .overlap-indicator {
    background: rgba(59, 130, 246, 0.15);
  }
</style>
