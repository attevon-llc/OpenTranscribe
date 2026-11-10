<!--
  UsageReportCard.svelte — the opt-in anonymous usage report (issue #1019, super_admin).

  Off by default. Shows the switch, why it is forced off (DO_NOT_TRACK, OPENTRANSCRIBE_TELEMETRY,
  demo mode, no receiver configured), when it last sent, and the exact request body the server
  would send, which the server builds with the same function it sends with. The switch is
  CONTROLLED (`PrivacySwitchCard`): a refused change leaves it where the server says it is.
  Mounted on the Privacy & Redaction "policy" tab, which is already super_admin only.
-->
<script lang="ts">
  import { onMount } from 'svelte';
  import SettingsSkeleton from '$components/ui/SettingsSkeleton.svelte';
  import PrivacySwitchCard from '$components/privacy/PrivacySwitchCard.svelte';
  import { getTelemetryStatus, setTelemetryEnabled, type TelemetryStatus } from '$lib/api/telemetry';
  import {
    usageReportBadge,
    usageReportBadgeKey,
    usageReportPreviewText,
    usageReportReasonKeys,
  } from '$lib/settings/usageReport';
  import { formatDateTime } from '$lib/utils/formatting';
  import { t, locale } from '$stores/locale';
  import { toastStore } from '$stores/toast';

  let status: TelemetryStatus | null = null;
  let loading = true;
  let loadFailed = false;
  let busy = false;

  $: reasonKeys = usageReportReasonKeys(status?.off_reasons ?? []);
  $: badge = usageReportBadge(status);
  $: preview = usageReportPreviewText(status);
  // A stored "on" that something forces off can still be switched off, never on.
  $: locked = !!status && reasonKeys.length > 0 && !status.enabled;

  onMount(load);

  async function load() {
    loading = true;
    loadFailed = false;
    try {
      const loaded = await getTelemetryStatus();
      // An answer without the contract's shape is a failure to load, never a guess.
      if (!Array.isArray(loaded?.off_reasons)) throw new Error('unexpected usage report status');
      status = loaded;
    } catch {
      console.error('Failed to load the usage report setting');
      loadFailed = true;
    } finally {
      loading = false;
    }
  }

  async function toggle(next: boolean) {
    if (!status || busy) return;
    busy = true;
    try {
      status = await setTelemetryEnabled(next);
      toastStore.success($t(next ? 'settings.usageReport.turnedOn' : 'settings.usageReport.turnedOff'));
    } catch (error) {
      const response = (error as { response?: { status?: number } }).response;
      toastStore.error(
        $t(response?.status === 409 ? 'settings.usageReport.refused' : 'settings.usageReport.saveFailed')
      );
      await load();
    } finally {
      busy = false;
    }
  }
</script>

<section class="usage-report" data-testid="usage-report-card" aria-labelledby="usage-report-heading">
  <div class="head">
    <h3 id="usage-report-heading" class="heading">{$t('settings.usageReport.heading')}</h3>
    {#if !loading && status}
      <span class="chip chip-{badge}" data-testid="usage-report-status">
        {$t(usageReportBadgeKey(badge))}
      </span>
    {/if}
  </div>

  {#if loading}
    <SettingsSkeleton />
  {:else if loadFailed || !status}
    <p class="note warn" role="alert" data-testid="usage-report-load-failed">
      {$t('settings.usageReport.loadFailed')}
    </p>
  {:else}
    <PrivacySwitchCard
      id="usage-report"
      title={$t('settings.usageReport.switchTitle')}
      description={$t('settings.usageReport.description')}
      checked={status.enabled}
      {locked}
      lockedReason={$t('settings.usageReport.lockedReason')}
      {busy}
      onToggle={toggle}
    />

    {#if reasonKeys.length > 0}
      <ul class="reasons" data-testid="usage-report-reasons">
        {#each reasonKeys as key (key)}
          <li>{$t(key)}</li>
        {/each}
      </ul>
    {/if}

    <p class="meta" data-testid="usage-report-last-sent">
      {status.last_sent_at
        ? $t('settings.usageReport.lastSent', { time: formatDateTime(status.last_sent_at, '', $locale) })
        : $t('settings.usageReport.neverSent')}
    </p>
    <p class="meta">
      {$t('settings.usageReport.destination')}
      <code dir="ltr" data-testid="usage-report-endpoint">{status.endpoint}</code>
    </p>

    <h4 class="preview-heading">{$t('settings.usageReport.previewHeading')}</h4>
    <p class="meta">{$t('settings.usageReport.previewNote')}</p>
    <pre class="preview" dir="ltr" data-testid="usage-report-preview">{preview}</pre>
  {/if}
</section>

<style>
  .usage-report {
    max-width: 800px;
    margin-top: var(--space-6);
  }

  .head {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    flex-wrap: wrap;
    margin-bottom: var(--space-2);
  }

  .heading {
    margin: 0;
    font-size: var(--font-size-md);
    font-weight: var(--font-weight-semibold);
    color: var(--text-color);
  }

  .chip {
    margin-inline-start: auto;
    padding: var(--space-0-5) var(--space-2-5);
    border-radius: var(--radius-pill);
    border: 1px solid var(--border-color);
    font-size: var(--font-size-xs);
    font-weight: var(--font-weight-medium);
    color: var(--text-secondary);
  }

  .chip-on {
    color: var(--success-text);
    border-color: var(--success-color);
  }

  .chip-blocked {
    color: var(--warning-text);
    border-color: var(--warning-color);
  }

  .reasons {
    margin: var(--space-3) 0 0;
    padding-inline-start: var(--space-5);
    font-size: var(--font-size-sm);
    color: var(--text-color);
  }

  .meta,
  .note {
    margin: var(--space-2) 0 0;
    font-size: var(--font-size-sm);
    color: var(--text-muted);
  }

  .note.warn {
    padding: var(--space-2) var(--space-3);
    border-radius: var(--radius-md);
    border: 1px solid var(--warning-color);
    color: var(--text-color);
  }

  .meta code {
    font-size: var(--font-size-xs);
    color: var(--text-color);
  }

  .preview-heading {
    margin: var(--space-4) 0 0;
    font-size: var(--font-size-md);
    font-weight: var(--font-weight-semibold);
    color: var(--text-color);
  }

  .preview {
    margin: var(--space-2) 0 0;
    padding: var(--space-3);
    overflow-x: auto;
    border: 1px solid var(--border-color);
    border-radius: var(--radius-md);
    background: var(--background-color);
    color: var(--text-color);
    font-size: var(--font-size-xs);
    line-height: 1.4;
    text-align: start;
  }
</style>
