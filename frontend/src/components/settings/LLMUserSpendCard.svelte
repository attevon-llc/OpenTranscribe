<!--
  Admin view of LLM spend per user (issue #790): tokens today and this UTC month from the
  enforcement counters, and an estimated cost. Counts and money only; no conversation content.
-->
<script lang="ts">
  import { onMount } from 'svelte';
  import SettingsCard from '$components/settings/transcription/SettingsCard.svelte';
  import SettingsSkeleton from '$components/ui/SettingsSkeleton.svelte';
  import { getLlmSpendPerUser, type LlmUserSpendReport } from '$lib/api/llmUsage';
  import { formatEstimatedCost, formatTokens } from '$lib/settings/llmUsageView';
  import { getErrorMessage } from '$lib/utils/apiError';
  import { toastStore } from '$stores/toast';
  import { t, locale } from '$stores/locale';

  let report: LlmUserSpendReport | null = null;
  let loading = true;

  onMount(async () => {
    try {
      report = await getLlmSpendPerUser();
    } catch (err: unknown) {
      toastStore.error(getErrorMessage(err, $t('settings.llmUsage.perUser.loadFailed')));
    } finally {
      loading = false;
    }
  });
</script>

<SettingsCard
  title={$t('settings.llmUsage.perUser.title')}
  description={$t('settings.llmUsage.perUser.desc')}
>
  {#if loading}
    <SettingsSkeleton variant="form" count={3} label={$t('common.loading')} />
  {:else if report && report.users.length === 0}
    <p class="hint" data-testid="llm-user-spend-empty">{$t('settings.llmUsage.perUser.empty')}</p>
  {:else if report}
    <table class="spend-table" data-testid="llm-user-spend-table">
      <thead>
        <tr>
          <th scope="col">{$t('settings.llmUsage.perUser.col.user')}</th>
          <th scope="col" class="num">{$t('settings.llmUsage.perUser.col.today')}</th>
          <th scope="col" class="num">{$t('settings.llmUsage.perUser.col.month')}</th>
          <th scope="col" class="num">{$t('settings.llmUsage.col.cost')}</th>
        </tr>
      </thead>
      <tbody>
        {#each report.users as row (row.user_uuid)}
          <tr>
            <td>
              {#if row.full_name}<span class="name">{row.full_name}</span>{/if}
              <span class="email">{row.email}</span>
            </td>
            <td class="num">{formatTokens(row.tokens_today, $locale)}</td>
            <td class="num">{formatTokens(row.tokens_this_month, $locale)}</td>
            <td class="num">
              {formatEstimatedCost(row.estimated_cost_usd, $locale) ??
                $t('settings.llmUsage.unpriced')}
              {#if row.cost_incomplete && row.estimated_cost_usd !== null}
                <span class="partial" title={$t('settings.llmUsage.perUser.partial')} aria-hidden="true"
                  >*</span
                >
                <span class="sr-only" data-testid="llm-user-spend-partial">
                  {$t('settings.llmUsage.perUser.partial')}
                </span>
              {/if}
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
    <p class="hint">
      {$t('settings.llmUsage.perUser.footnote')}
      {$t('settings.llmUsage.ratesVerified', { date: report.rates_verified_on })}
    </p>
  {/if}
</SettingsCard>

<style>
  .hint {
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
    margin: var(--space-2) 0 0;
  }

  .spend-table {
    width: 100%;
    border-collapse: collapse;
    font-size: var(--font-size-md);
    color: var(--text-color);
  }

  .spend-table th,
  .spend-table td {
    padding: var(--space-1-5) var(--space-2);
    border-bottom: 1px solid var(--border-color);
    text-align: start;
  }

  .spend-table th {
    font-weight: var(--font-weight-semibold);
    color: var(--text-secondary);
  }

  .spend-table .num {
    text-align: end;
    font-variant-numeric: tabular-nums;
  }

  .name {
    display: block;
  }

  .email {
    display: block;
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
    overflow-wrap: anywhere;
  }

  .partial {
    color: var(--color-warning-text);
    cursor: help;
  }
</style>
