<script lang="ts">
  import { slide } from 'svelte/transition';
  import SpeakerStats from './SpeakerStats.svelte';
  import { t } from '$stores/locale';

  interface OverallAnalytics {
    word_count?: number;
    duration_seconds?: number;
    talk_time?: {
      by_speaker: Record<string, number>;
      total: number;
    };
    turn_taking?: {
      by_speaker: Record<string, number>;
      total_turns: number;
    };
    interruptions?: {
      by_speaker: Record<string, number>;
      total: number;
    };
    questions?: {
      by_speaker: Record<string, number>;
      total: number;
    };
  }

  interface FileAnalytics {
    analytics?: {
      overall_analytics?: OverallAnalytics;
    };
    overall_analytics?: OverallAnalytics;
    status?: string;
  }

  interface Speaker {
    uuid: string;  // UUID (public identifier)
    name: string;
    display_name?: string;
  }

  export let file: FileAnalytics | null = null;
  export let isAnalyticsExpanded: boolean = false;
  export let speakerList: Speaker[] = [];
  export let transcriptStore: { speakers: Speaker[] } = { speakers: [] };
  export let diarizationDisabled: boolean = false;

  // Simple refresh counter to force re-rendering
  let refreshCounter = 0;

  // Use speakerList prop (gets updated immediately) or fallback to transcript store
  $: activeSpeakers = speakerList?.length > 0 ? speakerList : (transcriptStore?.speakers || []);


  // Create a unique key based on active speaker list and analytics data to force re-rendering when speakers change
  $: speakerKey = activeSpeakers.map(s => `${s.uuid}-${s.display_name || s.name}`).join('-');

  // Use only backend-provided analytics structure
  // Support both file.analytics.overall_analytics (nested) and file.overall_analytics (flat)
  $: analyticsData = file?.analytics?.overall_analytics || file?.overall_analytics;
  $: analyticsKey = analyticsData ?
    JSON.stringify({
      talk_time: analyticsData.talk_time,
      turn_taking: analyticsData.turn_taking,
      interruptions: analyticsData.interruptions,
      questions: analyticsData.questions
    }) : '';
  $: combinedKey = `${speakerKey}-${analyticsKey}-${refreshCounter}`;

  // Watch for changes and force refresh when analytics data OR speaker names change
  $: if ((file?.analytics?.overall_analytics || file?.overall_analytics) && analyticsKey) {
    refreshCounter++;
  }

  // Force refresh when speaker names change (for immediate frontend updates)
  $: if (speakerKey) {
    refreshCounter++;
  }

  function toggleAnalytics() {
    isAnalyticsExpanded = !isAnalyticsExpanded;
  }
</script>

<div class="analytics-dropdown-section">
  <button
    class="analytics-header"
    title={$t('analytics.toggleTooltip')} on:click={toggleAnalytics} on:keydown={e => e.key === 'Enter' && toggleAnalytics()} aria-expanded={isAnalyticsExpanded}>
    <h4 class="section-heading">{$t('analytics.title')}</h4>
    <div class="analytics-preview">
      {#if analyticsData}
        <span class="analytics-chip">
          {analyticsData.word_count ? $t('analytics.wordsCount', { count: analyticsData.word_count }) : $t('analytics.available')}
        </span>
      {:else if file && file.status === 'processing'}
        <span class="analytics-chip processing">{$t('analytics.processing')}</span>
      {:else}
        <span class="no-analytics">{$t('analytics.noAnalytics')}</span>
      {/if}
    </div>
    <span class="dropdown-toggle" aria-hidden="true">
      <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="transform: rotate({isAnalyticsExpanded ? '180deg' : '0deg'})">
        <polyline points="6 9 12 15 18 9"></polyline>
      </svg>
    </span>
  </button>

  {#if isAnalyticsExpanded}
    <div class="analytics-content" transition:slide={{ duration: 200 }}>
      {#if analyticsData}
        <!-- SpeakerStats is now properly reactive to its `analytics` prop, so this
             remount-on-change key is redundant, not wrong — harmless belt-and-braces. -->
        {#key combinedKey}
          <SpeakerStats
            analytics={{
              talk_time: analyticsData.talk_time || { by_speaker: {}, total: 0 },
              interruptions: analyticsData.interruptions || { by_speaker: {}, total: 0 },
              turn_taking: analyticsData.turn_taking || { by_speaker: {}, total_turns: 0 },
              questions: analyticsData.questions || { by_speaker: {}, total: 0 },
              ...analyticsData
            }}
            speakerList={activeSpeakers as Array<{name: string, display_name: string}>}
            {diarizationDisabled}
          />
        {/key}
      {:else if file && file.status === 'processing'}
        <p>{$t('analytics.beingProcessed')}</p>
      {:else if file && file.status === 'completed' && !file?.analytics?.overall_analytics && !file?.overall_analytics}
        <p>{$t('analytics.notAvailable')}</p>
      {/if}
    </div>
  {/if}
</div>

<style>
  .analytics-dropdown-section {
    margin-bottom: 0;
  }

  .analytics-header {
    width: 100%;
    display: flex;
    align-items: center;
    justify-content: space-between;
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: 8px;
    padding: 12px 16px;
    cursor: pointer;
    transition: all 0.2s ease;
  }

  .analytics-header:hover {
    background: var(--surface-hover);
    border-color: var(--border-hover);
  }

  .section-heading {
    margin: 0;
    font-size: 16px;
    font-weight: 600;
    color: var(--text-primary);
  }

  .analytics-preview {
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .analytics-chip {
    background: var(--primary-light);
    color: var(--primary-color);
    padding: 4px 8px;
    border-radius: 12px;
    font-size: 12px;
    font-weight: 500;
  }

  .analytics-chip.processing {
    background: var(--warning-light);
    color: var(--warning-color);
  }

  .no-analytics {
    color: var(--text-secondary);
    font-size: 12px;
    font-style: italic;
  }

  .dropdown-toggle svg {
    transition: transform 0.2s ease;
  }

  .analytics-content {
    border: 1px solid var(--border-color);
    border-top: none;
    border-radius: 0 0 8px 8px;
    background: var(--surface-color);
    padding: 20px;
  }

  .analytics-content p {
    margin: 0;
    color: var(--text-secondary);
    font-style: italic;
    text-align: center;
    padding: 20px;
  }
</style>
