<script lang="ts">
  import InfoIcon from '$components/ui/InfoIcon.svelte';
  import type { TranscriptSourceFile } from '$lib/types/media';
  import type { CrossVideoMatch, SpeakerEditorEntry } from '$lib/types/speaker';
  import { resolve } from '$app/paths';
  import MenuIcon from '$components/ui/MenuIcon.svelte';
  import { createEventDispatcher, onDestroy, onMount } from 'svelte';
  import { slide } from 'svelte/transition';
  import { getSpeakerColor } from '$lib/utils/speakerColors';
  import { formatClock } from '$lib/utils/formatting';
  import SpeakerMerge from '$components/SpeakerMerge.svelte';
  import SpeakerPrivacyNotice from '$components/privacy/SpeakerPrivacyNotice.svelte';
  import SpeakerIdentityHistory from '$components/speakers/SpeakerIdentityHistory.svelte';
  import FileVoiceprintConsent from '$components/privacy/FileVoiceprintConsent.svelte';
  import { user } from '$stores/auth';
  import { t } from '$stores/locale';
  import Spinner from '$components/ui/Spinner.svelte';
  import { translateSpeakerLabel } from '$lib/i18n';
  import { RENAME_PROPAGATION_TIMEOUT_MS } from '$lib/api/speakerClusters';
  import { isPlaceholderSpeakerName } from '$lib/utils/speakerNames';

  export let file: TranscriptSourceFile | null = null;
  export let speakerList: SpeakerEditorEntry[] = [];
  export let speakerNamesChanged: boolean = false;
  export let savingSpeakers: boolean = false;

  const dispatch = createEventDispatcher();

  // Rename-propagation progress. This panel is normally a thin
  // presentational child — no API calls, no stores-as-state (see this folder's
  // CLAUDE.md) — but the digest-regeneration a rename triggers server-side
  // finishes on its own schedule with no request/response the coordinator can
  // hand down as a prop, only a WebSocket completion event
  // (`speaker_rename_propagation` -> `speaker-rename-propagation` window
  // event, `$stores/websocket`). Listening for it here, scoped to "did I just
  // save", is the narrow exception rather than threading a new prop through
  // TranscriptDisplay for a purely informational status line.
  let propagationPending = false;
  let propagationTimer: ReturnType<typeof setTimeout> | undefined;
  let wasSaving = savingSpeakers;

  // The transition check and the bookkeeping assignment must share ONE
  // reactive block: a separate `$: wasSaving = savingSpeakers` block would be
  // ordered by Svelte's dependency sort to run BEFORE this one (this block
  // reads `wasSaving`), so the "previous" value would already have been
  // overwritten with the current one by the time the comparison ran.
  function trackSaveCompletion(saving: boolean) {
    if (wasSaving && !saving) {
      // The save request just completed — the backend has committed the
      // rename and queued digest regeneration in the background by now.
      propagationPending = true;
      clearTimeout(propagationTimer);
      propagationTimer = setTimeout(() => {
        propagationPending = false;
      }, RENAME_PROPAGATION_TIMEOUT_MS);
    }
    wasSaving = saving;
  }

  $: trackSaveCompletion(savingSpeakers);

  function handleRenamePropagation(): void {
    propagationPending = false;
    clearTimeout(propagationTimer);
  }

  onMount(() => {
    window.addEventListener('speaker-rename-propagation', handleRenamePropagation);
  });

  onDestroy(() => {
    window.removeEventListener('speaker-rename-propagation', handleRenamePropagation);
    clearTimeout(propagationTimer);
  });

  // Create a reactive key based on speakerList to force re-renders of speaker sections
  // This ensures Edit Speakers and Merge Speakers sections update when speakers change
  $: showsGender = speakerList.some((s) => s.predicted_gender && s.predicted_gender !== 'unknown');
  $: speakerListKey = speakerList.map(s => s.uuid).join('-');

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


  function handleSpeakerTimestampClick(startTime: number, segmentUuid: string | undefined, segmentIndex: number | undefined) {
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

  function saveSpeakerNames() {
    dispatch('saveSpeakerNames');
  }

  function handleSpeakersMerged() {
    // Dispatch event to parent to refresh speakers and transcript data
    dispatch('speakersMerged');
  }

  // Helper function to check if speaker has cross-video matches to display
  function hasCrossVideoMatches(
    speaker: SpeakerEditorEntry
  ): speaker is SpeakerEditorEntry & { cross_video_matches: CrossVideoMatch[] } {
    if (!speaker.cross_video_matches || speaker.cross_video_matches.length === 0) {
      return false;
    }
    // Only labeled speakers have cross-video matches (appears in X videos).
    // Uses the shared placeholder contract rather than a local
    // `.startsWith('SPEAKER_')`, which also swallowed a genuinely named speaker
    // such as "SPEAKER_OF_THE_HOUSE" — see `$lib/utils/speakerNames`.
    return !isPlaceholderSpeakerName(speaker.display_name);
  }
</script>

<div class="speaker-editor-container" transition:slide={{ duration: 200 }}>
  <div class="speaker-editor-header">
    <h4>
      {$t('transcript.editSpeakerNames')}
      {#if speakerNamesChanged}
        <span class="unsaved-indicator" role="img" aria-label={$t('transcript.unsavedChanges')} title={$t('transcript.unsavedChanges')}><MenuIcon name="dot" size={10} /></span>
      {/if}
    </h4>

    <!-- Confidence Legend - Compact Info Icon -->
    <div class="legend-info-container">
      <span class="legend-title">{$t('transcript.colorLegend')}</span>
      <div class="legend-info-wrapper">
        <button class="legend-info-icon" title={$t('transcript.clickToSeeConfidenceColors')}>
          <InfoIcon />
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

  <SpeakerPrivacyNotice fileUuid={file?.uuid} show={showsGender ? 'both' : 'voiceprints'} />
  {#if file?.uuid}
    <FileVoiceprintConsent
      fileUuid={file.uuid}
      state={file.voiceprint_consent ?? 'unspecified'}
      isOwner={!!file.user_id && file.user_id === $user?.uuid}
    />
  {/if}

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
      {#each speakerList as speaker (speaker.uuid)}
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
                title={$t('speaker.predictedGenderTooltip', {
                  gender: speaker.predicted_gender,
                  confidence: Math.round((speaker.attribute_confidence?.gender ?? 0) * 100),
                })}
              >
                {#if speaker.predicted_gender === 'male'}
                  <svg class="gender-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="10" cy="14" r="7"/><line x1="15" y1="9" x2="21" y2="3"/><polyline points="15 3 21 3 21 9"/></svg>
                  {$t('speakers.member.male')}
                {:else}
                  <svg class="gender-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="9" r="7"/><line x1="12" y1="16" x2="12" y2="23"/><line x1="9" y1="20" x2="15" y2="20"/></svg>
                  {$t('speakers.member.female')}
                {/if}
              </span>
            {/if}
            <div class="speaker-input-wrapper">
            <!--
              Deliberately NO `on:focus` auto-fill here. This field used to write
              `speaker.suggested_name` into `speaker.display_name` the moment the
              input received focus whenever `is_high_confidence` was true — which
              fires on nothing more than tabbing or clicking into the field to
              look at it, not on an affirmative accept. Suggestions (LLM,
              profile-match, metadata) are never auto-applied — only the explicit
              chips below (`suggestions-dropdown`) or a same-name edit that Save
              commits do that, both of which are the one-click accept the rule
              still allows. The `suggested-high`/`suggested-medium` border colors
              and the greyed-out placeholder are enough of a visual hint without
              silently writing a value nobody clicked to accept.
            -->
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
            />
            {#if speaker.show_profile_badge}
              <!-- Links to the speakers-page profile view rather than being a
                   dead-end badge — a labeled speaker's cross-recording profile
                   (occurrences, avatar, verified voiceprint) lives there, not here. -->
              <a
                class="speaker-profile-badge"
                href={resolve('/speakers?tab=profiles')}
                title={$t('transcript.speakerHasProfile')}
              >
                <svg class="profile-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"></path>
                  <circle cx="12" cy="7" r="4"></circle>
                </svg>
                <span class="profile-text">{$t('transcript.profile')}</span>
              </a>
            {/if}
            </div>
          </div>

          <!-- Speaker Timestamp Links -->
          {#if speaker.segment_timestamps && speaker.segment_timestamps.length > 0}
            <div class="speaker-timestamps">
              {#each speaker.segment_timestamps as ts, i (i)}
                <button
                  class="timestamp-link"
                  title={$t('transcript.jumpToTimestampTitle', { time: formatClock(ts.start_time) })}
                  on:click={() => handleSpeakerTimestampClick(ts.start_time, ts.uuid, ts.segment_index)}
                >
                  {formatClock(ts.start_time)}
                </button>
              {/each}
              {#if (speaker.segment_count ?? 0) > speaker.segment_timestamps.length}
                <span class="timestamp-more">
                  {(speaker.segment_count ?? 0) - speaker.segment_timestamps.length === 1
                    ? $t('transcript.moreSegmentsSingular')
                    : $t('transcript.moreSegments', { count: (speaker.segment_count ?? 0) - speaker.segment_timestamps.length })}
                </span>
              {/if}
            </div>
          {/if}

          {#if speaker.uuid}
            <SpeakerIdentityHistory speakerUuid={speaker.uuid} />
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
                            {#each speaker.metadata_hints.slice(0, 4) as hint, i (i)}
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
                                <span class="chip-confidence" dir="ltr">{Math.round(hint.confidence * 100)}%</span>
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
                            class:high-confidence={(speaker.confidence ?? 0) >= 0.75}
                            class:medium-confidence={(speaker.confidence ?? 0) >= 0.5 && (speaker.confidence ?? 0) < 0.75}
                            class:low-confidence={(speaker.confidence ?? 0) < 0.5}
                            on:click={() => {
                              // Unlike the metadata/profile chips beside it, this one only
                              // mutated `speaker.display_name` in place and never told the
                              // parent — `speakerNamesChanged` never flipped true, so Save
                              // stayed disabled until the user separately typed into the
                              // input. One click was supposed to be enough.
                              speaker.display_name = speaker.suggested_name ?? '';
                              dispatch('speakerNameChanged', { speakerId: speaker.uuid, newName: speaker.suggested_name ?? '' });
                            }}
                            title={speaker.suggestion_source === 'llm_analysis' ? $t('transcript.aiSuggestedBasedOnContent') : $t('transcript.aiSuggestedBasedOnSimilarity')}
                          >
                            {#if speaker.suggestion_source === 'llm_analysis'}
                              <svg class="source-icon" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                                <path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/>
                                <path d="M19 10v2a7 7 0 0 1-14 0v-2"/>
                                <line x1="12" y1="19" x2="12" y2="23"/>
                                <line x1="8" y1="23" x2="16" y2="23"/>
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
                              <span class="alignment-dot match" title={$t('speaker.genderMatchesHint')}></span>
                            {:else if speaker.gender_alignment === 'mismatch'}
                              <span class="alignment-dot mismatch" title={$t('speaker.genderConflictsHint')}></span>
                            {/if}
                            {speaker.suggested_name}
                            <span class="chip-source-label">
                              {speaker.suggestion_source === 'llm_analysis'
                                ? $t('transcript.sourceContentAnalysis')
                                  : $t('transcript.sourceVoiceSimilarity')}
                            </span>
                            <span class="chip-confidence" dir="ltr">{Math.round((speaker.confidence ?? 0) * 100)}%</span>
                          </button>
                        </div>
                      {/if}

                      {#if speaker.profile_suggestions && speaker.profile_suggestions.length > 0}
                        <div class="chip-row">
                          <span class="chip-label profile-label">{$t('transcript.profileMatch')}</span>
                          <div class="chips-wrap">
                            {#each speaker.profile_suggestions.slice(0, 4) as suggestion, i (i)}
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
                      <InfoIcon size={12} />
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
                        {#each visibleMatches as match, i (i)}
                          <div class="compact-match" title={match.title || match.media_file_title || $t('transcript.unknownVideo')}>
                            <span class="match-text">{((match.title || match.media_file_title || $t('transcript.unknownVideo')).length > 35 ? (match.title || match.media_file_title || $t('transcript.unknownVideo')).substring(0, 35) + '...' : (match.title || match.media_file_title || $t('transcript.unknownVideo')))}</span>
                            <span class="match-confidence">
                              <MenuIcon name="check" size={10} />
                              {#if match.same_speaker}
                                {$t('transcript.currentVideo')}
                              {:else if match.confidence}
                                <bdi dir="ltr">{Math.round(match.confidence * 100)}%</bdi>
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
                              {#each remainingMatches as match, i (i)}
                                <div class="popup-match">
                                  <span class="popup-match-text">{match.title || match.media_file_title || $t('transcript.unknownVideo')}</span>
                                  <span class="popup-match-confidence">
                                    <MenuIcon name="check" size={10} />
                                    {#if match.same_speaker}
                                      {$t('transcript.currentVideo')}
                                    {:else if match.confidence}
                                      <bdi dir="ltr">{Math.round(match.confidence * 100)}%</bdi>
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
          <Spinner size="inline" color="currentColor" />
          {$t('common.saving')}
        {:else}
          {$t('transcript.saveSpeakerNames')}
        {/if}
      </button>
      {#if propagationPending}
        <span
          class="propagation-pending"
          data-testid="rename-propagation-pending"
          title={$t('speakers.renamePropagationPending')}
        >
          <span class="propagation-pending-spinner" aria-hidden="true"></span>
          {$t('speakers.renamePropagationPending')}
        </span>
      {/if}
    </div>
  {:else}
    <p>{$t('transcript.noSpeakersFound')}</p>
  {/if}
  {/key}
</div>

<style>
  .speaker-editor-container {
    margin-top: var(--space-5);
    padding: var(--space-5);
    background: var(--background-secondary);
    border-radius: var(--radius-lg);
    border: 1px solid var(--border-color);
  }

  .speaker-editor-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: var(--space-4);
  }

  .legend-info-container {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    font-size: 0.9rem;
  }

  .legend-title {
    font-weight: var(--font-weight-semibold);
    color: var(--text-primary);
  }

  .legend-info-wrapper {
    position: relative;
    display: inline-block;
  }

  .legend-info-icon {
    background: none;
    border: none;
    color: var(--primary-on-surface);
    cursor: pointer;
    padding: var(--space-0-5);
    border-radius: var(--radius-round);
    transition: all var(--duration-base) ease;
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
    border-radius: var(--radius-md);
    box-shadow: var(--shadow-lg);
    z-index: var(--z-dropdown);
    padding: var(--space-3);
    min-width: 200px;
    margin-top: var(--space-1);
  }

  .legend-item {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    color: var(--text-color-secondary);
    margin-bottom: var(--space-2);
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
    font-size: var(--font-size-base);
    font-weight: var(--font-weight-semibold);
    color: var(--text-primary);
  }

  .speaker-list {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
  }

  .speaker-item {
    padding: var(--space-4);
    background: var(--surface-color);
    border-radius: var(--radius-lg);
    border: 1px solid var(--border-color);
    margin-bottom: var(--space-1);
    display: flex;
    flex-direction: column;
  }

  .speaker-header {
    display: flex;
    align-items: center;
    gap: var(--space-3);
    margin-bottom: var(--space-3);
  }

  .speaker-timestamps {
    display: flex;
    align-items: center;
    gap: var(--space-1-5);
    margin-bottom: var(--space-2);
    flex-wrap: wrap;
  }

  .timestamp-link {
    font-family: 'SF Mono', 'Fira Code', 'Fira Mono', 'Roboto Mono', monospace;
    font-size: 0.7rem;
    font-weight: var(--font-weight-medium);
    padding: var(--space-0-5) var(--space-2);
    border-radius: var(--radius-xl);
    border: 1px solid var(--border-color);
    background: var(--surface-color);
    color: var(--primary-on-surface);
    cursor: pointer;
    transition: all var(--duration-fast) ease;
    line-height: 1.4;
  }

  .timestamp-link:hover {
    background: var(--primary-color);
    color: var(--text-on-solid);
    border-color: var(--primary-color);
    transform: scale(1.02);
    box-shadow: var(--shadow-md);
  }

  .timestamp-more {
    font-size: 0.65rem;
    color: var(--text-secondary-color);
    font-style: italic;
    padding-top: 1px;
  }

  .speaker-content-below {
    margin-left: 0;
    padding-left: 0;
  }

  /* Matches .segment-speaker styling from transcript segments */
  .speaker-original {
    font-size: var(--font-size-xs);
    font-weight: var(--font-weight-semibold);
    padding: var(--space-0-5) var(--space-2);
    border-radius: var(--radius-2xl);
    white-space: nowrap;
    min-width: fit-content;
    max-width: 150px;
    overflow: hidden;
    text-overflow: ellipsis;
    border: 1px solid;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    transition: all var(--duration-base) ease;
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
    gap: var(--space-3);
  }

  .speaker-item input {
    flex: 1;
  }


  /* Embedding Suggestion Interface */

  .gender-badge {
    display: inline-flex;
    align-items: center;
    gap: 3px;
    font-size: 0.65rem;
    font-weight: var(--font-weight-medium);
    padding: 0.15rem 0.4rem;
    border-radius: var(--radius-pill);
    flex-shrink: 0;
    cursor: default;
    white-space: nowrap;
    background: rgba(128, 128, 128, 0.1);
    color: var(--text-secondary);
    border: 1px solid rgba(128, 128, 128, 0.25);
  }

  .gender-svg {
    width: 12px;
    height: 12px;
    flex-shrink: 0;
  }

  :global([data-theme='dark']) .gender-badge {
    background: rgba(255, 255, 255, 0.07);
    color: var(--text-secondary);
    border-color: rgba(255, 255, 255, 0.12);
  }

  .speaker-profile-badge {
    background: #f59e0b;
    color: var(--text-on-solid);
    font-size: var(--font-size-2xs);
    font-weight: var(--font-weight-semibold);
    padding: var(--space-1) var(--space-2);
    border-radius: var(--radius-md);
    display: inline-flex;
    align-items: center;
    gap: var(--space-1-5);
    white-space: nowrap;
    box-shadow: 0 1px 2px rgba(0, 0, 0, 0.1);
    flex-shrink: 0; /* Don't shrink the badge */
    text-decoration: none;
    cursor: pointer;
    transition: background var(--duration-fast) ease, transform var(--duration-fast) ease;
  }

  .speaker-profile-badge:hover {
    background: #d97706;
    transform: scale(1.03);
  }

  .profile-icon {
    width: 14px;
    height: 14px;
    stroke-width: 2.5;
  }

  .profile-text {
    line-height: 1;
    font-weight: var(--font-weight-semibold);
  }




  .match-confidence {
    display: inline-flex;
    align-items: center;
    gap: 0.2rem;
    font-size: var(--font-size-xs);
    font-weight: normal;
  }

  .save-speakers-button {
    margin-top: var(--space-4);
    padding: 0.6rem 1.2rem;
    background: var(--primary-color);
    color: var(--text-on-solid);
    border: none;
    border-radius: var(--radius-xl);
    font-size: 0.95rem;
    font-weight: var(--font-weight-medium);
    cursor: pointer;
    transition: all var(--duration-base) ease;
    box-shadow: var(--shadow-primary);
  }

  .save-speakers-button:hover {
    background: var(--primary-color);
    transform: scale(1.02);
    box-shadow: var(--shadow-primary-hover);
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

  .propagation-pending {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    margin-left: var(--space-3);
    padding: var(--space-1) 0.65rem;
    border-radius: var(--radius-pill);
    background-color: rgba(var(--primary-color-rgb), 0.1);
    color: var(--primary-on-surface);
    font-size: 0.78rem;
    font-weight: var(--font-weight-medium);
  }

  .propagation-pending-spinner {
    width: 0.8rem;
    height: 0.8rem;
    border: 2px solid rgba(var(--primary-color-rgb), 0.3);
    border-top-color: var(--primary-color);
    border-radius: var(--radius-round);
    animation: propagation-pending-spin 0.7s linear infinite;
  }

  @keyframes propagation-pending-spin {
    to {
      transform: rotate(360deg);
    }
  }

  /* Unsaved changes indicator (yellow dot) - matches AI Prompts modal */
  .unsaved-indicator {
    display: inline-flex;
    align-items: center;
    color: #f59e0b; /* Amber/yellow warning color */
    margin-inline-start: var(--space-2);
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

  /* Compact cross-video UI styles */
  .cross-video-compact {
    margin-top: 0.3rem;
    padding: var(--space-2);
    background-color: var(--background-main);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-sm);
    font-size: var(--font-size-xs);
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
    gap: var(--space-1);
  }

  .info-btn-consistent, .dropdown-arrow {
    background: none;
    border: none;
    cursor: pointer;
    padding: 0.15rem;
    border-radius: 3px;
    color: var(--text-color-secondary);
    transition: background-color var(--duration-base) ease;
    display: flex;
    align-items: center;
    justify-content: center;
  }

  .info-btn-consistent:hover, .dropdown-arrow:hover {
    background-color: var(--border-color-soft);
  }

  .dropdown-arrow svg {
    transition: transform var(--duration-base) ease;
  }

  .dropdown-arrow svg.rotated {
    transform: rotate(180deg);
  }

  .compact-dropdown {
    margin-top: var(--space-2);
    padding-top: var(--space-2);
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
    transition: background-color var(--duration-base) ease;
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
    font-weight: var(--font-weight-medium);
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
    color: var(--primary-on-surface);
    font-weight: var(--font-weight-medium);
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
    border-radius: var(--radius-lg);
    box-shadow: var(--shadow-lg);
    z-index: var(--z-dropdown);
    min-width: 250px;
    max-width: 350px;
    padding: var(--space-2);
    margin-top: var(--space-1);
  }

  .hover-container:hover .hover-popup {
    display: block;
  }

  .popup-match {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 0.3rem var(--space-2);
    border-radius: var(--radius-sm);
    margin-bottom: 0.2rem;
    background: var(--surface-color);
  }

  .popup-match:last-child {
    margin-bottom: 0;
  }

  .popup-match-text {
    flex: 1;
    font-size: var(--font-size-xs);
    color: var(--text-color);
    margin-right: var(--space-2);
  }

  .popup-match-confidence {
    display: inline-flex;
    align-items: center;
    gap: 0.2rem;
    font-size: 0.65rem;
    font-weight: var(--font-weight-medium);
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
    padding: 0.3rem var(--space-2);
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-sm);
    color: var(--text-secondary-color);
    font-size: var(--font-size-xs);
    cursor: pointer;
    transition: all var(--duration-base) ease;
    width: 100%;
    text-align: left;
  }

  .suggestions-toggle:hover {
    background: var(--surface-hover);
    color: var(--text-primary);
  }

  .suggestions-toggle svg {
    transition: transform var(--duration-base) ease;
    flex-shrink: 0;
  }

  .suggestions-toggle svg.rotated {
    transform: rotate(180deg);
  }

  .suggestions-dropdown {
    margin-top: 0.3rem;
    border: 1px solid var(--border-color);
    border-radius: var(--radius-sm);
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
    gap: var(--space-2);
  }

  .chip-row {
    display: flex;
    align-items: flex-start;
    gap: var(--space-2);
  }

  .chip-label {
    font-size: 0.7rem;
    font-weight: var(--font-weight-semibold);
    color: var(--text-secondary-color);
    min-width: 35px;
    padding-top: var(--space-1);
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
    padding: var(--space-1) var(--space-2);
    border: none;
    border-radius: var(--radius-3xl);
    font-size: 0.7rem;
    font-weight: var(--font-weight-medium);
    cursor: pointer;
    transition: all var(--duration-base) ease;
    color: var(--text-on-solid);
    white-space: nowrap;
  }

  .suggestion-chip:hover {
    transform: scale(1.02);
    box-shadow: 0 2px 4px rgba(0, 0, 0, 0.15);
  }

  .suggestion-chip.llm-chip {
    background: var(--primary-color);
  }

  .suggestion-chip.llm-chip:hover {
    background: var(--primary-color);
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
    font-weight: var(--font-weight-semibold);
  }

  .chip-confidence {
    font-size: 0.65rem;
    background: rgba(255, 255, 255, 0.25);
    padding: 0.1rem 0.3rem;
    border-radius: var(--radius-lg);
    font-weight: var(--font-weight-semibold);
  }

  .source-icon {
    width: 12px;
    height: 12px;
    margin-right: var(--space-1);
    opacity: 0.9;
    stroke: var(--text-on-solid);
    fill: none;
  }

  .metadata-chip {
    background: rgba(100, 149, 237, 0.1);
    color: var(--text-color);
    border: 1px solid rgba(100, 149, 237, 0.3);
  }

  .metadata-chip:hover {
    background: rgba(100, 149, 237, 0.2);
  }

  .metadata-label {
    color: var(--text-secondary);
  }

  .chip-role {
    font-size: 0.65rem;
    opacity: 0.7;
    text-transform: capitalize;
    font-style: italic;
  }

  /* The suggestion's source, always visible on the chip — not only in its
     hover tooltip. Distinguishing "content analysis" from "voice profile"
     from "voice similarity" matters for deciding whether to trust a
     suggestion, and a tooltip is invisible to touch and keyboard use. */
  .chip-source-label {
    font-size: 0.62rem;
    opacity: 0.85;
    font-style: italic;
    white-space: nowrap;
  }

  .alignment-dot {
    display: inline-block;
    width: 6px;
    height: 6px;
    border-radius: var(--radius-round);
    flex-shrink: 0;
  }

  .alignment-dot.match {
    background: var(--success-color);
  }

  .alignment-dot.mismatch {
    background: var(--warning-color);
  }


  @media (max-width: 768px) {
    .speaker-item {
      flex-direction: column;
      align-items: stretch;
    }

    .speaker-header {
      flex-wrap: wrap;
      gap: var(--space-1-5);
    }

    .speaker-original {
      font-size: var(--font-size-2xs);
      padding: var(--space-0-5) var(--space-1-5);
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
</style>
