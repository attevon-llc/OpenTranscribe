<!--
  AI usage (issue #790): the caller's LLM tokens today against their daily limit, and the last
  30 days by feature with an estimated cost. Admins also get the limit editor
  (LLMBudgetAdminCard). Limits are tokens; dollars are only ever an estimate.
-->
<script lang="ts">
  import { onMount } from 'svelte';
  import SettingsCard from '$components/settings/transcription/SettingsCard.svelte';
  import SettingsSkeleton from '$components/ui/SettingsSkeleton.svelte';
  import ProgressBar from '$components/ui/ProgressBar.svelte';
  import LLMBudgetAdminCard from '$components/settings/LLMBudgetAdminCard.svelte';
  import { getMyUsage, type MyUsage } from '$lib/api/llmUsage';
  import { LLM_FEATURE_KEYS, labelFor } from '$lib/i18n/keyMaps';
  import { formatEstimatedCost, formatTokens, percentUsed } from '$lib/settings/llmUsageView';
  import { formatDateTime } from '$lib/utils/formatting';
  import { t, locale } from '$stores/locale';
  import './transcription/formLayout.css';
  import './transcription/formControls.css';

  export let isAdmin = false;

  let usage: MyUsage | null = null;
  let loading = true;
  let failed = false;

  onMount(load);

  async function load(): Promise<void> {
    loading = true;
    failed = false;
    try {
      usage = await getMyUsage(30);
    } catch {
      failed = true;
    } finally {
      loading = false;
    }
  }

  $: budget = usage?.budget ?? null;
  $: dailyPercent = budget ? percentUsed(budget.used_today, budget.user_daily_tokens) : null;
  $: totalCost = usage ? formatEstimatedCost(usage.estimated_cost_usd, $locale) : null;
</script>

<div class="llm-usage tx-form">
  <div class="settings-form">
    <SettingsCard
      title={$t('settings.llmUsage.todayTitle')}
      description={$t('settings.llmUsage.todayDesc')}
    >
      {#if loading}
        <SettingsSkeleton variant="dashboard" count={2} label={$t('common.loading')} />
      {:else if failed || !usage || !budget}
        <p class="notice" role="alert">{$t('settings.llmUsage.loadFailed')}</p>
        <button type="button" class="btn btn-secondary" on:click={load}>
          {$t('settings.llmUsage.retry')}
        </button>
      {:else}
        {#if budget.user_daily_tokens > 0}
          <ProgressBar
            percent={dailyPercent}
            label={$t('settings.llmUsage.usedOfLimit', {
              used: formatTokens(budget.used_today, $locale),
              limit: formatTokens(budget.user_daily_tokens, $locale),
            })}
          />
          <p class="hint">
            {$t('settings.llmUsage.resetsAt', {
              time: formatDateTime(budget.day_resets_at, '', $locale),
            })}
          </p>
        {:else}
          <p class="figure" data-testid="llm-usage-today">
            {$t('settings.llmUsage.usedUnlimited', { used: formatTokens(budget.used_today, $locale) })}
          </p>
        {/if}
        {#if budget.instance_limit_reached}
          <p class="notice" role="status">{$t('settings.llmUsage.instanceLimitReached')}</p>
        {/if}
      {/if}
    </SettingsCard>

    {#if usage && !loading}
      <SettingsCard
        title={$t('settings.llmUsage.byFeatureTitle', { days: usage.window_days })}
        description={$t('settings.llmUsage.byFeatureDesc')}
      >
        {#if usage.by_feature.length === 0}
          <p class="hint">{$t('settings.llmUsage.none')}</p>
        {:else}
          <table class="usage-table">
            <thead>
              <tr>
                <th scope="col">{$t('settings.llmUsage.col.feature')}</th>
                <th scope="col" class="num">{$t('settings.llmUsage.col.tokens')}</th>
                <th scope="col" class="num">{$t('settings.llmUsage.col.cost')}</th>
              </tr>
            </thead>
            <tbody>
              {#each usage.by_feature as row (row.event_type)}
                <tr>
                  <td>{labelFor($t, LLM_FEATURE_KEYS, row.event_type)}</td>
                  <td class="num">{formatTokens(row.total_tokens, $locale)}</td>
                  <td class="num">
                    {formatEstimatedCost(row.estimated_cost_usd, $locale) ??
                      $t('settings.llmUsage.unpriced')}
                  </td>
                </tr>
              {/each}
            </tbody>
          </table>
          <p class="hint" data-testid="llm-usage-cost">
            {#if totalCost !== null}
              {usage.cost_incomplete
                ? $t('settings.llmUsage.costPartial', { cost: totalCost })
                : $t('settings.llmUsage.costEstimate', { cost: totalCost })}
            {:else}
              {$t('settings.llmUsage.costUnknown')}
            {/if}
            {$t('settings.llmUsage.ratesVerified', { date: usage.rates_verified_on })}
          </p>
        {/if}
      </SettingsCard>
    {/if}

    {#if isAdmin}
      <LLMBudgetAdminCard on:saved={load} />
    {/if}
  </div>
</div>

<style>
  .figure {
    font-size: var(--font-size-base);
    color: var(--text-color);
    margin: 0;
  }

  .hint {
    font-size: var(--font-size-sm);
    color: var(--text-muted);
    margin: var(--space-2) 0 0;
  }

  .notice {
    font-size: var(--font-size-md);
    color: var(--color-warning-text);
    margin: var(--space-2) 0 0;
  }

  .usage-table {
    width: 100%;
    border-collapse: collapse;
    font-size: var(--font-size-md);
    color: var(--text-color);
  }

  .usage-table th,
  .usage-table td {
    padding: var(--space-1-5) var(--space-2);
    border-bottom: 1px solid var(--border-color);
    text-align: start;
  }

  .usage-table th {
    font-weight: var(--font-weight-semibold);
    color: var(--text-muted);
  }

  .usage-table .num {
    text-align: end;
    font-variant-numeric: tabular-nums;
  }
</style>
