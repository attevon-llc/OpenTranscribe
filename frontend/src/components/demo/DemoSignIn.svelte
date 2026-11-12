<script lang="ts">
  /**
   * "Try the demo" on the login page of a public read-only demo (issue #628, D5): signs in as
   * the shared demo user without a password, so a visitor can never be locked out of a
   * published credential. Also says which release the demo runs and when it was upgraded.
   * Rendered by the login page only when `/auth/methods` reports `demo_mode`.
   */
  import { onMount } from 'svelte';
  import { t } from '$stores/locale';
  import { toastStore } from '$stores/toast';
  import { loginAsDemo } from '$stores/auth';
  import { demoInfo, loadDemoInfo } from '$stores/demo';
  import { getServerVersion } from '$lib/api/version';
  import { formatDateTime } from '$lib/utils/formatting';
  import Spinner from '$components/ui/Spinner.svelte';

  export let disabled = false;
  /** Called once the demo session exists; the page owns navigation. */
  export let onSignedIn: () => void = () => {};

  let busy = false;
  let version = '';

  $: upgraded = $demoInfo?.upgraded_at ? formatDateTime($demoInfo.upgraded_at) : '';
  $: buildInfo = !version
    ? ''
    : upgraded
      ? $t('demo.buildInfo', { version, time: upgraded })
      : $t('demo.buildInfoNoTime', { version });

  async function start() {
    busy = true;
    const result = await loginAsDemo();
    busy = false;
    if (result.success) onSignedIn();
    else toastStore.error(result.message || $t('demo.signInFailed'));
  }

  onMount(() => {
    void loadDemoInfo();
    getServerVersion()
      .then((v) => (version = v.version))
      .catch(() => {});
  });
</script>

<div class="demo-signin" data-testid="demo-signin">
  <button
    type="button"
    class="demo-button"
    on:click={start}
    disabled={busy || disabled}
    aria-busy={busy}
  >
    {#if busy}<Spinner size="small" color="white" />{/if}
    <span>{$t('demo.tryDemo')}</span>
  </button>
  <p class="demo-hint">{$t('demo.tryDemoHint')}</p>
  {#if buildInfo}
    <p class="demo-build" data-testid="demo-build-info">{buildInfo}</p>
  {/if}
</div>

<style>
  .demo-signin {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
    margin-bottom: var(--space-5);
    padding: var(--space-4);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-lg);
    background-color: var(--background-color);
    text-align: center;
  }

  .demo-button {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: var(--space-2);
    width: 100%;
    padding: var(--space-2-5) var(--space-5);
    border: none;
    border-radius: var(--radius-lg);
    background-color: var(--primary-color);
    color: var(--text-on-solid);
    font-size: var(--font-size-base);
    font-weight: var(--font-weight-semibold);
    cursor: pointer;
  }

  .demo-button:disabled {
    background-color: var(--border-color);
    cursor: not-allowed;
  }

  .demo-button:hover:not(:disabled) {
    background-color: var(--primary-hover);
  }

  .demo-hint,
  .demo-build {
    margin: 0;
    font-size: var(--font-size-md);
    color: var(--text-secondary);
  }

  .demo-build {
    font-size: var(--font-size-sm);
  }
</style>
