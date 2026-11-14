<script>
  import { getSpeakerColor } from '$lib/utils/speakerColors';
  import { formatClock } from '$lib/utils/formatting';
  import { t } from '$stores/locale';
  import { translateSpeakerLabel } from '$lib/i18n';
  import EmptyState from './ui/EmptyState.svelte';

  // Type definitions matching FileDetail.svelte
  /**
   * @typedef {Object} SpeakerTime
   * @property {Record<string, number>} [by_speaker] - Speaker time mapped by speaker name
   * @property {number} [total] - Total talk time in seconds
   */

  /**
   * @typedef {Object} Interruptions
   * @property {Record<string, number>} [by_speaker] - Interruptions count by speaker name
   * @property {number} [total] - Total interruptions
   */

  /**
   * @typedef {Object} TurnTaking
   * @property {Record<string, number>} [by_speaker] - Turn count by speaker name
   * @property {number} [total_turns] - Total turns
   */

  /**
   * @typedef {Object} Questions
   * @property {Record<string, number>} [by_speaker] - Questions count by speaker name
   * @property {number} [total] - Total questions
   */

  /**
   * @typedef {Object} OverallFileMetrics
   * @property {number} [word_count] - Total words in the transcript
   * @property {number} [duration_seconds] - Duration of the audio/video file in seconds
   * @property {string} [clarity_score] - Clarity score (e.g., "Good", "Fair", "Poor")
   * @property {number} [sentiment_score] - Sentiment score from -1 (negative) to 1 (positive)
   * @property {number} [sentiment_magnitude] - Strength of sentiment
   * @property {number} [silence_ratio] - Ratio of silence in the audio
   * @property {number} [speaking_pace] - Average words per minute
   * @property {string} [language] - Detected language
   * @property {SpeakerTime} [talk_time] - Talk time statistics
   * @property {Interruptions} [interruptions] - Interruptions statistics
   * @property {TurnTaking} [turn_taking] - Turn taking statistics
   * @property {Questions} [questions] - Questions statistics
   */

  // Default values for all metrics
  const DEFAULT_METRICS = {
    word_count: 0,
    duration_seconds: 0,
    clarity_score: 'N/A',
    sentiment_score: 0,
    sentiment_magnitude: 0,
    silence_ratio: 0,
    speaking_pace: 0,
    language: 'en',
    talk_time: { by_speaker: {}, total: 0 },
    interruptions: { by_speaker: {}, total: 0 },
    turn_taking: { by_speaker: {}, total_turns: 0 },
    questions: { by_speaker: {}, total: 0 }
  };

  // Component props - accept partial metrics with all properties optional
  /** @type {OverallFileMetrics} */
  export let analytics = {};
  /** @type {Array<{name: string, display_name: string}>} */
  export let speakerList = [];
  /** @type {boolean} */
  export let diarizationDisabled = false;

  // Merge provided analytics with defaults. Reactive ($:), not const — this must
  // recompute whenever the `analytics` prop changes, or `normalizedAnalytics`
  // below latches to whatever was passed on the FIRST render.
  $: safeAnalytics = {
    ...DEFAULT_METRICS,
    ...analytics,
    talk_time: {
      by_speaker: {},
      total: 0,
      ...(analytics.talk_time || {})
    },
    interruptions: {
      by_speaker: {},
      total: 0,
      ...(analytics.interruptions || {})
    },
    turn_taking: {
      by_speaker: {},
      total_turns: 0,
      ...(analytics.turn_taking || {})
    },
    questions: {
      by_speaker: {},
      total: 0,
      ...(analytics.questions || {})
    }
  };

  // Create a normalized analytics object with default values
  $: normalizedAnalytics = {
    ...safeAnalytics,
    talk_time: {
      by_speaker: safeAnalytics.talk_time?.by_speaker || {},
      total: safeAnalytics.talk_time?.total || 0
    },
    interruptions: {
      by_speaker: safeAnalytics.interruptions?.by_speaker || {},
      total: safeAnalytics.interruptions?.total || 0
    },
    turn_taking: {
      by_speaker: safeAnalytics.turn_taking?.by_speaker || {},
      total_turns: safeAnalytics.turn_taking?.total_turns || 0
    },
    questions: {
      by_speaker: safeAnalytics.questions?.by_speaker || {},
      total: safeAnalytics.questions?.total || 0
    }
  };

  /**
   * Safely get entries from an object with type safety
   * @template T
   * @param {Record<string, T> | null | undefined} obj - The object to get entries from
   * @returns {[string, T][]} Array of [key, value] pairs
   */
  function getObjectEntries(obj) {
    return Object.entries(obj || {});
  }

  /**
   * Safely calculate percentage, handling division by zero
   * @param {number} [part=0] - The part of the total
   * @param {number} [total=0] - The total value
   * @returns {number} The calculated percentage or 0 if total is 0
   */
  function safeCalculatePercentage(part = 0, total = 0) {
    return total > 0 ? calculatePercentage(part, total) : 0;
  }

  /**
   * Calculate percentage for talk time
   * @param {number} speakerTime - The time a speaker has talked
   * @param {number} totalTime - The total time of the recording
   * @returns {number} - The percentage of time the speaker talked
   */
  function calculatePercentage(speakerTime, totalTime) {
    if (!totalTime) return 0;
    return (speakerTime / totalTime) * 100;
  }

  /**
   * Create reactive mapping of speaker keys to display names
   * This will automatically update when speakerList changes
   */
  $: speakerDisplayNameMap = speakerList.reduce((map, speaker) => {
    map[speaker.name] = speaker.display_name || speaker.name;
    return map;
  }, /** @type {Record<string, string>} */ ({}));


  /**
   * Get display name for a speaker (maps SPEAKER_XX to user-assigned names or translated labels)
   * @param {string} speakerKey - The speaker key from analytics (e.g., "SPEAKER_01")
   * @returns {string} - Display name, translated label, or original key if not found
   */
  function getSpeakerDisplayName(speakerKey) {
    // First check if there's a user-assigned display name
    // The map falls back to the raw SPEAKER_XX key for unnamed speakers, so the label is
    // translated on the way out; a user-assigned name passes through unchanged.
    return translateSpeakerLabel(speakerDisplayNameMap[speakerKey] || speakerKey);
  }

  /**
   * Get speaker color using the shared color system (matching transcript style)
   * @param {string} speakerKey - The speaker key from analytics (e.g., "SPEAKER_01")
   * @returns {string} - A CSS color value
   */
  function getSpeakerBgColor(speakerKey) {
    // Analytics data always uses original speaker keys (SPEAKER_XX)
    // Always use the original key for color consistency
    return getSpeakerColor(speakerKey).bg;
  }

