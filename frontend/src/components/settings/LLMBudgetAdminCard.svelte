<!--
  Admin LLM token limits (issue #790, D13): tokens per user per UTC day and per instance per UTC
  month, over every AI feature; 0 = unlimited. Local models are counted unless exempted here.
-->
<script lang="ts">
  import { createEventDispatcher, onMount } from 'svelte';
  import SettingsCard from '$components/settings/transcription/SettingsCard.svelte';
  import SettingsSkeleton from '$components/ui/SettingsSkeleton.svelte';
  import ProgressBar from '$components/ui/ProgressBar.svelte';
  import { getLlmBudget, updateLlmBudget, type LlmBudgetSettings } from '$lib/api/llmUsage';
  import { formatTokens, parseLimit, percentUsed } from '$lib/settings/llmUsageView';
  import { getErrorMessage } from '$lib/utils/apiError';
  import { toastStore } from '$stores/toast';
  import { t, locale } from '$stores/locale';

  const dispatch = createEventDispatcher<{ saved: LlmBudgetSettings }>();

  let current: LlmBudgetSettings | null = null;
  let loading = true;
  let saving = false;
  let dailyInput: string | number | null = '0';
  let monthlyInput: string | number | null = '0';
  let localUnlimited = false;

  onMount(async () => {
    try {
      apply(await getLlmBudget());
    } catch (err: unknown) {
      toastStore.error(getErrorMessage(err, $t('settings.llmUsage.admin.loadFailed')));
    } finally {
      loading = false;
    }
  });

  function apply(settings: LlmBudgetSettings): void {
    current = settings;
    dailyInput = String(settings.user_daily_tokens);
    monthlyInput = String(settings.instance_monthly_tokens);
    localUnlimited = settings.local_unlimited;
  }

  $: daily = parseLimit(dailyInput);
  $: monthly = parseLimit(monthlyInput);
  $: invalid = daily === null || monthly === null;
  $: dirty =
    current !== null &&
    (daily !== current.user_daily_tokens ||
      monthly !== current.instance_monthly_tokens ||
      localUnlimited !== current.local_unlimited);
  $: monthPercent = current
    ? percentUsed(current.instance_used_this_month, current.instance_monthly_tokens)
    : null;

  async function save(): Promise<void> {
    if (invalid || daily === null || monthly === null) return;
    saving = true;
    try {
      const saved = await updateLlmBudget({
        user_daily_tokens: daily,
        instance_monthly_tokens: monthly,
        local_unlimited: localUnlimited,
      });
      apply(saved);
      toastStore.success($t('settings.llmUsage.admin.saved'));
      dispatch('saved', saved);
    } catch (err: unknown) {
      toastStore.error(getErrorMessage(err, $t('settings.llmUsage.admin.saveFailed')));
    } finally {
      saving = false;
    }
  }
</script>

<SettingsCard
  title={$t('settings.llmUsage.admin.title')}
  description={$t('settings.llmUsage.admin.desc')}
  tooltip={$t('settings.llmUsage.admin.tooltip')}
>
  {#if loading}
    <SettingsSkeleton variant="form" count={3} label={$t('common.loading')} />
  {:else if current}
    {#if current.instance_monthly_tokens > 0}
      <ProgressBar
        percent={monthPercent}
        label={$t('settings.llmUsage.admin.monthUsed', {
          used: formatTokens(current.instance_used_this_month, $locale),
          limit: formatTokens(current.instance_monthly_tokens, $locale),
        })}
      />
    {:else}
      <p class="hint" data-testid="llm-budget-month-used">
        {$t('settings.llmUsage.admin.monthUsedUnlimited', {
          used: formatTokens(current.instance_used_this_month, $locale),
        })}
      </p>
    {/if}

    <div class="fields">
      <label class="field">
        <span class="field-label">{$t('settings.llmUsage.admin.dailyLabel')}</span>
        <input
          type="number"
          min="0"
          step="1000"
          inputmode="numeric"
          bind:value={dailyInput}
          aria-invalid={daily === null}
          data-testid="llm-budget-daily"
        />
      </label>
      <label class="field">
        <span class="field-label">{$t('settings.llmUsage.admin.monthlyLabel')}</span>
        <input
          type="number"
          min="0"
          step="100000"
          inputmode="numeric"
          bind:value={monthlyInput}
          aria-invalid={monthly === null}
          data-testid="llm-budget-monthly"
        />
      </label>
      <p class="hint">{$t('settings.llmUsage.admin.zeroUnlimited')}</p>
      <label class="check">
        <input type="checkbox" bind:checked={localUnlimited} data-testid="llm-budget-local" />
        <span>{$t('settings.llmUsage.admin.localUnlimited')}</span>
      </label>
    </div>

    {#if invalid}
      <p class="error" role="alert">{$t('settings.llmUsage.admin.invalid')}</p>
    {/if}
    <div class="button-row">
      <button
        type="button"
        class="btn btn-primary"
        on:click={save}
        disabled={saving || invalid || !dirty}
      >
        {saving ? $t('settings.llmUsage.admin.saving') : $t('settings.llmUsage.admin.save')}
      </button>
    </div>
  {/if}
</SettingsCard>

<style>
  .fields {
    display: flex;
    flex-direction: column;
    gap: var(--space-3);
    margin-top: var(--space-3);
  }

  .field {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
    max-width: 20rem;
  }

  .field-label {
    font-size: var(--font-size-md);
    color: var(--text-color);
  }

  .check {
    display: flex;
    align-items: center;
    gap: var(--space-2);
    font-size: var(--font-size-md);
    color: var(--text-color);
  }

  .hint {
    font-size: var(--font-size-sm);
    color: var(--text-muted);
    margin: 0;
  }

  .error {
    font-size: var(--font-size-md);
    color: var(--error-text);
    margin: var(--space-2) 0 0;
  }

  .button-row {
    display: flex;
    justify-content: flex-end;
    margin-top: var(--space-3);
  }
</style>
