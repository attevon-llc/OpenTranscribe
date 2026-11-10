<script lang="ts">
  import { SUPPORT_LEVEL_KEYS } from '$lib/i18n/keyMaps';
  import { onDestroy } from 'svelte';
  import { locale, t } from '$stores/locale';
  import { toastStore } from '$stores/toast';
  import { supportSession } from '$stores/supportSession';
  import StatusChip from './StatusChip.svelte';
  import ConfirmationModal from '$components/ConfirmationModal.svelte';
  import { SupportAccessApi } from '$lib/api/supportAccess';
  import { getErrorMessage } from '$lib/utils/apiError';
  import { formatClock, formatTimeOfDay } from '$lib/utils/formatting';

  /**
   * "You are acting inside someone else's workspace." Mounted by the root layout while a
   * support session is active. The ticking digits are `aria-hidden`; screen readers get a
   * separate live region that speaks only the start, the 5-minute and the 1-minute marks.
   */
  let height = 0;
  let confirmOpen = false;
  let revoking = false;

  $: s = $supportSession;
  $: breakGlass = s.mode === 'break_glass';
  $: expiresIso = new Date(s.expiresAtMs).toISOString();
  $: timeOfDay = formatTimeOfDay(expiresIso, $locale);

  // The layout offsets its content by this banner's measured height (it wraps on narrow screens).
  $: if (typeof document !== 'undefined') {
    document.documentElement.style.setProperty('--support-banner-height', `${height}px`);
  }
  onDestroy(() => {
    if (typeof document !== 'undefined') {
      document.documentElement.style.removeProperty('--support-banner-height');
    }
  });

  // The expiry sentence has two values that must stay left-to-right inside an RTL sentence,
  // so they are rendered as <bdi> elements between the translated fragments.
  const TIME = '@@TIME@@';
  const REMAINING = '@@REMAINING@@';
  $: expiryParts = $t('supportAccess.banner.expires', { time: TIME, remaining: REMAINING }).split(
    new RegExp(`(${TIME}|${REMAINING})`)
  );

  let announcement = '';
  let announcedFor: string | null = null;
  let fiveDone = false;
  let oneDone = false;
  function announce(grantUuid: string | null | undefined, remainingSeconds: number) {
    if (!grantUuid) return;
    if (announcedFor !== grantUuid) {
      announcedFor = grantUuid;
      fiveDone = false;
      oneDone = false;
      announcement = $t('supportAccess.announce.started', { time: timeOfDay });
    }
    if (!fiveDone && remainingSeconds <= 300) {
      fiveDone = true;
      announcement = $t('supportAccess.announce.fiveMinutes');
    }
    if (!oneDone && remainingSeconds <= 60) {
      oneDone = true;
      announcement = $t('supportAccess.announce.oneMinute');
    }
  }

  $: if (s.active) announce(s.grantUuid, s.remainingSeconds);

  async function revoke() {
    confirmOpen = false;
    if (!s.grantUuid) return;
    revoking = true;
    try {
      await SupportAccessApi.revokeGrant(s.grantUuid, {});
      await supportSession.end('revoked');
    } catch (err: unknown) {
      toastStore.error(getErrorMessage(err, $t('supportAccess.revoke.failed')));
    } finally {
      revoking = false;
    }
  }
</script>

<section
  class="banner"
  class:break-glass={breakGlass}
  aria-label={$t('supportAccess.banner.region')}
  bind:offsetHeight={height}
>
  <div class="message">
    <span class="what">
      {#if s.targetKind === 'organization'}
        {$t('supportAccess.banner.org', { org: s.targetLabel })}
      {:else}
        {$t('supportAccess.banner.personal', { name: s.targetLabel })}
      {/if}
    </span>
    {#if breakGlass}<StatusChip tone="error">{$t('supportAccess.mode.break_glass')}</StatusChip>{/if}
    <StatusChip tone={s.level === 'read' ? 'warning' : 'info'}>
      {$t(SUPPORT_LEVEL_KEYS[s.level])}
    </StatusChip>
    <span class="expires">
      {#each expiryParts as part, i (i)}
        {#if part === TIME}
          <bdi>{timeOfDay}</bdi>
        {:else if part === REMAINING}
          <bdi aria-hidden="true">{formatClock(s.remainingSeconds)}</bdi>
        {:else}
          {part}
        {/if}
      {/each}
    </span>
  </div>
  <div class="buttons">
    <button type="button" class="btn btn-secondary" on:click={() => supportSession.end('user')}>
      {$t('supportAccess.action.endSession')}
    </button>
    <button
      type="button"
      class="btn btn-danger sa-danger"
      disabled={revoking}
      on:click={() => (confirmOpen = true)}
    >
      {$t('supportAccess.action.revoke')}
    </button>
  </div>
  <div
    class="sr-only"
    data-testid="support-announcer"
    aria-live={breakGlass ? 'assertive' : 'polite'}
    aria-atomic="true"
  >{announcement}</div>
</section>

<ConfirmationModal
  isOpen={confirmOpen}
  title={$t('supportAccess.revoke.title')}
  message={$t('supportAccess.revoke.body', { name: s.targetLabel })}
  on:confirm={revoke}
  on:cancel={() => (confirmOpen = false)}
  on:close={() => (confirmOpen = false)}
/>

<style>
  .banner {
    position: fixed;
    inset-inline: 0;
    top: calc(var(--banner-offset, 0px) + var(--navbar-height));
    z-index: calc(var(--z-navbar) - 1);
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-2) var(--space-4);
    min-height: 40px;
    padding: 0.35rem var(--space-4);
    box-sizing: border-box;
    background: var(--warning-bg);
    border-bottom: 2px solid var(--warning-color);
    color: var(--text-color);
    font-size: var(--font-size-sm);
  }
  .banner.break-glass {
    background: var(--error-background);
    border-bottom-color: var(--error-color);
  }
  .message {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-1) 0.6rem;
    min-width: 0;
  }
  .what {
    font-weight: var(--font-weight-semibold);
    overflow-wrap: anywhere;
  }
  .buttons {
    display: flex;
    gap: var(--space-2);
  }
  .buttons .btn {
    padding: var(--space-1) 0.7rem;
    font-size: var(--font-size-xs);
  }
  @media (max-width: 768px) {
    .buttons .btn {
      min-height: 44px;
    }
  }
</style>
