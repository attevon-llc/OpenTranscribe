<!--
  ChatControlsPanel.svelte — per-conversation behaviour, from the header gear.

  Scoped to ONE conversation on purpose. Turning context off or pinning a
  different system prompt is usually something you want for the thing you're
  working on right now, not a permanent change to how chat behaves — the
  account-wide equivalents live in Settings → Chat.
-->
<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import { clickOutside } from '$lib/actions/clickOutside';
  import { focusTrap } from '$lib/actions/focusTrap';
  import ModelSwitcher from './ModelSwitcher.svelte';
  import type { ConversationSettings, SearchMode } from '$lib/types/chat';

  export let isOpen = false;
  export let settings: ConversationSettings = {};
  /** Resolved use-context (conversation override, else the user default). */
  export let useContext = true;
  export let disabled = false;
  /** Conversation's pinned LLM config uuid (null follows the account default). */
  export let llmConfigUuid: string | null = null;
  /** The gear button — so its click doesn't count as "outside" and re-close us. */
  export let triggerEl: HTMLElement | null = null;

  const dispatch = createEventDispatcher<{
    change: Partial<ConversationSettings>;
    model: string | null;
    close: void;
  }>();

  const SEARCH_MODES: SearchMode[] = ['hybrid', 'semantic', 'keyword'];

  let systemPromptDraft = '';
  let promptDirty = false;

  $: if (isOpen && !promptDirty) {
    systemPromptDraft = settings.system_prompt ?? '';
  }

  $: temperature = settings.temperature ?? 0.3;
  $: searchMode = settings.search_mode ?? 'hybrid';

  function toggleContext(): void {
    dispatch('change', { use_context: !useContext });
  }

  function commitPrompt(): void {
    promptDirty = false;
    dispatch('change', { system_prompt: systemPromptDraft.trim() || null });
  }

  /**
   * Escape closes the panel.
   *
   * Stopped from propagating so it doesn't also reach the page-level handler,
   * which uses Escape to stop an in-flight generation — closing a settings
   * panel should never cancel someone's answer.
   */
  function handleKeydown(event: KeyboardEvent): void {
    if (event.key === 'Escape') {
      event.stopPropagation();
      dispatch('close');
    }
  }
</script>

