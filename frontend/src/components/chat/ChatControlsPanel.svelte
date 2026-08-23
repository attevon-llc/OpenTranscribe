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
  import { LLMSettingsApi } from '$lib/api/llmSettings';
  import type { ReasoningOffSwitch } from '$lib/api/llmSettings';
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

  /**
   * The model's MEASURED reasoning off-switch (issue #64).
   *
   * Null until the server answers, and null whenever the server has no verdict
   * — both render nothing, which is the point: a provider accepting a "no
   * reasoning" parameter is not evidence the model obeys it, so the control
   * exists only where a probe proved it does. On the measured gemma-4-e4b this
   * reports `'absent'` and no toggle appears, because a switch that silently
   * does nothing is worse than a missing one.
   */
  let reasoningOffSwitch: ReasoningOffSwitch | null = null;
  let reasoningLoadedFor: string | null | undefined = undefined;
  $: reasoningSupported = reasoningOffSwitch === 'works';
  $: reasoningOn = settings.reasoning !== false;

  async function loadReasoningCapability(pinned: string | null): Promise<void> {
    reasoningLoadedFor = pinned;
    try {
      const data = await LLMSettingsApi.getUserConfigurations();
      const uuid = pinned ?? data.active_configuration_id ?? null;
      reasoningOffSwitch = uuid ? (data.reasoning_off_switch?.[uuid] ?? null) : null;
    } catch {
      // A capability lookup must never break the panel: no verdict, no control,
      // and every other setting still works.
      reasoningOffSwitch = null;
    }
  }

  $: if (isOpen && reasoningLoadedFor !== llmConfigUuid) {
    loadReasoningCapability(llmConfigUuid);
  }

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

    <!-- Collapsed by default: most people never need these, and the panel is
         friendlier without two extra numeric fields competing for attention. -->
    <details class="advanced">
      <summary data-testid="chat-advanced-toggle">{$t('chat.controls.advanced')}</summary>

      <!-- Rendered ONLY where the server measured a working off-switch for this
           model. Do not relax this condition to "the provider accepts the
           parameter": that is exactly what the measurement disproved. -->
      {#if reasoningSupported}
        <div class="control-group">
          <label class="toggle-row">
            <input
              type="checkbox"
              checked={reasoningOn}
              on:change={(e) =>
                dispatch('change', {
                  reasoning: (e.target as HTMLInputElement).checked ? null : false
                })}
              {disabled}
              data-testid="chat-reasoning-toggle"
            />
            <span>
              <span class="toggle-label">{$t('chat.controls.reasoning')}</span>
              <span class="toggle-hint">{$t('chat.controls.reasoningHint')}</span>
            </span>
          </label>
        </div>
      {/if}

      <div class="control-group">
        <label class="field-label" for="chat-max-tokens">
          {$t('chat.controls.maxTokens')}
          <span class="value">
            {settings.max_tokens ?? $t('chat.controls.inherit')}
          </span>
        </label>
        <input
          id="chat-max-tokens"
          type="number"
          min="256"
          max="200000"
          step="256"
          placeholder={$t('chat.controls.inherit')}
          value={settings.max_tokens ?? ''}
          on:change={(e) => {
            const raw = (e.target as HTMLInputElement).value.trim();
            dispatch('change', { max_tokens: raw === '' ? null : Number(raw) });
          }}
          {disabled}
          data-testid="chat-max-tokens"
        />
        <span class="range-hint">{$t('chat.controls.maxTokensHint')}</span>
      </div>

      <div class="control-group">
        <label class="field-label" for="chat-top-p">
          {$t('chat.controls.topP')}
          <span class="value">
            {settings.top_p ?? $t('chat.controls.inherit')}
          </span>
        </label>
        <input
          id="chat-top-p"
          type="range"
          min="0"
          max="1"
          step="0.05"
          value={settings.top_p ?? 1}
          on:change={(e) =>
            dispatch('change', { top_p: Number((e.target as HTMLInputElement).value) })}
          {disabled}
          data-testid="chat-top-p"
        />
        <span class="range-hint">{$t('chat.controls.topPHint')}</span>
      </div>
    </details>
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
    /* ⚠️ Without these the panel had NO bound at all — `max-height: none`,
       `overflow-y: visible`. Expanding "Advanced" takes it from ~610px to
       875px, and measured at 1440x{900,800,700} it ran past the viewport
       bottom by 87 / 187 / 247px with no scrollbar, so the last controls were
       simply unreachable. It is anchored 112px below the viewport top
       (navbar + the 3.25rem offset above), hence the subtraction.
       `overscroll-behavior` stops a scroll that reaches the end of the panel
       from chaining into the transcript thread behind it. */
    max-height: calc(100vh - var(--navbar-height, 60px) - 5rem);
    overflow-y: auto;
    overscroll-behavior: contain;
  }

  /* Sticky rather than a scrolling body wrapper: it pins the close button and
     the unsaved-changes dot with no markup change at all. `top` cancels the
     panel's own padding so the header sits flush against the top edge while
     stuck, and the background is opaque so content cannot show through it. */
  .panel-header {
    position: sticky;
    top: -1rem;
    z-index: 1;
    margin: -1rem -1rem 0;
    padding: 1rem 1rem 0.6rem;
    background-color: var(--card-background);
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

  .advanced {
    border-top: 1px solid var(--border-color);
    padding-top: 0.85rem;
  }

  .advanced summary {
    cursor: pointer;
    font-size: 0.82rem;
    font-weight: 500;
    color: var(--text-secondary);
    list-style: revert;
  }

  .advanced summary:hover {
    color: var(--text-color);
  }

  .advanced summary:focus-visible {
    outline: 2px solid var(--primary-color);
    outline-offset: 2px;
    border-radius: 4px;
  }

  .advanced > .control-group {
    margin-top: 0.9rem;
  }

  /* form-elements.css gives every <input> width:100% and a surface fill; the
     number field wants that, but it must not inherit the button transform. */
  .advanced input[type='number'] {
    padding: 0.4rem 0.6rem;
    font-size: 0.85rem;
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
