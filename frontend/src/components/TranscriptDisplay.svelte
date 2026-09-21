<script lang="ts">
  import type { Segment, Speaker } from '$lib/types/speaker';
  import { resolveGroupedSegments } from '$lib/transcript/resolveGroupedSegments';
  import { createEventDispatcher } from 'svelte';
  import TranscriptSearch from './TranscriptSearch.svelte';
  import TranscriptSegmentList from './transcript/TranscriptSegmentList.svelte';
  import { type TranscriptSegment, findCurrentSegment } from '$lib/utils/scrollbarCalculations';
  import { toastStore } from '$stores/toast';
  import { type SearchMatch } from '$lib/utils/searchHighlight';
  import { updateSegmentSpeaker } from '$lib/api/transcripts';
  import { patchSegmentInFile } from '$lib/fileDetail/segmentSync';
  import { t } from '$stores/locale';
  import { getErrorMessage } from '$lib/utils/apiError';

  // Issue #748: the Export/Edit-speakers/Download row (`TranscriptActionsBar`) and the
  // speaker editor panel moved into the file-detail page's left column, and the download
  // SSE stream that row triggers moved with them into `$lib/fileDetail/downloadStream.ts`
  // (J5). This component is now just the search bar + the segment list.

  export let file: any = null;
  export let savingTranscript: boolean = false;

  export let editingSegmentId: string | number | null = null;
  export let editingSegmentText: string = '';
  export let speakerList: Speaker[] = [];
  export let currentTime: number = 0;
  export let diarizationDisabled: boolean = false;

  // Pagination props
  export let totalSegments: number = 0;
  export let hasMoreSegments: boolean = false;
  export let loadingMoreSegments: boolean = false;

  const dispatch = createEventDispatcher();

  // Reactive transcript segments (passed to search + segment list children)
  $: transcriptSegments = (file?.transcript_segments || []) as TranscriptSegment[];

  // The backend owns grouping (fat backend, thin frontend); resolution against the flat
  // uuid-keyed segment list (the single representation, #352) is shared with the
  // search-result "view transcript" surface via `$lib/transcript/resolveGroupedSegments`
  // (issue #755) rather than duplicated here.
  $: groupedTranscriptSegments = resolveGroupedSegments(transcriptSegments, file?.grouped_segments);

  // Search functionality state
  let searchMatches: SearchMatch[] = [];
  let currentMatchIndex = -1;
  let searchQuery = '';

  let segmentListComponent: TranscriptSegmentList;

  function handleSegmentClick(startTime: number) {
    dispatch('segmentClick', { startTime });
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

  // Issue #748 §5.3: "Jump to current" scrolls the transcript to the segment under the
  // playhead and ONLY scrolls it — it must never also re-seek the player. The old
  // `ScrollbarIndicator` did both, which silently rewound playback by 0.5s on every click
  // (chased through `seekToPlayhead` -> `+page.svelte`'s `seekToTime`'s padding subtraction).
  // That whole chain is deleted; this handler talks straight to the segment list.
  function handleJumpToPlayhead() {
    const target = findCurrentSegment(currentTime, transcriptSegments);
    if (target?.uuid != null) {
      segmentListComponent?.scrollToCurrentSegment(String(target.uuid));
    }
  }

  // Handle segment speaker change
  let updatingSegments = new Set<string>();

  async function handleSegmentSpeakerChange(event: CustomEvent) {
    const { segmentUuid, speakerUuid, speaker: createdSpeaker } = event.detail;

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

    // Optimistic update - find the new speaker from our speaker list.
    //
    // `createdSpeaker` is the just-created row handed over by the dropdown's
    // "Add speaker" flow. `speakerList` is reloaded asynchronously by the route, so
    // it CANNOT contain a speaker created a millisecond ago — the lookup returned
    // undefined and the segment optimistically rendered "Unknown", which is what
    // made "Add speaker" look like it had done nothing at all (#740).
    const newSpeaker = speakerUuid
      ? (speakerList.find((s: Speaker) => s.uuid === speakerUuid) ?? createdSpeaker ?? null)
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
</script>

<section class="transcript-column">
  <div class="transcript-header">
    <!-- Search component moved to header. Wrapped so its own `width: 100%` resolves
         against this shrinkable slot rather than crowding out the jump button below. -->
    <div class="search-slot">
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
    <!-- Issue #748 §5.3: always visible (not gated behind the collapsed find bar), since
         it replaces the always-visible ScrollbarIndicator minimap. Scrolls ONLY — see
         handleJumpToPlayhead's comment for why it must never re-seek the player. -->
    <button
      type="button"
      class="jump-to-playhead-button"
      on:click={handleJumpToPlayhead}
      disabled={!file?.transcript_segments?.length}
      title={$t('transcriptSearch.jumpToPlayheadTitle')}
      aria-label={$t('transcriptSearch.jumpToPlayheadAriaLabel')}
    >
      <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <circle cx="12" cy="12" r="3"></circle>
        <path d="M12 2v4M12 18v4M4.93 4.93l2.83 2.83M16.24 16.24l2.83 2.83M2 12h4M18 12h4M4.93 19.07l2.83-2.83M16.24 7.76l2.83-2.83" />
      </svg>
    </button>
  </div>

  {#if file.transcript_segments && file.transcript_segments.length > 0}
    <TranscriptSegmentList
      bind:this={segmentListComponent}
      {file}
      {groupedTranscriptSegments}
      {speakerList}
      {diarizationDisabled}
      editable={true}
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
      on:segmentSpeakerChange={handleSegmentSpeakerChange}
      on:speakerCreatedFromDropdown={handleSpeakerCreated}
      on:speakerUpdate
    />
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
    gap: 6px;
    margin-bottom: 6px;
    min-height: 32px;
  }

  .search-slot {
    flex: 1;
    min-width: 0;
  }

  .jump-to-playhead-button {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 32px;
    height: 32px;
    padding: 0;
    background: none;
    border: 1px solid var(--border-color);
    border-radius: 4px;
    color: var(--text-secondary);
    cursor: pointer;
    flex-shrink: 0;
    transition:
      background 0.15s ease,
      color 0.15s ease;
  }

  .jump-to-playhead-button:hover:not(:disabled) {
    background: var(--button-hover, var(--hover-color));
    color: var(--text-color);
  }

  .jump-to-playhead-button:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

</style>
