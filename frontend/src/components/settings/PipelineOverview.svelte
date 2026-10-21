<!--
  PipelineOverview.svelte — the read-only "Processing pipeline" page (issue #1227).

  Shows what happens to the next recording, step by step (transcription, speaker detection,
  speaker identification, AI features), where each step runs, what the transcription model
  returns, and what stands in the way. Everything comes from GET /user-settings/pipeline: the
  server resolved it with the same function the pipeline runs, so this page cannot disagree
  with what happens. Nothing here edits a setting; each step and each problem links to the one
  tab that does. Problems are rendered from the plan's stable codes with fixed, translated
  sentences, never from raw server or vendor text.
-->
<script lang="ts">
  import SettingsSkeleton from '$components/ui/SettingsSkeleton.svelte';
  import { createEventDispatcher, onMount } from 'svelte';
  import { getPipelineView, type PipelineView } from '$lib/api/pipeline';
  import { audioVendors, buildPipelineSteps } from '$lib/settings/pipelineSteps';
  import {
    loadSectionData,
    peekSectionData,
    SectionDataKey,
    sameSectionData,
  } from '$lib/settings/sectionData';
  import type { SettingsSection } from '$stores/settingsModalStore';
  import { locale, t } from '$stores/locale';
  import Chip from '../ui/Chip.svelte';
  import CapabilityBadges from './CapabilityBadges.svelte';
  import IssueRow from './IssueRow.svelte';

  const dispatch = createEventDispatcher<{ navigate: SettingsSection }>();

  // Stale-while-revalidate: a view seen a moment ago paints at once and is refreshed behind it;
  // the skeleton is only for a cold miss.
  let view: PipelineView | null = peekSectionData<PipelineView>(SectionDataKey.pipeline) ?? null;
  let loading = view === null;
  let failed = false;

  $: model = view ? buildPipelineSteps(view) : null;
  $: vendors = view ? audioVendors(view) : [];
  // Presentational list joining ("A and B") in the active language.
  $: vendorList = new Intl.ListFormat($locale, { style: 'long', type: 'conjunction' }).format(vendors);

  async function load() {
    // A retry after a failure goes back to the skeleton; a refresh behind a shown view does not.
    if (view === null) loading = true;
    failed = false;
    try {
      const fresh = await loadSectionData(SectionDataKey.pipeline, getPipelineView);
      if (!sameSectionData(fresh, view)) view = fresh;
    } catch (err) {
      console.error('Failed to load the processing pipeline:', err);
      // A failed refresh keeps the view already on screen; only a cold failure shows the error.
      if (view === null) failed = true;
    } finally {
      loading = false;
    }
  }

  onMount(() => {
    void load();
  });
</script>

<div class="pipeline-overview" data-testid="pipeline-overview">
  {#if loading}
    <SettingsSkeleton variant="dashboard" label={$t('common.loading')} />
  {:else if failed || !model}
    <div class="state-box" role="alert">
      <p>{$t('settings.pipeline.loadFailed')}</p>
      <button type="button" class="btn btn-secondary" on:click={load}>
        {$t('settings.pipeline.retry')}
      </button>
    </div>
  {:else}
    {#if model.liteBanner}
      <p class="banner banner-info" data-testid="pipeline-lite-banner">
        {$t('settings.pipeline.liteBanner')}
      </p>
    {/if}
    {#if vendors.length > 0}
      <p class="banner banner-privacy" data-testid="pipeline-privacy">
        {$t('settings.pipeline.audioLeaves', { vendor: vendorList })}
      </p>
    {/if}

    <ol class="flow">
      <li class="audio-node" aria-hidden="true">{$t('settings.pipeline.audio')}</li>
      {#each model.steps as step, index (step.step)}
        <li class="flow-item">
          <span
            class="connector"
            class:off={!step.enabled}
            class:boundary={step.enabled && step.where === 'cloud'}
            class:caution={step.enabled && step.where !== 'cloud' && step.issues.some((i) => i.severity !== 'info')}
            data-testid="pipeline-connector"
            aria-hidden="true"
          >
            <svg class="connector-svg" viewBox="0 0 24 48" width="24" height="48" focusable="false">
              <line class="rail" x1="12" y1="0" x2="12" y2="38" />
              <line class="flowline" x1="12" y1="0" x2="12" y2="38" />
              <path class="head" d="M12 47 L4.5 35 L19.5 35 Z" />
            </svg>
          </span>
          <article
            class="step-card"
            class:off={!step.enabled}
            data-testid="pipeline-step"
            data-step={step.step}
            aria-labelledby={`pipeline-step-${step.step}`}
          >
            <header class="step-head">
              <span class="step-number" aria-hidden="true">{index + 1}</span>
              <h4 class="step-title" id={`pipeline-step-${step.step}`}>{$t(step.titleKey)}</h4>
              <span class="step-where" data-testid="pipeline-where">
                {#if !step.enabled}
                  <Chip>{$t('settings.pipeline.off')}</Chip>
                {:else if step.where === 'cloud'}
                  <Chip variant="info">
                    {$t('settings.pipeline.where.cloud', { vendor: step.vendor ?? '' })}
                  </Chip>
                {:else}
                  <Chip>{$t('settings.pipeline.where.server')}</Chip>
                {/if}
              </span>
            </header>
            <p class="step-purpose">{$t(step.purposeKey)}</p>

            {#if step.enabled}
              <p class="engine-line">
                <span class="engine-name">
                  {step.engineLabelKey ? $t(step.engineLabelKey) : (step.engineText ?? '')}
                </span>
                {#if step.model}<code class="engine-model">{step.model}</code>{/if}
              </p>
              {#if step.returnsSpeakers !== null || step.timing !== null || step.adapterBilling}
                <div class="badge-row">
                  <CapabilityBadges
                    returnsSpeakers={step.returnsSpeakers}
                    timing={step.timing}
                    adapterBilling={step.adapterBilling}
                  />
                </div>
              {/if}
              {#if step.hintsKey}
                <p class="hint-line" data-testid="pipeline-hints">{$t(step.hintsKey)}</p>
              {/if}
            {/if}

            {#if step.issues.length > 0}
              <ul class="issues">
                {#each step.issues as issue (issue.code)}
                  <IssueRow
                    code={issue.code}
                    severity={issue.severity}
                    messageKey={issue.messageKey}
                    fallbackText={issue.fallbackText}
                    sections={issue.fixSections}
                    on:navigate={(e) => dispatch('navigate', e.detail)}
                  />
                {/each}
              </ul>
            {/if}

            <footer class="step-foot">
              <button
                type="button"
                class="link-button"
                data-testid="pipeline-change"
                data-section={step.settingsSection}
                on:click={() => dispatch('navigate', step.settingsSection)}
              >
                {$t('settings.pipeline.change')}
              </button>
            </footer>
          </article>
        </li>
      {/each}
    </ol>
  {/if}
</div>

<style>
  .pipeline-overview {
    display: flex;
    flex-direction: column;
    gap: 1rem;
  }

  .state-box {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 1rem;
    padding: 2.5rem 1rem;
    color: var(--text-secondary);
  }

  .banner {
    margin: 0;
    padding: 0.75rem 1rem;
    border-radius: 8px;
    border: 1px solid var(--border-color);
    font-size: 0.85rem;
    line-height: 1.45;
    color: var(--text-color);
  }

  .banner-info {
    background: rgba(var(--primary-color-rgb), 0.08);
    border-color: rgba(var(--primary-color-rgb), 0.3);
  }

  .banner-privacy {
    background: rgba(var(--warning-color-rgb), 0.1);
    border-color: rgba(var(--warning-color-rgb), 0.35);
  }

  .flow {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    align-items: stretch;
  }

  .audio-node {
    align-self: flex-start;
    padding: 0.25rem 0.75rem;
    border-radius: 999px;
    border: 1px dashed var(--border-color);
    font-size: 0.8rem;
    color: var(--text-secondary);
  }

  .flow-item {
    display: flex;
    flex-direction: column;
  }

  /*
   * The connector into a step: a 48px rail with a filled arrowhead, drawn in SVG so the stroke is
   * the same weight everywhere. It is vertical in every layout (the cards stack at every width) and
   * symmetric, so it reads the same in RTL; only its inset follows the writing direction.
   * Tones: neutral blue for a step that runs on this server, amber where the audio leaves the
   * server or the step has a problem, grey and still for a step that is off.
   */
  .connector {
    --connector-color: var(--primary-on-surface);
    align-self: flex-start;
    /* Centre the 24px rail under the step number (card border + padding + half the number). */
    margin-inline-start: calc(1px + 1.25rem + 0.75rem - 12px);
    height: 48px;
    line-height: 0;
  }

  .connector.boundary,
  .connector.caution {
    --connector-color: var(--warning-color);
  }

  .connector.off {
    --connector-color: var(--text-secondary);
  }

  .connector-svg {
    display: block;
    overflow: visible;
  }

  .rail {
    stroke: var(--connector-color);
    stroke-width: 2;
    stroke-linecap: round;
    opacity: 0.35;
  }

  /* Dashes that travel toward the step: calm for an ordinary step, a little quicker over a boundary. */
  .flowline {
    stroke: var(--connector-color);
    stroke-width: 2;
    stroke-linecap: round;
    stroke-dasharray: 5 9;
    animation: pipeline-flow 2.8s linear infinite;
  }

  .connector.boundary .flowline {
    animation-duration: 1.6s;
  }

  .connector.off .flowline {
    animation: none;
    stroke-dasharray: 2 6;
    opacity: 0.6;
  }

  .head {
    fill: var(--connector-color);
  }

  @keyframes pipeline-flow {
    to {
      stroke-dashoffset: -28;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .flowline {
      animation: none;
    }
  }

  .step-card {
    background: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: 8px;
    padding: 1rem 1.25rem;
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }

  /* A step that does not run is dimmed, but its problem and its link are not. */
  .step-card.off > :not(.issues):not(.step-foot) {
    opacity: 0.6;
  }

  .step-head {
    display: flex;
    align-items: center;
    gap: 0.625rem;
  }

  .step-number {
    flex: none;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 1.5rem;
    height: 1.5rem;
    border-radius: 50%;
    background: rgba(var(--primary-color-rgb), 0.12);
    color: var(--primary-on-surface);
    font-size: 0.75rem;
    font-weight: var(--font-weight-semibold);
  }

  .step-title {
    flex: 1;
    margin: 0;
    font-size: 0.95rem;
    font-weight: 600;
    color: var(--text-color);
  }

  .step-purpose,
  .hint-line {
    margin: 0;
    font-size: 0.8rem;
    color: var(--text-secondary);
  }

  .engine-line {
    margin: 0;
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 0.5rem;
    font-size: 0.875rem;
    color: var(--text-color);
  }

  .engine-name {
    font-weight: 600;
  }

  .engine-model {
    font-size: 0.8rem;
    color: var(--text-secondary);
    background: var(--surface-secondary);
    border-radius: 4px;
    padding: 0.05rem 0.4rem;
  }

  .issues {
    list-style: none;
    margin: 0.25rem 0 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }

  .step-foot {
    display: flex;
    justify-content: flex-end;
  }

  .link-button {
    display: inline;
    padding: 0;
    border: none;
    border-radius: 0;
    box-shadow: none;
    background: none;
    color: var(--primary-on-surface);
    font: inherit;
    font-size: 0.8rem;
    text-decoration: underline;
    cursor: pointer;
  }

  .link-button:hover,
  .link-button:focus {
    background: none;
    box-shadow: none;
    transform: none;
  }

  .link-button:focus-visible {
    outline: 2px solid var(--primary-color);
    outline-offset: 2px;
  }
</style>
