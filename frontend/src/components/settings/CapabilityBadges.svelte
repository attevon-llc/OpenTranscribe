<!--
  CapabilityBadges.svelte — what a transcription model hands back, as pills (issue #1227).

  The one place the vocabulary is drawn, for every surface that picks or shows a transcription
  model or provider (issue #1243): "Speakers included" / "Text only" says whether the model
  labels who spoke; "Word timing" / "Segment timing" / "No timing" says how finely it times
  its text, which decides whether speakers can be added to it at all. A provider chosen only
  for its speakers shows "Speakers via adapter (extra billing)". Values come straight from the
  server's catalog or pipeline view, never from a name.

  `explain` adds the consequence under the pills (what the choice means for speaker detection
  and what can be mixed); `legend` renders the key to the pills instead of a model's values.
-->
<script lang="ts">
  import Badge from '$components/ui/Badge.svelte';
  import type { TimestampGranularity } from '$lib/api/asrSettings';
  import { capabilityNotes } from '$lib/settings/asrCapabilities';
  import { TIMING_KEYS } from '$lib/settings/pipelineKeys';
  import { t } from '$stores/locale';

  /** Null hides the speakers pill (steps where it does not apply). */
  export let returnsSpeakers: boolean | null = null;
  /** Null hides the timing pill. */
  export let timing: TimestampGranularity | null = null;
  /** The provider is used as a speaker detector for a different transcription provider. */
  export let adapterBilling = false;
  /** Say what the flags mean for speaker detection under the pills. */
  export let explain = false;
  /** The model runs on this server: its speakers come from this server's detector. */
  export let local = false;
  /** The deployment has no local speaker detection. */
  export let lite = false;
  /** Render the key to the pills (one line per flag) instead of a model's values. */
  export let legend = false;

  const TIMING_VARIANT = { word: 'success', segment: 'warning', none: 'error' } as const;
  const TIMINGS = ['word', 'segment', 'none'] as const;
  const LEGEND_TIMING_KEYS = {
    word: 'settings.asrProvider.legend.timingWord',
    segment: 'settings.asrProvider.legend.timingSegment',
    none: 'settings.asrProvider.legend.timingNone',
  } as const;

  $: notes =
    explain && returnsSpeakers !== null && timing !== null
      ? capabilityNotes({ returnsSpeakers, timing }, { local, lite })
      : [];
</script>

{#if legend}
  <dl class="capability-legend" data-testid="capability-legend">
    <div class="legend-row">
      <dt>
        <Badge variant="success">{$t('settings.asrProvider.speakersIncluded')}</Badge>
      </dt>
      <dd>{$t('settings.asrProvider.legend.speakersIncluded')}</dd>
    </div>
    <div class="legend-row">
      <dt><Badge variant="default">{$t('settings.asrProvider.textOnly')}</Badge></dt>
      <dd>{$t('settings.asrProvider.legend.textOnly')}</dd>
    </div>
    {#each TIMINGS as granularity (granularity)}
      <div class="legend-row">
        <dt>
          <Badge variant={TIMING_VARIANT[granularity]}>{$t(TIMING_KEYS[granularity])}</Badge>
        </dt>
        <dd>{$t(LEGEND_TIMING_KEYS[granularity])}</dd>
      </div>
    {/each}
  </dl>
{:else}
  <span class="capability-badges" data-testid="capability-badges">
    {#if returnsSpeakers !== null}
      <span data-testid="badge-speakers" data-value={returnsSpeakers ? 'included' : 'text-only'}>
        <Badge variant={returnsSpeakers ? 'success' : 'default'}>
          {returnsSpeakers
            ? $t('settings.asrProvider.speakersIncluded')
            : $t('settings.asrProvider.textOnly')}
        </Badge>
      </span>
    {/if}
    {#if timing !== null}
      <span data-testid="badge-timing" data-value={timing}>
        <Badge variant={TIMING_VARIANT[timing]}>{$t(TIMING_KEYS[timing])}</Badge>
      </span>
    {/if}
    {#if adapterBilling}
      <span data-testid="badge-adapter">
        <Badge variant="warning">{$t('settings.asrProvider.speakersViaAdapter')}</Badge>
      </span>
    {/if}
  </span>
  {#if notes.length > 0}
    <ul class="capability-notes" data-testid="capability-notes">
      {#each notes as note (note.key)}
        <li class:blocking={note.blocking} data-testid="capability-note" data-key={note.key}>
          {$t(note.key)}
        </li>
      {/each}
    </ul>
  {/if}
{/if}

<style>
  .capability-badges {
    display: inline-flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.375rem;
  }

  .capability-notes {
    list-style: none;
    margin: 0.375rem 0 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 0.125rem;
    font-size: 0.75rem;
    line-height: 1.4;
    color: var(--text-secondary);
  }

  .capability-notes .blocking {
    color: var(--error-color, #dc2626);
  }

  .capability-legend {
    margin: 0;
    display: flex;
    flex-direction: column;
    gap: 0.375rem;
  }

  .legend-row {
    display: grid;
    grid-template-columns: 9.5rem 1fr;
    align-items: baseline;
    gap: 0.5rem;
  }

  .capability-legend dt,
  .capability-legend dd {
    margin: 0;
  }

  .capability-legend dd {
    font-size: 0.75rem;
    line-height: 1.4;
    color: var(--text-secondary);
  }

  @media (max-width: 540px) {
    .legend-row {
      grid-template-columns: 1fr;
      gap: 0.125rem;
    }
  }
</style>
