<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { slide } from 'svelte/transition';
  import { getSpeakerColor } from '$lib/utils/speakerColors';
  import SpeakerMerge from '$components/SpeakerMerge.svelte';
  import { t } from '$stores/locale';
  import { translateSpeakerLabel } from '$lib/i18n';

  export let file: any = null;
  export let speakerList: any[] = [];
  export let speakerNamesChanged: boolean = false;
  export let savingSpeakers: boolean = false;

  const dispatch = createEventDispatcher();

  // Create a reactive key based on speakerList to force re-renders of speaker sections
  // This ensures Edit Speakers and Merge Speakers sections update when speakers change
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

  function formatSimpleTimestamp(seconds: number): string {
    const minutes = Math.floor(seconds / 60);
    const remainingSeconds = Math.floor(seconds % 60);
    return `${minutes}:${remainingSeconds.toString().padStart(2, '0')}`;
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
</script>

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

<style>
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

  /* Second .spinner rule (later in the original stylesheet → wins for
     conflicting `animation`/size props). Preserved verbatim so the
     save-speakers-button spinner renders identically. */
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

  @media (max-width: 768px) {
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
</style>
