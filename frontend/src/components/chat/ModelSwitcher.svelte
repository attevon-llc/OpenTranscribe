<!--
  ModelSwitcher.svelte — pin a different model for this conversation.

  Scoped to one conversation rather than changing the account default: you often
  want a bigger model for one hard question without permanently paying for it.

  Switching to a SMALLER context window warns first, because it silently changes
  what the model can see — earlier turns start getting dropped from the prompt,
  which looks like the assistant forgetting rather than a setting you changed.
-->
<script lang="ts">
  import { createEventDispatcher, onMount } from 'svelte';
  import { t } from '$stores/locale';
  import { LLMSettingsApi, type UserLLMSettings } from '$lib/api/llmSettings';

  /** Conversation's pinned config uuid, or null to follow the account default. */
  export let selectedUuid: string | null = null;
  export let disabled = false;

  const dispatch = createEventDispatcher<{ change: string | null }>();

  let configs: UserLLMSettings[] = [];
  let activeDefaultUuid: string | null = null;
  let pendingUuid: string | null = null;

  $: current = configs.find((c) => c.uuid === selectedUuid) ?? null;
  $: currentWindow = current?.max_tokens ?? defaultConfig?.max_tokens ?? 0;
  $: defaultConfig = configs.find((c) => c.uuid === activeDefaultUuid) ?? null;
  $: pending = configs.find((c) => c.uuid === pendingUuid) ?? null;

  onMount(async () => {
    try {
      const list = await LLMSettingsApi.getUserConfigurations();
      configs = [...list.configurations, ...(list.shared_configurations ?? [])];
      activeDefaultUuid = list.active_configuration_id ?? null;
    } catch {
      configs = [];
    }
  });

  function handleSelect(event: Event): void {
    const value = (event.target as HTMLSelectElement).value;
    const next = value || null;

    // Warn only when the switch REDUCES what the model can see.
    const nextConfig = configs.find((c) => c.uuid === next);
    const nextWindow = nextConfig?.max_tokens ?? defaultConfig?.max_tokens ?? 0;
    if (next && currentWindow > 0 && nextWindow > 0 && nextWindow < currentWindow) {
      pendingUuid = next;
      return;
    }
    dispatch('change', next);
  }

  function confirmSwitch(): void {
    dispatch('change', pendingUuid);
    pendingUuid = null;
  }
</script>

<div class="model-switcher">
  <label class="field-label" for="chat-model-select">{$t('chat.model.title')}</label>
  <select
    id="chat-model-select"
    value={selectedUuid ?? ''}
    on:change={handleSelect}
    {disabled}
    data-testid="chat-model-select"
  >
    <option value="">
      {defaultConfig
        ? `${$t('chat.model.default')} — ${defaultConfig.name}`
        : $t('chat.model.default')}
    </option>
    {#each configs as config (config.uuid)}
      <option value={config.uuid}>
        {config.name} ({config.model_name})
      </option>
    {/each}
  </select>

  {#if currentWindow > 0}
    <span class="context-note">
      {$t('chat.model.contextWindow', { tokens: currentWindow.toLocaleString() })}
    </span>
  {/if}

  {#if pending}
    <div class="switch-warning" data-testid="chat-model-switch-warning">
      <p class="warning-title">{$t('chat.model.switchWarningTitle')}</p>
      <p class="warning-body">
        {$t('chat.model.switchWarningMessage', {
          tokens: (pending.max_tokens ?? 0).toLocaleString(),
        })}
      </p>
      <div class="warning-actions">
        <button type="button" class="warning-btn primary" on:click={confirmSwitch}>
          {$t('chat.model.switchAnyway')}
        </button>
        <button type="button" class="warning-btn" on:click={() => (pendingUuid = null)}>
          {$t('common.cancel')}
        </button>
      </div>
    </div>
  {/if}
</div>

<style>
  .model-switcher {
    display: flex;
    flex-direction: column;
    gap: var(--space-1);
  }

  .field-label {
    font-size: var(--font-size-md);
    font-weight: var(--font-weight-medium);
    color: var(--text-color);
  }

  select {
    width: 100%;
  }

  .context-note {
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
    font-variant-numeric: tabular-nums;
  }

  .switch-warning {
    margin-top: var(--space-1-5);
    padding: var(--space-2-5) var(--space-3);
    border: 1px solid rgba(var(--warning-color-rgb), 0.5);
    border-radius: var(--radius-lg);
    background-color: rgba(var(--warning-color-rgb), 0.1);
  }

  .warning-title {
    margin: 0 0 var(--space-1);
    font-size: var(--font-size-sm);
    font-weight: var(--font-weight-semibold);
    color: var(--text-color);
  }

  .warning-body {
    margin: 0 0 var(--space-2);
    font-size: var(--font-size-sm);
    color: var(--text-secondary);
    line-height: 1.45;
  }

  .warning-actions {
    display: flex;
    gap: var(--space-1-5);
  }

  .warning-btn {
    padding: var(--space-1) var(--space-2-5);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-md);
    background-color: var(--card-background);
    color: var(--text-color);
    font-size: var(--font-size-sm);
    cursor: pointer;
  }

  .warning-btn:hover {
    background-color: var(--button-hover);
  }

  .warning-btn.primary {
    border-color: var(--primary-color);
    color: var(--primary-on-surface);
    font-weight: var(--font-weight-medium);
  }
</style>
