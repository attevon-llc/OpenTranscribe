<script lang="ts">
  /**
   * The file page's collapsible "Processing" section (#1229), beside "File details".
   * Collapsed by default; the rows come from the page's `file.processing_steps`.
   */
  import { slide } from 'svelte/transition';
  import { t } from '$stores/locale';
  import type { ProcessingStep } from '$lib/types/media';
  import ProcessingProvenance from '$components/ProcessingProvenance.svelte';

  export let steps: ProcessingStep[] = [];
  export let expanded = false;

  const regionId = 'file-processing-section';
</script>

{#if steps.length}
  <div class="processing-section">
    <button
      type="button"
      class="section-header"
      aria-expanded={expanded}
      aria-controls={regionId}
      on:click={() => (expanded = !expanded)}
    >
      <h4 class="section-heading">{$t('fileProcessing.title')}</h4>
      <svg
        class="chevron"
        class:open={expanded}
        width="16"
        height="16"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="2"
        stroke-linecap="round"
        stroke-linejoin="round"
        aria-hidden="true"
      >
        <polyline points="6 9 12 15 18 9"></polyline>
      </svg>
    </button>
    {#if expanded}
      <div class="section-content" id={regionId} transition:slide={{ duration: 200 }}>
        <ProcessingProvenance {steps} />
      </div>
    {/if}
  </div>
{/if}

<style>
  .processing-section {
    margin-bottom: 20px;
  }

  .section-header {
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
    color: var(--text-primary);
  }

  .section-header:hover {
    background: var(--surface-hover);
    border-color: var(--border-hover);
  }

  .section-heading {
    margin: 0;
    font-size: 16px;
    font-weight: 600;
    color: var(--text-primary);
  }

  .chevron {
    transition: transform 0.2s ease;
  }

  .chevron.open {
    transform: rotate(180deg);
  }

  .section-content {
    border: 1px solid var(--border-color);
    border-top: none;
    border-radius: 0 0 8px 8px;
    background: var(--surface-color);
    padding: 16px 20px;
  }
</style>
