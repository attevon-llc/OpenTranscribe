<script lang="ts">
  import type { GroupedSegmentView, GroupedTranscriptSegment } from '$lib/types/media';
  import type { Segment, Speaker } from '$lib/types/speaker';
  import { createEventDispatcher, onDestroy } from 'svelte';
  import TranscriptSearch from './TranscriptSearch.svelte';
  import SpeakerEditorPanel from './transcript/SpeakerEditorPanel.svelte';
  import TranscriptActionsBar from './transcript/TranscriptActionsBar.svelte';
  import TranscriptSegmentList from './transcript/TranscriptSegmentList.svelte';
  import { type TranscriptSegment } from '$lib/utils/scrollbarCalculations';
  import { downloadStore } from '$stores/downloads';
  import { toastStore } from '$stores/toast';
  import { type SearchMatch } from '$lib/utils/searchHighlight';
  import { updateSegmentSpeaker } from '$lib/api/transcripts';
  import { patchSegmentInFile } from '$lib/fileDetail/segmentSync';
  import axiosInstance from '$lib/axios';
  import { t } from '$stores/locale';
  import { getErrorMessage } from '$lib/utils/apiError';

  export let file: any = null;
  export let savingTranscript: boolean = false;
  export let savingSpeakers: boolean = false;

  export let speakerNamesChanged: boolean = false; // Track if speaker names have unsaved changes
  export let editingSegmentId: string | number | null = null;
  export let editingSegmentText: string = '';
  export let isEditingSpeakers: boolean = false;
  export let speakerList: Speaker[] = [];
  export let reprocessing: boolean = false;
  export let currentTime: number = 0;
  export let diarizationDisabled: boolean = false;

  // Bridge events from the actions bar to the existing handlers, keeping the
  // download/export logic owned by this component.
  function handleExportFromBar(event: CustomEvent) {
    exportTranscript(event.detail.format);
  }

  function handleDownloadFromBar(event: CustomEvent) {
    downloadMedia(event.detail.mode as DownloadMode);
  }

  // Pagination props
  export let totalSegments: number = 0;
  export let hasMoreSegments: boolean = false;
  export let loadingMoreSegments: boolean = false;

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

  // Reactive transcript segments (passed to search + segment list children)
  $: transcriptSegments = (file?.transcript_segments || []) as TranscriptSegment[];

  // Index the flat segment list once per change, not once per group — resolving each
  // group with a linear scan would be O(n²) on a 500-segment page.
  $: segmentsByUuid = (() => {
    const map = new Map<string, TranscriptSegment>();
    for (const segment of transcriptSegments) {
      if (segment?.uuid != null) map.set(String(segment.uuid), segment);
    }
    return map;
  })();

  // Resolve a backend group's uuid references against the flat segment list, into the
  // camelCase shape the template consumes.
  //
  // `transcript_segments` is the SINGLE representation of segment data. Groups used to
  // embed copies, which gave the page two objects per segment; every optimistic update
  // patched only the flat one, so renames and text edits rendered stale until a full
  // reload (#352). Resolving by uuid here makes that desync impossible.
  //
  // `claimed` enforces the invariant the render layer depends on: a segment belongs to
  // exactly one group. The rows are a keyed each, so the same uuid reaching two groups is
  // a duplicate key — Svelte throws and the whole transcript list fails to render, not
  // just the offending row. Enforcing it here covers every payload source (initial load,
  // refetch, redaction reload, pagination), which a guard on any single path would not.
  function mapBackendGroup(
    group: GroupedTranscriptSegment,
    claimed: Set<string>
  ): GroupedSegmentView {
    const segments: TranscriptSegment[] = [];
    for (const raw of group.segment_uuids || []) {
      const uuid = String(raw);
      if (claimed.has(uuid)) continue;
      // A group can reference segments from a page that hasn't loaded yet; skip those
      // rather than rendering holes.
      const segment = segmentsByUuid.get(uuid);
      if (!segment) continue;
      claimed.add(uuid);
      segments.push(segment);
    }
    return {
      // A run reduced to one member by the checks above is no longer an overlap cluster.
      isOverlapGroup: (group.is_overlap_group ?? false) && segments.length > 1,
      overlapGroupId: group.overlap_group_id ?? undefined,
      startTime: group.start_time,
      endTime: group.end_time,
      segments,
      startSegmentIndex: group.start_segment_index ?? 0,
    };
  }

  // The backend owns grouping (fat backend, thin frontend) and both the detail and the
  // paginated segments endpoints return it. `TranscriptSegmentList` dereferences
  // `group.segments[0]`, so groups that resolved to nothing are dropped.
  $: groupedTranscriptSegments = (() => {
    const claimed = new Set<string>();
    return ((file?.grouped_segments || []) as GroupedTranscriptSegment[])
      .map((group) => mapBackendGroup(group, claimed))
      .filter((group: GroupedSegmentView) => group.segments.length > 0);
  })();

  // Search functionality state
  let searchMatches: SearchMatch[] = [];
  let currentMatchIndex = -1;
  let searchQuery = '';


  function handleSegmentClick(startTime: number) {
    dispatch('segmentClick', { startTime });
  }

  function exportTranscript(format: string) {
    dispatch('exportTranscript', { format });
  }

  function toggleSpeakerEditor() {
    isEditingSpeakers = !isEditingSpeakers;
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
    const existingSegment = file.transcript_segments?.find(
      (s: Segment) => s.uuid === segmentUuid
    );

    if (!existingSegment) {
      toastStore.error($t('transcript.segmentNotFound'));
      updatingSegments.delete(segmentUuid);
      return;
    }

    // Store original speaker for rollback and orphan detection
    const originalSpeaker = existingSegment.speaker;

    // Optimistic update - find the new speaker from our speaker list
    const newSpeaker = speakerUuid
      ? speakerList.find((s: Speaker) => s.uuid === speakerUuid)
      : null;

    // `file` is bound, so these assignments reach the page — which is what makes the
    // grouped view (the thing actually rendered) pick them up.
    file = patchSegmentInFile(file, segmentUuid, {
      speaker: newSpeaker,
      speaker_id: newSpeaker?.uuid ?? null,
      resolved_speaker_name: newSpeaker?.display_name || newSpeaker?.name || null
    });

    try {
      // Make API call
      const updatedSegment = await updateSegmentSpeaker(segmentUuid, speakerUuid);

      // Update with server response
      file = patchSegmentInFile(file, segmentUuid, updatedSegment);

      // Check if the old speaker is now orphaned (no remaining segments)
      // The backend auto-deletes orphaned speakers, so we need to sync the frontend
      const originalSpeakerUuid = originalSpeaker?.uuid;
      if (originalSpeaker && originalSpeakerUuid) {
        const oldSpeakerStillUsed = file.transcript_segments.some(
          (s: Segment) => s.speaker?.uuid === originalSpeakerUuid
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
    } catch (error: unknown) {
      console.error('Error updating segment speaker:', error);

      // Rollback on error
      file = patchSegmentInFile(file, segmentUuid, {
        speaker: originalSpeaker,
        speaker_id: originalSpeaker?.uuid ?? null,
        resolved_speaker_name: existingSegment.resolved_speaker_name ?? null
      });

      toastStore.error(getErrorMessage(error, $t('transcript.failedToUpdateSpeaker')));
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
  // Watchdog timers for the streams above, keyed the same way, so onDestroy can
  // clear a still-pending one rather than letting it fire after unmount.
  const downloadTimeouts = new Map<string, ReturnType<typeof setTimeout>>();
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
    } catch (error: unknown) {
      console.error('Download error:', error);
      downloadStore.updateStatus(
        fileId,
        'error',
        undefined,
        getErrorMessage(error, $t('transcript.downloadFailed'))
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
    downloadTimeouts.set(fileId, timeout);

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
    const timeout = downloadTimeouts.get(fileId);
    if (timeout) {
      clearTimeout(timeout);
      downloadTimeouts.delete(fileId);
    }
  }

  onDestroy(() => {
    downloadStreams.forEach((es) => es.close());
    downloadStreams.clear();
    downloadTimeouts.forEach((timeout) => clearTimeout(timeout));
    downloadTimeouts.clear();
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

<section class="transcript-column">
  <div class="transcript-header">
    <!-- Search component moved to header -->
    <TranscriptSearch
      {transcriptSegments}
      {speakerList}
      fileUuid={file?.uuid ?? ''}
      {hasMoreSegments}
      {loadingMoreSegments}
      disabled={!file?.transcript_segments?.length}
      on:searchResults={handleSearchResults}
      on:navigateToMatch={handleNavigateToMatch}
      on:loadMore
    />
  </div>

  {#if file.transcript_segments && file.transcript_segments.length > 0}
    <TranscriptSegmentList
      {file}
      {groupedTranscriptSegments}
      {transcriptSegments}
      {speakerList}
      {currentTime}
      {diarizationDisabled}
      {editingSegmentId}
      bind:editingSegmentText
      {savingTranscript}
      {searchQuery}
      {searchMatches}
      {currentMatchIndex}
      {totalSegments}
      {hasMoreSegments}
      {loadingMoreSegments}
      on:segmentClick
      on:editSegment
      on:saveSegment
      on:cancelEditSegment
      on:loadMore
      on:seekToPlayhead
      on:segmentSpeakerChange={handleSegmentSpeakerChange}
      on:speakerCreatedFromDropdown={handleSpeakerCreated}
    />


    <TranscriptActionsBar
      {file}
      {diarizationDisabled}
      {isEditingSpeakers}
      {isDownloading}
      {currentDownload}
      {isVideoFile}
      {canEmbedSubtitles}
      on:exportTranscript={handleExportFromBar}
      on:toggleSpeakerEditor={toggleSpeakerEditor}
      on:download={handleDownloadFromBar}
    />

    {#if isEditingSpeakers && !diarizationDisabled}
      <SpeakerEditorPanel
        {file}
        {speakerList}
        {speakerNamesChanged}
        {savingSpeakers}
        on:speakerNameChanged
        on:speakerUpdate
        on:speakersMerged
        on:saveSpeakerNames
        on:segmentClick
        on:loadUpTo
      />
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

</style>
