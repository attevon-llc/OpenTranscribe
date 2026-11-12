<script lang="ts">
  /**
   * First-login opt-in prompt for the anonymous usage report (issue #1019), super_admin only.
   *
   * Shown once, and only when the server would actually send the report if it were turned on:
   * nothing forces it off (DO_NOT_TRACK, offline, PKI, FIPS, demo, no receiver) and it is not
   * on yet. "No thanks" is remembered in this browser; the Settings card stays the place to
   * change the choice later. Fixed in a corner so it never shifts the page layout.
   */
  import { onMount } from 'svelte';
  import { getTelemetryStatus, setTelemetryEnabled } from '$lib/api/telemetry';
  import { t } from '$stores/locale';
  import { toastStore } from '$stores/toast';
  import { settingsModalStore } from '$stores/settingsModalStore';

  const STORAGE_KEY = 'usage-report-prompt';

  let visible = false;
  let busy = false;

  function alreadyAnswered(): boolean {
    try {
      return localStorage.getItem(STORAGE_KEY) !== null;
    } catch {
      return true;
    }
  }

  function remember(answer: string) {
    try {
      localStorage.setItem(STORAGE_KEY, answer);
    } catch {
      // Private mode: the prompt may show again next session, which is harmless.
    }
  }

  onMount(async () => {
    if (alreadyAnswered()) return;
    try {
      const status = await getTelemetryStatus();
      visible = !status.enabled && status.off_reasons.length === 0;
    } catch {
      visible = false;
    }
  });

  function dismiss() {
    remember('dismissed');
    visible = false;
  }

  async function enable() {
    if (busy) return;
    busy = true;
    try {
      await setTelemetryEnabled(true);
      remember('enabled');
      visible = false;
      toastStore.success($t('settings.usageReport.turnedOn'));
    } catch {
      toastStore.error($t('settings.usageReport.saveFailed'));
    } finally {
      busy = false;
    }
  }

  function details() {
    remember('dismissed');
    visible = false;
    settingsModalStore.open('redaction-policy');
  }
</script>

{#if visible}
  <div class="prompt" role="dialog" aria-labelledby="usage-report-prompt-title" data-testid="usage-report-prompt">
    <h2 id="usage-report-prompt-title" class="title">{$t('settings.usageReport.prompt.title')}</h2>
    <p class="body">{$t('settings.usageReport.prompt.body')}</p>
    <div class="actions">
      <button type="button" class="btn btn-primary btn-sm" disabled={busy} data-testid="usage-report-prompt-enable" on:click={enable}>
        {$t('settings.usageReport.prompt.enable')}
      </button>
      <button type="button" class="btn btn-secondary btn-sm" data-testid="usage-report-prompt-dismiss" on:click={dismiss}>
        {$t('settings.usageReport.prompt.dismiss')}
      </button>
      <button type="button" class="link" data-testid="usage-report-prompt-details" on:click={details}>
        {$t('settings.usageReport.prompt.details')}
      </button>
    </div>
  </div>
{/if}

<style>
  .prompt {
    position: fixed;
    inset-inline-end: var(--space-4);
    inset-block-end: var(--space-4);
    z-index: var(--z-navbar);
    max-width: 24rem;
    padding: var(--space-4);
    background: var(--surface-color);
    color: var(--text-color);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-md);
    box-shadow: var(--shadow-lg);
  }

  .title {
    margin: 0 0 var(--space-2);
    font-size: var(--font-size-base);
  }

  .body {
    margin: 0 0 var(--space-3);
    font-size: var(--font-size-sm);
  }

  .actions {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-2);
  }

  .link {
    background: none;
    border: none;
    padding: 0;
    cursor: pointer;
    color: inherit;
    text-decoration: underline;
    font: inherit;
    font-size: var(--font-size-sm);
  }
</style>
