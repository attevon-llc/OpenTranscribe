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
  /* The toggle is only as wide as its text and the tip follows it on the same line; the opened
     text drops below both at full width. `display: contents` lets the heading, the tip and the
     content be placed as items of this one flex row (the tip is a sibling of the disclosure,
     never inside its button). */
  .hold-explainer {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    column-gap: var(--space-2);
    font-size: var(--font-size-sm);
    color: var(--text-color);
  }

  .disclosure,
  .hold-explainer :global(.expandable) {
    display: contents;
  }

  .hold-explainer :global(.expandable) {
    border-block-end: none;
  }

  .hold-explainer :global(.expandable-heading) {
    order: 0;
  }

  .hold-explainer :global(.expandable-header) {
    inline-size: auto;
    padding-block: var(--space-2);
    padding-inline: 0;
    font-size: var(--font-size-sm);
  }

  .hold-explainer :global(.expandable-title) {
    flex: 0 1 auto;
  }

  .tip-slot {
    order: 1;
    display: inline-flex;
    align-items: center;
  }

  .hold-explainer :global(.expandable-content) {
    order: 2;
    flex: 0 0 100%;
    min-inline-size: 0;
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
