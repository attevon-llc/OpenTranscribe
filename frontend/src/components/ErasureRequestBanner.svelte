<script lang="ts">
  /**
   * App-level notice while the signed-in person has an open self-service erasure request
   * (issue #1354). The account stays usable during the grace period; this only says so and
   * opens the Settings card where the request can be cancelled. Reads once on mount; the
   * Settings card keeps `openErasureRequest` current after that.
   */
  import { onMount } from 'svelte';
  import { getSelfErasureStatus } from '$lib/api/selfErasure';
  import { locale, t } from '$stores/locale';
  import { openErasureRequest } from '$stores/selfErasure';
  import { settingsModalStore } from '$stores/settingsModalStore';

  $: request = $openErasureRequest ?? null;
  $: date = request?.scheduled_for
    ? new Intl.DateTimeFormat($locale, { dateStyle: 'long' }).format(new Date(request.scheduled_for))
    : '';

  onMount(() => {
    (async () => {
      try {
        const status = await getSelfErasureStatus();
        openErasureRequest.set(status.request);
      } catch {
        // A banner is not worth an error; the Settings card reports load failures itself.
        openErasureRequest.set(null);
      }
    })();
  });
</script>

{#if request}
  <div class="erasure-banner" role="status" data-testid="erasure-banner">
    <span>{$t('banner.accountErasure.message', { date })}</span>
    <button type="button" class="manage" on:click={() => settingsModalStore.open('profile')}>
      {$t('banner.accountErasure.manage')}
    </button>
  </div>
{/if}

<style>
  .erasure-banner {
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
