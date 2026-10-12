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
  import { createEventDispatcher, onMount } from 'svelte';
  import { getPipelineView, type PipelineView } from '$lib/api/pipeline';
  import { audioVendors, buildPipelineSteps } from '$lib/settings/pipelineSteps';
  import type { SettingsSection } from '$stores/settingsModalStore';
  import { locale, t } from '$stores/locale';
  import Spinner from '../ui/Spinner.svelte';
  import Chip from '../ui/Chip.svelte';
  import CapabilityBadges from './CapabilityBadges.svelte';
  import IssueRow from './IssueRow.svelte';

  const dispatch = createEventDispatcher<{ navigate: SettingsSection }>();

  let view: PipelineView | null = null;
  let loading = true;
  let failed = false;

  $: model = view ? buildPipelineSteps(view) : null;
  $: vendors = view ? audioVendors(view) : [];
  // Presentational list joining ("A and B") in the active language.
  $: vendorList = new Intl.ListFormat($locale, { style: 'long', type: 'conjunction' }).format(vendors);

  async function load() {
    loading = true;
    failed = false;
    try {
      view = await getPipelineView();
    } catch (err) {
      console.error('Failed to load the processing pipeline:', err);
      failed = true;
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
    <div class="state-box">
      <Spinner size="large" />
    </div>
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
          <span class="connector" aria-hidden="true"></span>
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

  /* A short vertical rule with an arrow head: reads the same in both writing directions. */
  .connector {
    align-self: flex-start;
    margin-inline-start: 1.6rem;
    width: 2px;
    height: 1.25rem;
    background: var(--border-color);
    position: relative;
  }

  .connector::after {
    content: '';
    position: absolute;
    inset-inline-start: -4px;
    bottom: -1px;
    border-inline: 5px solid transparent;
    border-top: 6px solid var(--border-color);
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
    font-weight: 700;
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
