<script lang="ts">
  /**
   * Notice under the navbar on a public read-only demo (issue #628): changes are turned off and
   * the data is replaced at the next nightly reset. Fixed like the erasure and support banners;
   * the measured height is published as `--demo-banner-height` so the layout offsets content.
   */
  import { onDestroy, onMount } from 'svelte';
  import { locale, t } from '$stores/locale';
  import { demoInfo, demoMode, loadDemoInfo } from '$stores/demo';

  let height = 0;

  function measure(node: HTMLElement) {
    const update = () => (height = node.offsetHeight);
    update();
    if (typeof ResizeObserver === 'undefined') return {};
    const observer = new ResizeObserver(update);
    observer.observe(node);
    return { destroy: () => observer.disconnect() };
  }

  $: resetTime = $demoInfo?.next_reset_at
    ? new Intl.DateTimeFormat($locale, { dateStyle: 'medium', timeStyle: 'short' }).format(
        new Date($demoInfo.next_reset_at)
      )
    : '';

  $: if (typeof document !== 'undefined') {
    document.documentElement.style.setProperty('--demo-banner-height', `${$demoMode ? height : 0}px`);
  }
  onDestroy(() => {
    if (typeof document !== 'undefined') {
      document.documentElement.style.removeProperty('--demo-banner-height');
    }
  });

  onMount(() => {
    if ($demoMode) void loadDemoInfo();
  });
</script>

{#if $demoMode}
  <div class="demo-banner" role="status" data-testid="demo-banner" use:measure>
    {resetTime ? $t('demo.bannerMessage', { time: resetTime }) : $t('demo.bannerMessageNoTime')}
  </div>
{/if}

<style>
  .demo-banner {
    position: fixed;
    inset-inline: 0;
    top: calc(var(--navbar-height) + env(safe-area-inset-top, 0px) + var(--support-banner-height));
    z-index: var(--z-dropdown);
    padding: var(--space-1-5) var(--space-4);
    text-align: center;
    font-size: var(--font-size-sm);
    background: var(--info-bg);
    color: var(--info-text);
    border-bottom: 1px solid var(--info-border);
  }
</style>
