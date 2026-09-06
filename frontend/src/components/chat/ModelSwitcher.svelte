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
    gap: 0.3rem;
  }

  .field-label {
    font-size: 0.82rem;
    font-weight: 500;
    color: var(--text-color);
  }

  select {
    width: 100%;
    padding: 0.5rem 0.65rem;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    background-color: var(--surface-color);
    color: var(--text-color);
    font-family: inherit;
    font-size: 0.85rem;
  }

  select:focus {
    outline: none;
    border-color: var(--primary-color);
  }

  .context-note {
    font-size: 0.72rem;
    color: var(--text-secondary);
    font-variant-numeric: tabular-nums;
  }

  .switch-warning {
    margin-top: 0.35rem;
    padding: 0.6rem 0.7rem;
    border: 1px solid rgba(var(--warning-color-rgb, 255, 193, 7), 0.5);
    border-radius: 8px;
    background-color: rgba(var(--warning-color-rgb, 255, 193, 7), 0.1);
  }

  .warning-title {
    margin: 0 0 0.2rem;
    font-size: 0.8rem;
    font-weight: 600;
    color: var(--text-color);
  }

  .warning-body {
    margin: 0 0 0.5rem;
    font-size: 0.76rem;
    color: var(--text-secondary);
    line-height: 1.45;
  }

  .warning-actions {
    display: flex;
    gap: 0.35rem;
  }

  .warning-btn {
    padding: 0.25rem 0.6rem;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    background-color: var(--card-background);
    color: var(--text-color);
    font-size: 0.76rem;
    cursor: pointer;
  }

  .warning-btn:hover {
    background-color: var(--button-hover);
  }

  .warning-btn.primary {
    border-color: var(--primary-color);
    color: var(--primary-on-surface);
    font-weight: 500;
  }
</style>