{#if isOpen}
  <div
    class="controls-panel"
    role="dialog"
    tabindex="-1"
    aria-modal="false"
    aria-label={$t('chat.controls.title')}
    use:focusTrap={{ enabled: isOpen }}
    use:clickOutside={{ enabled: isOpen, ignore: [triggerEl] }}
    on:click_outside={() => dispatch('close')}
    on:keydown={handleKeydown}
    data-testid="chat-controls-panel"
  >
    <div class="panel-header">
      <h2>{$t('chat.controls.title')}</h2>
      {#if promptDirty}
        <span class="dirty-indicator" title={$t('common.unsavedChanges')}>●</span>
      {/if}
      <button
        type="button"
        class="close-btn"
        on:click={() => dispatch('close')}
        aria-label={$t('common.close')}
      >
        ×
      </button>
    </div>

    <div class="control-group">
      <label class="toggle-row">
        <input
          type="checkbox"
          checked={useContext}
          on:change={toggleContext}
          {disabled}
          data-testid="chat-use-context-toggle"
        />
        <span>
          <span class="toggle-label">{$t('chat.controls.useContext')}</span>
          <span class="toggle-hint">{$t('chat.controls.useContextHint')}</span>
        </span>
      </label>
    </div>

    <div class="control-group">
      <label class="field-label" for="chat-system-prompt">
        {$t('chat.controls.systemPrompt')}
      </label>
      <textarea
        id="chat-system-prompt"
        bind:value={systemPromptDraft}
        on:input={() => (promptDirty = true)}
        on:blur={commitPrompt}
        rows="4"
        maxlength="2000"
        placeholder={$t('chat.controls.systemPromptPlaceholder')}
        {disabled}
        data-testid="chat-system-prompt"
      ></textarea>
      <span class="char-count">{systemPromptDraft.length} / 2000</span>
    </div>

    <div class="control-group">
      <label class="field-label" for="chat-temperature">
        {$t('chat.controls.temperature')}
        <span class="value">{temperature.toFixed(2)}</span>
      </label>
      <input
        id="chat-temperature"
        type="range"
        min="0"
        max="1"
        step="0.05"
        value={temperature}
        on:change={(e) =>
          dispatch('change', { temperature: Number((e.target as HTMLInputElement).value) })}
        {disabled}
        data-testid="chat-temperature"
      />
      <span class="range-hint">{$t('chat.controls.temperatureHint')}</span>
    </div>

    <div class="control-group">
      <ModelSwitcher
        selectedUuid={llmConfigUuid}
        {disabled}
        on:change={(e) => dispatch('model', e.detail)}
      />
    </div>

    <div class="control-group">
      <label class="field-label" for="chat-search-mode">{$t('chat.controls.searchMode')}</label>
      <select
        id="chat-search-mode"
        value={searchMode}
        on:change={(e) =>
          dispatch('change', {
            search_mode: (e.target as HTMLSelectElement).value as SearchMode,
          })}
        disabled={disabled || !useContext}
        data-testid="chat-search-mode"
      >
        {#each SEARCH_MODES as mode}
          <option value={mode}>{$t(`chat.searchMode.${mode}`)}</option>
        {/each}
      </select>
    </div>
  </div>
{/if}

<style>
  .controls-panel {
    position: absolute;
    top: 3.25rem;
    right: 0.75rem;
    z-index: 20;
    width: min(22rem, calc(100vw - 2rem));
    display: flex;
    flex-direction: column;
    gap: 1.1rem;
    padding: 1rem;
    border: 1px solid var(--border-color);
    border-radius: 12px;
    background-color: var(--card-background);
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.16);
  }

  .panel-header {
    display: flex;
    align-items: center;
    gap: 0.4rem;
  }

  .panel-header h2 {
    margin: 0;
    flex: 1;
    font-size: 0.95rem;
    font-weight: 600;
    color: var(--text-color);
  }

  .dirty-indicator {
    color: var(--primary-color);
    font-size: 0.9rem;
  }

  .close-btn {
    background: none;
    border: none;
    font-size: 1.3rem;
    line-height: 1;
    color: var(--text-secondary);
    cursor: pointer;
    padding: 0 0.25rem;
    border-radius: 4px;
  }

  .close-btn:hover {
    background-color: var(--button-hover);
    color: var(--text-color);
  }

  .control-group {
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
  }

  .toggle-row {
    display: flex;
    align-items: flex-start;
    gap: 0.6rem;
    cursor: pointer;
  }

  /* form-elements.css sets `input { width: 100% }` with no type exemption, so an
     unreset checkbox stretches to fill the row and shoves its label off to the
     right. Every other panel in the app resets this; the chat surface must too. */
  .toggle-row input[type='checkbox'] {
    flex: none;
    width: 1rem;
    height: 1rem;
    margin: 0.15rem 0 0;
    padding: 0;
    accent-color: var(--primary-color);
    cursor: pointer;
  }

  .toggle-label {
    display: block;
    font-size: 0.87rem;
    font-weight: 500;
    color: var(--text-color);
  }

  .toggle-hint,
  .range-hint {
    display: block;
    font-size: 0.75rem;
    color: var(--text-secondary);
    line-height: 1.4;
  }

  .field-label {
    display: flex;
    justify-content: space-between;
    align-items: center;
    font-size: 0.82rem;
    font-weight: 500;
    color: var(--text-color);
  }

  .value {
    color: var(--text-secondary);
    font-variant-numeric: tabular-nums;
  }

  textarea,
  select {
    width: 100%;
    padding: 0.5rem 0.65rem;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    background-color: var(--surface-color);
    color: var(--text-color);
    font-family: inherit;
    font-size: 0.85rem;
    resize: vertical;
  }

  textarea:focus-visible,
  select:focus-visible {
    outline: 2px solid var(--primary-color);
    outline-offset: 1px;
    border-color: var(--primary-color);
  }

  textarea:focus,
  select:focus {
    border-color: var(--primary-color);
  }

  input[type='range'] {
    width: 100%;
    accent-color: var(--primary-color);
  }

  .char-count {
    align-self: flex-end;
    font-size: 0.7rem;
    color: var(--text-secondary);
    font-variant-numeric: tabular-nums;
  }
</style>