</script>

<div class="speaker-stats compact-layout">
  <!-- Analytics Overview Cards -->
  <div class="analytics-overview">
    <div class="overview-card">
      <div class="card-icon">
        <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>
        </svg>
      </div>
      <div class="card-content">
        <div class="card-value">{normalizedAnalytics.word_count || 0}</div>
        <div class="card-label">{$t('stats.words')}</div>
      </div>
    </div>

    <div class="overview-card">
      <div class="card-icon">
        <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <circle cx="12" cy="12" r="10"/>
          <polyline points="12,6 12,12 16,14"/>
        </svg>
      </div>
      <div class="card-content">
        <div class="card-value">{formatClock(normalizedAnalytics.duration_seconds || 0)}</div>
        <div class="card-label">{$t('stats.duration')}</div>
      </div>
    </div>

    {#if !diarizationDisabled}
    <div class="overview-card">
      <div class="card-icon">
        <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M8 7a4 4 0 1 0 8 0 4 4 0 0 0-8 0z"/>
          <path d="M6 21v-2a4 4 0 0 1 4-4h4a4 4 0 0 1 4 4v2"/>
        </svg>
      </div>
      <div class="card-content">
        <div class="card-value">{Object.keys(normalizedAnalytics.talk_time?.by_speaker || {}).length}</div>
        <div class="card-label">{$t('stats.speakers')}</div>
      </div>
    </div>

    {#if normalizedAnalytics.interruptions?.total > 0}
    <div class="overview-card">
      <div class="card-icon">
        <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>
        </svg>
      </div>
      <div class="card-content">
        <div class="card-value">{normalizedAnalytics.interruptions.total}</div>
        <div class="card-label">{$t('stats.interruptions')}</div>
      </div>
    </div>
    {/if}
    {/if}
  </div>

  {#if normalizedAnalytics.talk_time.total > 0}
    {#if !diarizationDisabled}
    <!-- Compact Talk Time Section -->
    <div class="section-compact">
      <h3 class="section-title">
        <span class="section-icon">
          <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M3 3v18h18"/>
            <path d="M18.7 8l-5.1 5.2-2.8-2.7L7 14.3"/>
          </svg>
        </span>
        {$t('stats.talkTimeDistribution')}
      </h3>

      <!-- Large Horizontal Bar Chart -->
      <div class="talk-time-bar-large">
        {#each getObjectEntries(normalizedAnalytics.talk_time.by_speaker) as [speakerName, time] (speakerName)}
          <div
            class="speaker-segment-large"
            style="
              width: {safeCalculatePercentage(time, normalizedAnalytics.talk_time.total)}%;
              background-color: {getSpeakerBgColor(speakerName)};
            "
            title="{getSpeakerDisplayName(speakerName)}: {formatClock(time)} ({safeCalculatePercentage(time, normalizedAnalytics.talk_time.total).toFixed(1)}%)"
          ></div>
        {/each}
      </div>

      <!-- Combined Speaker Data Chips -->
      <div class="speaker-chips-grid">
        {#each getObjectEntries(normalizedAnalytics.talk_time.by_speaker).sort(([,a], [,b]) => b - a) as [speakerName, time] (speakerName)}
          {@const speakerTurns = normalizedAnalytics.turn_taking?.by_speaker?.[speakerName] || 0}
          <div class="speaker-chip">
            <div class="chip-header">
              <div class="speaker-dot-large" style="background-color: {getSpeakerBgColor(speakerName)};"></div>
              <span class="speaker-name-chip">{getSpeakerDisplayName(speakerName)}</span>
            </div>
            <div class="chip-stats">
              <div class="stat-item">
                <span class="stat-value">{formatClock(time)}</span>
                <span class="stat-label">{$t('stats.time')}</span>
              </div>
              <div class="stat-item">
                <span class="stat-value" dir="ltr">{safeCalculatePercentage(time, normalizedAnalytics.talk_time.total).toFixed(1)}%</span>
                <span class="stat-label">{$t('stats.share')}</span>
              </div>
              {#if normalizedAnalytics.turn_taking?.total_turns > 0}
              <div class="stat-item">
                <span class="stat-value">{speakerTurns}</span>
                <span class="stat-label">{speakerTurns === 1 ? $t('stats.turn') : $t('stats.turns')}</span>
              </div>
              {/if}
            </div>
          </div>
        {/each}
      </div>
    </div>

      <!-- Additional Metrics Row -->
      {#if (normalizedAnalytics.interruptions?.total > 0) || (normalizedAnalytics.questions?.total > 0)}
      <div class="additional-metrics-row">
        {#if normalizedAnalytics.interruptions?.total > 0}
        <div class="metric-section">
          <h4 class="metric-title">
            <span class="metric-icon">
              <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>
              </svg>
            </span>
            {$t('stats.interruptions')}
          </h4>
          <div class="metric-list">
            {#each getObjectEntries(normalizedAnalytics.interruptions.by_speaker) as [speakerName, count] (speakerName)}
              <div class="metric-item">
                <div class="metric-speaker">
                  <div class="speaker-dot" style="background-color: {getSpeakerBgColor(speakerName)};"></div>
                  <span class="speaker-name-small">{getSpeakerDisplayName(speakerName)}</span>
                </div>
                <span class="metric-value">{count}</span>
              </div>
            {/each}
          </div>
        </div>
        {/if}

        {#if normalizedAnalytics.questions?.total > 0}
        <div class="metric-section">
          <h4 class="metric-title">
            <span class="metric-icon">
              <svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                <circle cx="12" cy="12" r="10"/>
                <path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/>
                <circle cx="12" cy="17" r="0.5"/>
              </svg>
            </span>
            {$t('stats.questions')}
          </h4>
          <div class="metric-list">
            {#each getObjectEntries(normalizedAnalytics.questions.by_speaker) as [speakerName, count] (speakerName)}
              <div class="metric-item">
                <div class="metric-speaker">
                  <div class="speaker-dot" style="background-color: {getSpeakerBgColor(speakerName)};"></div>
                  <span class="speaker-name-small">{getSpeakerDisplayName(speakerName)}</span>
                </div>
                <span class="metric-value">{count}</span>
              </div>
            {/each}
          </div>
        </div>
        {/if}
      </div>
      {/if}
    {/if}
  {:else}
    <EmptyState title={$t('stats.noAnalytics')} padding="1rem" />
  {/if}
</div>

<style>
  .speaker-stats.compact-layout {
    display: flex;
    flex-direction: column;
    gap: var(--space-4);
    background-color: var(--surface-color);
    border-radius: var(--radius-lg);
    padding: var(--space-4);
    box-shadow: var(--shadow-sm);
  }

  /* Analytics Overview Cards */
  .analytics-overview {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
    gap: var(--space-3);
    margin-bottom: var(--space-2);
  }

  .overview-card {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    background: var(--background-color);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-md);
    padding: var(--space-3);
    transition: all var(--duration-base) ease;
  }

  .overview-card:hover {
    border-color: var(--primary-color);
    transform: scale(1.02);
  }

  .card-icon {
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--primary-on-surface);
    flex-shrink: 0;
  }

  .card-icon svg {
    width: 16px;
    height: 16px;
  }

  .card-content {
    display: flex;
    flex-direction: column;
    gap: var(--space-0-5);
    min-width: 0;
  }

  .card-value {
    font-size: var(--font-size-xl);
    font-weight: var(--font-weight-semibold);
    color: var(--text-color);
    line-height: 1.2;
  }

  .card-label {
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
    line-height: 1;
  }

  /* Compact Sections */
  .section-compact {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
  }

  .section-title {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    font-size: var(--font-size-body);
    font-weight: var(--font-weight-semibold);
    color: var(--text-color);
    margin: 0;
  }

  .section-icon {
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--primary-on-surface);
  }

  .section-icon svg {
    width: 14px;
    height: 14px;
  }

  /* Large Talk Time Bar */
  .talk-time-bar-large {
    display: flex;
    width: 100%;
    height: 24px;
    border-radius: var(--radius-2xl);
    overflow: hidden;
    box-shadow: var(--shadow-md);
    margin: var(--space-3) 0 var(--space-4) 0;
    border: 1px solid var(--border-color);
  }

  .speaker-segment-large {
    height: 100%;
    transition: all var(--duration-base) ease;
    cursor: pointer;
  }

  .speaker-segment-large:hover {
    opacity: 0.85;
    transform: scaleY(1.1);
  }

  /* Speaker Data Chips Grid */
  .speaker-chips-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: var(--space-3);
  }

  .speaker-chip {
    background: var(--background-color);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-lg);
    padding: var(--space-3);
    transition: all var(--duration-base) ease;
  }

  .speaker-chip:hover {
    border-color: var(--primary-color);
    box-shadow: var(--shadow-md);
    transform: scale(1.02);
  }

  .chip-header {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    margin-bottom: var(--space-2);
  }

  .speaker-dot-large {
    width: 16px;
    height: 16px;
    border-radius: var(--radius-round);
    flex-shrink: 0;
  }

  .speaker-name-chip {
    font-weight: var(--font-weight-medium);
    color: var(--text-color);
    font-size: var(--font-size-body);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    flex: 1;
    min-width: 0;
  }

  .chip-stats {
    display: flex;
    justify-content: space-between;
    gap: var(--space-3);
  }

  .stat-item {
    display: flex;
    flex-direction: column;
    align-items: center;
    flex: 1;
  }

  .stat-value {
    font-weight: var(--font-weight-semibold);
    color: var(--text-color);
    font-size: var(--font-size-body);
    line-height: 1.2;
  }

  .stat-label {
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
    margin-top: var(--space-0-5);
  }

  /* Additional Metrics Row */
  .additional-metrics-row {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: var(--space-3);
    margin-top: var(--space-3);
  }

  .metric-section {
    background: var(--background-color);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-md);
    padding: var(--space-3);
  }

  .metric-title {
    display: flex;
    align-items: center;
    gap: var(--space-1-5);
    font-size: var(--font-size-md);
    font-weight: var(--font-weight-semibold);
    color: var(--text-color);
    margin: 0 0 var(--space-2) 0;
  }

  .metric-icon {
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--text-secondary);
  }

  .metric-icon svg {
    width: 12px;
    height: 12px;
  }

  .metric-list {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
  }

  .metric-item {
    display: flex;
    justify-content: space-between;
    align-items: center;
    font-size: var(--font-size-sm);
    padding: var(--space-1) 0;
  }

  .metric-speaker {
    display: flex;
    align-items: center;
    gap: var(--space-1-5);
    flex: 1;
    min-width: 0;
  }

  .speaker-dot {
    width: 6px;
    height: 6px;
    border-radius: var(--radius-round);
    flex-shrink: 0;
  }

  .speaker-name-small {
    color: var(--text-color);
    font-weight: var(--font-weight-medium);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .metric-value {
    color: var(--text-secondary);
    font-weight: var(--font-weight-medium);
    flex-shrink: 0;
  }

  @media (max-width: 768px) {
    .speaker-stats.compact-layout {
      padding: var(--space-3);
    }

    .analytics-overview {
      grid-template-columns: repeat(auto-fit, minmax(100px, 1fr));
      gap: var(--space-2);
    }

    .overview-card {
      padding: var(--space-2);
    }

    .card-value {
      font-size: var(--font-size-base);
    }

    .card-label {
      font-size: var(--font-size-xs);
    }

    .speaker-chips-grid {
      grid-template-columns: 1fr;
    }

    .chip-stats {
      gap: var(--space-2);
    }

    .additional-metrics-row {
      grid-template-columns: 1fr;
    }
  }

  @media (max-width: 480px) {
    .analytics-overview {
      grid-template-columns: repeat(2, 1fr);
    }

    .card-icon svg {
      width: 14px;
      height: 14px;
    }

    .section-title {
      font-size: var(--font-size-md);
    }

    .talk-time-bar-large {
      height: 20px;
      border-radius: var(--radius-xl);
    }

    .speaker-chip {
      padding: var(--space-2);
    }

    .chip-stats {
      flex-direction: column;
      gap: var(--space-1-5);
    }

    .stat-item {
      flex-direction: row;
      justify-content: space-between;
    }
  }
</style>
