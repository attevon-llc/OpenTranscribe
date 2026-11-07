<!--
  HeldFileBanner.svelte - "This file is under a legal hold", on the file page.

  Says who holds the file (organization, operator or both, from `held_by`) and never why:
  the reason belongs to the administrators who placed the hold. One plain line about what is
  frozen and what still works, then the shared explainer. Its body text has a fixed id so the
  page's disabled controls near it can point `aria-describedby` at it.
-->
<script lang="ts">
  import HoldExplainer from '$components/legal-hold/HoldExplainer.svelte';
  import MenuIcon from '$components/ui/MenuIcon.svelte';
  import { HOLD_BANNER_KEYS, holdAuthorityKey, labelFor } from '$lib/i18n/keyMaps';
  import type { HoldSummary } from '$lib/types/hold';
  import { t } from '$stores/locale';

  export let hold: HoldSummary | null | undefined = undefined;

  $: byWhom = labelFor($t, HOLD_BANNER_KEYS, holdAuthorityKey(hold?.held_by));
</script>

{#if hold?.held}
  <section class="held-banner" role="note" aria-labelledby="held-file-banner-title" data-testid="held-file-banner">
    <span class="icon" aria-hidden="true"><MenuIcon name="shield" size={18} /></span>
    <div class="content">
      <h2 class="title" id="held-file-banner-title">{$t('holds.banner.title')}</h2>
      <p class="text" id="held-file-banner-text">
        {#if byWhom}<span class="by">{byWhom}</span>{/if}
        {$t('holds.banner.body')}
      </p>
      <HoldExplainer />
    </div>
  </section>
{/if}

<style>
  .held-banner {
    display: flex;
    align-items: flex-start;
    gap: var(--space-3);
    margin-block: var(--space-3);
    padding: var(--space-3) var(--space-4);
    border: 1px solid var(--color-warning-border);
    border-inline-start: 4px solid var(--warning-solid);
    border-radius: var(--radius-md);
    background: var(--color-warning-bg);
    color: var(--text-color);
  }

  .icon {
    display: inline-flex;
    flex-shrink: 0;
    color: var(--color-warning-text);
    padding-block-start: var(--space-0-5);
  }

  .content {
    flex: 1;
    min-width: 0;
  }

  .title {
    margin: 0;
    font-size: var(--font-size-md);
    font-weight: var(--font-weight-semibold);
  }

  .text {
    margin: var(--space-1) 0 0;
    font-size: var(--font-size-sm);
    line-height: var(--line-height-body);
  }

  .by {
    font-weight: var(--font-weight-medium);
  }
</style>
