<!--
  HoldExplainer.svelte - what a legal hold does, in plain language (legal-hold plan, Lane D).

  An info tip with the one-sentence summary, and a "What does a legal hold do?" disclosure:
  what is kept, what is blocked, what still works, who can release it, and how a hold differs
  from quarantine, the trash and deleting. Used by the holds panel, the place-hold dialog and
  the held-file banner, so the three never explain it differently. It never shows a hold's
  reason: there is no prop for one.
-->
<script lang="ts">
  import InfoTip from '$components/settings/transcription/InfoTip.svelte';
  import ExpandableSection from '$components/ui/ExpandableSection.svelte';
  import { t } from '$stores/locale';

  /** Start with the disclosure open. */
  export let expanded = false;

  const POINTS = [
    'holds.explainer.kept',
    'holds.explainer.blocked',
    'holds.explainer.allowed',
    'holds.explainer.release',
    'holds.explainer.difference',
  ] as const;
</script>

<div class="hold-explainer" data-testid="hold-explainer">
  <!-- The tip sits beside the disclosure, not inside its header: a button inside a button is
       invalid and would swallow the header's click. -->
  <div class="disclosure">
    <ExpandableSection title={$t('holds.explainer.title')} bind:expanded>
      <p class="intro">{$t('holds.explainer.intro')}</p>
      <ul class="points">
        {#each POINTS as key (key)}
          <li>{$t(key)}</li>
        {/each}
      </ul>
    </ExpandableSection>
  </div>
  <span class="tip-slot">
    <InfoTip inline>{$t('holds.tooltip')}</InfoTip>
  </span>
</div>

<style>
  .hold-explainer {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    align-items: start;
    column-gap: var(--space-2);
    font-size: var(--font-size-sm);
    color: var(--text-color);
  }

  .disclosure {
    min-width: 0;
  }

  .hold-explainer :global(.expandable) {
    border-block-end: none;
  }

  .hold-explainer :global(.expandable-header) {
    padding-block: var(--space-2);
    padding-inline: 0;
    font-size: var(--font-size-sm);
  }

  .tip-slot {
    display: inline-flex;
    align-items: center;
    padding-block-start: var(--space-2);
  }

  .intro {
    margin: 0 0 var(--space-2);
    color: var(--text-secondary);
    line-height: var(--line-height-body);
  }

  .points {
    margin: 0;
    padding-inline-start: var(--space-5);
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
    line-height: var(--line-height-body);
  }
</style>
