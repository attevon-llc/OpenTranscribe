<!--
  CapabilityBadges.svelte — what a transcription model hands back, as pills (issue #1227).

  "Speakers included" / "Text only" says whether the model labels who spoke; "Word timing" /
  "Segment timing" / "No timing" says how finely it times its text, which decides whether speakers
  can be added to it at all. A provider chosen only for its speakers shows "Speakers via adapter
  (extra billing)". Values come straight from the server's catalog or pipeline view.
-->
<script lang="ts">
  import Badge from '$components/ui/Badge.svelte';
  import type { TimestampGranularity } from '$lib/api/asrSettings';
  import { TIMING_KEYS } from '$lib/i18n/keyMaps';
  import { t } from '$stores/locale';

  /** Null hides the speakers pill (steps where it does not apply). */
  export let returnsSpeakers: boolean | null = null;
  /** Null hides the timing pill. */
  export let timing: TimestampGranularity | null = null;
  /** The provider is used as a speaker detector for a different transcription provider. */
  export let adapterBilling = false;

  const TIMING_VARIANT = { word: 'success', segment: 'warning', none: 'error' } as const;
</script>

<span class="capability-badges" data-testid="capability-badges">
  {#if returnsSpeakers !== null}
    <span data-testid="badge-speakers" data-value={returnsSpeakers ? 'included' : 'text-only'}>
      <Badge variant={returnsSpeakers ? 'success' : 'default'}>
        {returnsSpeakers ? $t('settings.asrProvider.speakersIncluded') : $t('settings.asrProvider.textOnly')}
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

<style>
  .capability-badges {
    display: inline-flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.375rem;
  }
</style>
