<script lang="ts">
  /**
   * App-level notice while the signed-in person has an open self-service erasure request
   * (issue #1354). The account stays usable during the grace period; this only says so and
   * opens the Settings card where the request can be cancelled. Reads once on mount; the
   * Settings card keeps `openErasureRequest` current after that.
   */
  import { onDestroy, onMount } from 'svelte';
  import { getSelfErasureStatus } from '$lib/api/selfErasure';
  import { locale, t } from '$stores/locale';
  import { lastErasureOutcome, openErasureRequest } from '$stores/selfErasure';
  import { settingsModalStore } from '$stores/settingsModalStore';

  let height = 0;

  // Keeps `height` equal to the rendered height (the notice wraps on narrow screens).
  function measure(node: HTMLElement) {
    const update = () => (height = node.offsetHeight);
    update();
    if (typeof ResizeObserver === 'undefined') return {};
    const observer = new ResizeObserver(update);
    observer.observe(node);
    return { destroy: () => observer.disconnect() };
  }
  $: request = $openErasureRequest ?? null;
  $: date = request?.scheduled_for
    ? new Intl.DateTimeFormat($locale, { dateStyle: 'long', timeStyle: 'short' }).format(
        new Date(request.scheduled_for)
      )
    : '';

  // A block older than a week stays in the Settings card but no longer interrupts every page.
  const BLOCKED_NOTICE_DAYS = 7;
  $: blockedAt = $lastErasureOutcome?.at ? Date.parse($lastErasureOutcome.at) : NaN;
  $: showBlocked =
    !request &&
    $lastErasureOutcome?.reason === 'last_super_admin' &&
    Number.isFinite(blockedAt) &&
    Date.now() - blockedAt < BLOCKED_NOTICE_DAYS * 86_400_000;

  async function refresh() {
    try {
      const status = await getSelfErasureStatus();
      openErasureRequest.set(status.request);
      lastErasureOutcome.set(status.request ? null : (status.last_outcome ?? null));
    } catch {
      // A banner is not worth an error; the Settings card reports load failures itself.
      openErasureRequest.set(null);
    }
  }

  // A request can be blocked while the tab sits open; look again when the person returns.
  function onVisibility() {
    if (document.visibilityState === 'visible') void refresh();
  }

  // Fixed under the navbar (and a support banner), so the layout offsets its content by the
  // measured height, as for the support-access banner. In normal flow it sat BEHIND the fixed navbar.
  $: if (typeof document !== 'undefined') {
    document.documentElement.style.setProperty('--erasure-banner-height', `${showBanner ? height : 0}px`);
  }
  onDestroy(() => {
    if (typeof document !== 'undefined') {
      document.documentElement.style.removeProperty('--erasure-banner-height');
    }
  });
  $: showBanner = Boolean(request) || showBlocked;

  onMount(() => {
    void refresh();
    document.addEventListener('visibilitychange', onVisibility);
    return () => document.removeEventListener('visibilitychange', onVisibility);
  });
</script>

{#if showBanner}
  <div
    class="erasure-banner"
    role="status"
    data-testid="erasure-banner"
    use:measure
  >
    <span>
      {request ? $t('banner.accountErasure.message', { date }) : $t('banner.accountErasure.blocked')}
    </span>
    <button type="button" class="manage" on:click={() => settingsModalStore.open('profile')}>
      {$t('banner.accountErasure.manage')}
    </button>
  </div>
{/if}

<style>
  .erasure-banner {
    position: fixed;
    inset-inline: 0;
    top: calc(var(--navbar-height) + env(safe-area-inset-top, 0px) + var(--support-banner-height, 0px));
    z-index: 1150;
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: center;
    gap: 0.75rem;
    padding: 0.375rem 1rem;
    font-size: 0.8125rem;
    background: #fef3c7;
    color: #78350f;
    border-bottom: 1px solid #f59e0b;
  }

  :global([data-theme='dark']) .erasure-banner {
    background: #422006;
    color: #fde68a;
    border-bottom-color: #b45309;
  }

  .manage {
    background: none;
    border: none;
    padding: 0;
    cursor: pointer;
    color: inherit;
    text-decoration: underline;
    font: inherit;
  }
</style>
