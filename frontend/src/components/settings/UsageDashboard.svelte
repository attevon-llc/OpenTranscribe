<!--
  UsageDashboard.svelte — cloud-edition usage dashboard (org admins only).

  Shows used / limit / remaining transcription hours for the current billing
  period plus a per-member breakdown, from GET /api/billing/usage. Reuses the
  shared ProgressBar for the quota bar. Cloud edition only (gated upstream in
  SettingsModal by cap:usage_dashboard + audience=org_admin).
-->
<script lang="ts">
  import { onMount } from 'svelte';
  import { BillingApi, type BillingUsage } from '$lib/api/billing';
  import { usageStore } from '$stores/cloudBilling';
  import { toastStore } from '$stores/toast';
  import { t } from '$stores/locale';
  import { getErrorMessage } from '$lib/utils/apiError';
  import { formatDate } from '$lib/utils/formatting';
  import Spinner from '$components/ui/Spinner.svelte';
  import ProgressBar from '$components/ui/ProgressBar.svelte';
  import EmptyState from '$components/ui/EmptyState.svelte';

  let usage: BillingUsage | null = null;
  let loading = true;

  onMount(() => {
    const controller = new AbortController();
    (async () => {
      await loadUsage();
    })();
    return () => controller.abort();
  });

  async function loadUsage() {
    loading = true;
    try {
      usage = await BillingApi.getUsage();
      usageStore.set({
        loaded: true,
        hours_used: usage.hours_used ?? 0,
        limit: usage.limit ?? null,
        remaining: usage.remaining ?? null,
        files_this_month: usage.files_this_month ?? 0,
      });
    } catch (err: unknown) {
      toastStore.error(getErrorMessage(err, $t('usage.toast.loadFailed')));
    } finally {
      loading = false;
    }
  }

  // Rounded to one decimal for display.
  const round1 = (n: number) => Math.round(n * 10) / 10;

  $: percent =
    usage && usage.limit && usage.limit > 0
      ? Math.min(100, (usage.hours_used / usage.limit) * 100)
      : null;

  // Bar color shifts to warning/error as the quota fills.
  $: barColor =
    percent === null
      ? ''
      : percent >= 100
        ? 'var(--error-color, #dc2626)'
        : percent >= 80
          ? 'var(--warning-color, #d97706)'
          : 'var(--primary-color)';
</script>

<div class="usage-dashboard">
  {#if loading}
    <div class="loading-state">
      <Spinner size="medium" />
      <p>{$t('usage.loading')}</p>
    </div>
  {:else if usage}
    <!-- Period -->
    {#if usage.period_start && usage.period_end}
      <p class="usage-period">
        {$t('usage.period', {
          start: formatDate(usage.period_start),
          end: formatDate(usage.period_end),
        })}
      </p>
    {/if}

    <!-- Quota summary cards -->
    <div class="usage-stats">
      <div class="stat-card">
        <span class="stat-value">{round1(usage.hours_used)}</span>
        <span class="stat-label">{$t('usage.hoursUsed')}</span>
      </div>
      <div class="stat-card">
        <span class="stat-value">{usage.limit !== null ? round1(usage.limit) : $t('usage.unlimited')}</span>
        <span class="stat-label">{$t('usage.hoursLimit')}</span>
      </div>
      <div class="stat-card">
        <span class="stat-value">{usage.remaining !== null ? round1(usage.remaining) : '—'}</span>
        <span class="stat-label">{$t('usage.hoursRemaining')}</span>
      </div>
      <div class="stat-card">
        <span class="stat-value">{usage.files_this_month}</span>
        <span class="stat-label">{$t('usage.filesThisMonth')}</span>
      </div>
    </div>

    <!-- Quota bar -->
    {#if percent !== null}
      <ProgressBar
        {percent}
        color={barColor}
        label={$t('usage.quotaLabel', { used: round1(usage.hours_used), limit: round1(usage.limit ?? 0) })}
      />
    {/if}

    <!-- Per-member breakdown -->
    <div class="member-section">
      <h4 class="member-title">{$t('usage.byMember')}</h4>
      {#if usage.members.length === 0}
        <EmptyState title={$t('usage.noMembers')} padding="1.5rem 1rem" />
      {:else}
        <table class="member-table">
          <thead>
            <tr>
              <th>{$t('usage.member')}</th>
              <th class="num">{$t('usage.hours')}</th>
              <th class="num">{$t('usage.files')}</th>
            </tr>
          </thead>
          <tbody>
            {#each usage.members as member (member.user_uuid)}
              <tr>
                <td>
                  <span class="member-name">{member.full_name || member.email}</span>
                  {#if member.full_name}<span class="member-email">{member.email}</span>{/if}
                </td>
                <td class="num">{round1(member.hours_used)}</td>
                <td class="num">{member.files_count}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      {/if}
    </div>
  {:else}
    <div class="error-state">
      <p>{$t('usage.loadFailed')}</p>
      <button class="btn btn-secondary" on:click={loadUsage}>{$t('usage.retry')}</button>
    </div>
  {/if}
</div>

<style>
  .usage-dashboard {
    display: flex;
    flex-direction: column;
    gap: 1.25rem;
  }

  .loading-state {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 0.75rem;
    padding: 2rem 0;
    color: var(--text-secondary);
    font-size: 0.8125rem;
  }

  .loading-state p {
    margin: 0;
  }

  .usage-period {
    margin: 0;
    font-size: 0.8125rem;
    color: var(--text-secondary);
  }

  .usage-stats {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
    gap: 0.75rem;
  }

  .stat-card {
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
    padding: 0.875rem 1rem;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    background: var(--surface-color);
  }

  .stat-value {
    font-size: 1.5rem;
    font-weight: 700;
    color: var(--text-color);
    line-height: 1;
  }

  .stat-label {
    font-size: 0.75rem;
    color: var(--text-secondary);
  }

  .member-section {
    display: flex;
    flex-direction: column;
    gap: 0.625rem;
  }

  .member-title {
    margin: 0;
    font-size: 0.8125rem;
    font-weight: 600;
    color: var(--text-secondary);
    text-transform: uppercase;
    letter-spacing: 0.03em;
  }

  .member-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.8125rem;
  }

  .member-table th,
  .member-table td {
    text-align: left;
    padding: 0.5rem 0.625rem;
    border-bottom: 1px solid var(--border-color);
  }

  .member-table th {
    font-weight: 600;
    color: var(--text-secondary);
    font-size: 0.75rem;
    text-transform: uppercase;
    letter-spacing: 0.03em;
  }

  .member-table .num {
    text-align: right;
    font-variant-numeric: tabular-nums;
  }

  .member-name {
    display: block;
    color: var(--text-color);
    font-weight: 500;
  }

  .member-email {
    display: block;
    color: var(--text-secondary);
    font-size: 0.6875rem;
  }

  .error-state {
    text-align: center;
    padding: 1.5rem 0;
    color: var(--error-color, #dc2626);
    font-size: 0.8125rem;
  }

  .error-state p {
    margin: 0 0 0.75rem;
  }

  .btn {
    padding: 0.6rem 1.2rem;
    border-radius: 10px;
    border: 1px solid var(--border-color);
    background-color: var(--surface-color);
    color: var(--text-color);
    font-size: 0.8125rem;
    font-weight: 500;
    cursor: pointer;
    transition: all 0.2s ease;
  }

  .btn-secondary:hover {
    background-color: var(--button-hover);
  }
</style>
