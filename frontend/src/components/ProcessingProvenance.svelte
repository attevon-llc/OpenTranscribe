<script lang="ts">
  /**
   * How a file was processed (#1229): one row per step, rendered from the backend's
   * display-ready `processing_steps`. Nothing is derived here: provider names come
   * pre-formatted, and a step the backend could not record says so instead of guessing.
   */
  import { t } from '$stores/locale';
  import type { ProcessingStep } from '$lib/types/media';
  import Badge from '$components/ui/Badge.svelte';

  export let steps: ProcessingStep[] = [];

  const STEP_KEYS: Record<ProcessingStep['step'], string> = {
    transcription: 'fileProcessing.step.transcription',
    diarization: 'fileProcessing.step.diarization',
    voiceprints: 'fileProcessing.step.voiceprints',
    summary: 'fileProcessing.step.summary',
    topics: 'fileProcessing.step.topics',
    speaker_names: 'fileProcessing.step.speakerNames'
  };
  const STATUS_KEYS: Record<Exclude<ProcessingStep['status'], 'done'>, string> = {
    off: 'fileProcessing.status.off',
    not_run: 'fileProcessing.status.notRun',
    not_recorded: 'fileProcessing.status.notRecorded'
  };
  const TIMING_KEYS: Record<NonNullable<ProcessingStep['timing']>, string> = {
    word: 'fileProcessing.timing.word',
    segment: 'fileProcessing.timing.segment',
    none: 'fileProcessing.timing.none'
  };
</script>

{#if steps.length}
  <ul class="processing-steps" aria-label={$t('fileProcessing.listLabel')}>
    {#each steps as row (row.step)}
      <li class="step-row" data-step={row.step} data-status={row.status}>
        <span class="step-name">{$t(STEP_KEYS[row.step])}</span>
        <span class="step-detail">
          {#if row.status === 'done'}
            {#if row.provider_display}
              <bdi class="provider">{row.provider_display}</bdi>
            {/if}
            {#if row.model}
              <bdi class="model">{row.model}</bdi>
            {/if}
            {#if row.where === 'server'}
              <Badge variant="info">{$t('fileProcessing.where.server')}</Badge>
            {:else if row.where === 'cloud'}
              <Badge variant="warning">{$t('fileProcessing.where.cloud')}</Badge>
            {:else}
              <span class="muted">{$t('fileProcessing.whereNotRecorded')}</span>
            {/if}
            {#if row.timing}
              <Badge>{$t(TIMING_KEYS[row.timing])}</Badge>
            {/if}
          {:else}
            <span class="muted">{$t(STATUS_KEYS[row.status])}</span>
          {/if}
        </span>
      </li>
    {/each}
  </ul>
{/if}

<style>
  .processing-steps {
    list-style: none;
    margin: 0;
    padding: 0;
    display: grid;
    gap: var(--space-1-5);
  }

  .step-row {
    display: grid;
    grid-template-columns: minmax(8rem, max-content) 1fr;
    gap: var(--space-1) var(--space-3);
    align-items: baseline;
    font-size: var(--font-size-sm);
  }

  .step-name {
    color: var(--text-secondary);
    font-weight: var(--font-weight-medium);
  }

  .step-detail {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-1-5);
    align-items: baseline;
    color: var(--text-color);
    min-width: 0;
  }

  .provider {
    font-weight: var(--font-weight-semibold);
  }

  .model {
    font-family: var(--font-mono);
    font-size: var(--font-size-xs);
    overflow-wrap: anywhere;
  }

  .muted {
    color: var(--text-secondary);
    font-style: italic;
  }
</style>
