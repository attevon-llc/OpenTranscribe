<script lang="ts">
  /**
   * Credit and licence for a seeded demo file (CC BY and CC BY-SA terms require it wherever the
   * work is shown). Renders nothing for a file with no recorded licence, so it is safe to place
   * on every file page.
   */
  import { demoAttributions } from '$stores/demo';
  import { t } from '$stores/locale';
  import { SLOT, splitAtSlot } from '$lib/i18n/slots';

  export let fileUuid: string | null | undefined = null;

  $: item = fileUuid ? $demoAttributions[fileUuid] : undefined;
  $: [before, after] = splitAtSlot(
    $t('demo.attribution', { credit: item?.attribution ?? '', license: SLOT })
  );

  function safeHref(url: string): string | null {
    return /^https:\/\//i.test(url) ? url : null;
  }
</script>

{#if item}
  <p class="demo-attribution" data-testid="demo-attribution">
    {before}{#if safeHref(item.license_url)}<a
        href={safeHref(item.license_url)}
        target="_blank"
        rel="noopener noreferrer">{item.license}</a
      >{:else}{item.license}{/if}{after}
    {#if safeHref(item.source_url)}
      <a href={safeHref(item.source_url)} target="_blank" rel="noopener noreferrer"
        >{$t('demo.attributionSource')}</a
      >
    {/if}
  </p>
{/if}

<style>
  .demo-attribution {
    margin: var(--space-1) 0 0;
    font-size: var(--font-size-sm);
    color: var(--text-light);
  }

  .demo-attribution a {
    color: var(--primary-on-surface);
  }
</style>
