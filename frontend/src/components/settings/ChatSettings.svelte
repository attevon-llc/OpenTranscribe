<!--
  ChatSettings.svelte — per-user chat defaults (Settings → Chat).

  These are DEFAULTS for new conversations, not global switches: any individual
  chat can override them from its Chat Controls panel. The distinction matters
  because "answer as a meeting summary" is usually a per-project preference,
  while "always use my transcripts" is an account-level habit.
-->
<script lang="ts">
  import { onMount } from 'svelte';
  import { t } from '$stores/locale';
  import { settingsModalStore } from '$stores/settingsModalStore';
  import { toastStore } from '$stores/toast';
  import { getChatUserSettings, updateChatUserSettings } from '$lib/api/chatApi';
  import type { ChatUserSettings, SearchMode } from '$lib/types/chat';

  const SEARCH_MODES: SearchMode[] = ['hybrid', 'semantic', 'keyword'];
  const MAX_PROMPT_CHARS = 2000;

  const DEFAULTS: ChatUserSettings = {
    system_prompt: '',
    use_context_default: true,
    default_search_mode: 'hybrid',
  };

  let loading = true;
  let saving = false;
  let settings: ChatUserSettings = { ...DEFAULTS };
  let original: ChatUserSettings = { ...DEFAULTS };

  $: hasChanges = JSON.stringify(settings) !== JSON.stringify(original);
  $: settingsModalStore.setDirty('chat', hasChanges);

  onMount(() => {
    (async () => {
      try {
        const loaded = await getChatUserSettings();
        settings = { ...loaded };
        original = { ...loaded };
      } catch {
        settings = { ...DEFAULTS };
        original = { ...DEFAULTS };
      } finally {
        loading = false;
      }
    })();
  });

  async function save(): Promise<void> {
    saving = true;
    try {
      const saved = await updateChatUserSettings(settings);
      settings = { ...saved };
      original = { ...saved };
      toastStore.success($t('chat.settings.saved'));
    } catch {
      toastStore.error($t('chat.settings.saveError'));
    } finally {
      saving = false;
    }
  }
</script>

<section class="settings-section" data-testid="chat-settings">
  <header>
    <!-- SettingsModal already renders the section heading (.section-title); a
         second <h2> here duplicated "Chat" on screen and in the a11y tree. -->
    <p class="section-description">{$t('chat.settings.description')}</p>
  </header>

  {#if !loading}
    <div class="setting-row toggle-row">
      <label class="toggle-label" for="chat-use-context">
        {$t('chat.settings.useContextDefault')}
      </label>
      <label class="toggle-switch">
        <input
          id="chat-use-context"
          type="checkbox"
          bind:checked={settings.use_context_default}
          data-testid="chat-settings-use-context"
        />
        <span class="toggle-slider"></span>
      </label>
    </div>

    <div class="setting-row">
      <label class="field-label" for="chat-default-search-mode">
        {$t('chat.settings.defaultSearchMode')}
      </label>
      <select
        id="chat-default-search-mode"
        bind:value={settings.default_search_mode}
        data-testid="chat-settings-search-mode"
      >
        {#each SEARCH_MODES as mode}
          <option value={mode}>{$t(`chat.searchMode.${mode}`)}</option>
        {/each}
      </select>
    </div>

    <div class="setting-row">
      <label class="field-label" for="chat-default-prompt">
        {$t('chat.settings.defaultSystemPrompt')}
      </label>
      <textarea
        id="chat-default-prompt"
        bind:value={settings.system_prompt}
        rows="5"
        maxlength={MAX_PROMPT_CHARS}
        placeholder={$t('chat.controls.systemPromptPlaceholder')}
        data-testid="chat-settings-system-prompt"
      ></textarea>
      <div class="field-footer">
        <span class="hint">{$t('chat.settings.defaultSystemPromptHint')}</span>
        <span class="char-count">
          {settings.system_prompt.length} / {MAX_PROMPT_CHARS}
        </span>
      </div>
    </div>

    <div class="actions">
      <button
        type="button"
        class="btn btn-primary"
        on:click={save}
        disabled={!hasChanges || saving}
        data-testid="chat-settings-save"
      >
        {saving ? $t('common.saving') : $t('common.save')}
      </button>
    </div>
  {/if}
</section>

<style>
  .settings-section {
    display: flex;
    flex-direction: column;
    gap: 1.25rem;
  }

  .section-description {
    margin: 0;
    font-size: 0.85rem;
    color: var(--text-secondary);
    line-height: 1.5;
  }

  .setting-row {
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
  }

  /* Toggle switch, matching DownloadSettings / ContentRedactionSettings. A bare
     checkbox cannot be used here: form-elements.css sets `input { width: 100% }`
     with no type exemption, which stretched the box to 733px and pushed its
     label off to the right edge. The native input stays in the DOM (keyboard +
     screen-reader) but is sized to zero and painted by .toggle-slider. */
  .toggle-row {
    flex-direction: row;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
  }

  .toggle-label {
    font-size: 0.87rem;
    font-weight: 500;
    color: var(--text-color);
    cursor: pointer;
  }

  .toggle-switch {
    position: relative;
    display: inline-block;
    flex-shrink: 0;
    width: 2.75rem;
    height: 1.5rem;
    cursor: pointer;
  }

  .toggle-switch input {
    opacity: 0;
    width: 0;
    height: 0;
  }

  .toggle-slider {
    position: absolute;
    inset: 0;
    background-color: var(--border-color, #d1d5db);
    border-radius: 1.5rem;
    transition: background-color 0.2s;
  }

  .toggle-slider::before {
    content: '';
    position: absolute;
    height: 1.125rem;
    width: 1.125rem;
    left: 0.1875rem;
    bottom: 0.1875rem;
    background-color: white;
    border-radius: 50%;
    transition: transform 0.2s;
  }

  .toggle-switch input:checked + .toggle-slider {
    background-color: var(--primary-color);
  }

  .toggle-switch input:checked + .toggle-slider::before {
    transform: translateX(1.25rem);
  }

  .toggle-switch input:focus-visible + .toggle-slider {
    outline: 2px solid var(--primary-color);
    outline-offset: 2px;
  }

  .field-label {
    font-size: 0.87rem;
    font-weight: 500;
    color: var(--text-color);
  }

  select,
  textarea {
    width: 100%;
    padding: 0.5rem 0.7rem;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    background-color: var(--surface-color);
    color: var(--text-color);
    font-family: inherit;
    font-size: 0.88rem;
    resize: vertical;
  }

  select:focus-visible,
  textarea:focus-visible {
    outline: 2px solid var(--primary-color);
    outline-offset: 1px;
  }

  select:focus,
  textarea:focus {
    border-color: var(--primary-color);
  }

  .field-footer {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    gap: 1rem;
  }

  .hint {
    font-size: 0.78rem;
    color: var(--text-secondary);
    line-height: 1.45;
  }

  .char-count {
    flex-shrink: 0;
    font-size: 0.75rem;
    color: var(--text-secondary);
    font-variant-numeric: tabular-nums;
  }

  .actions {
    display: flex;
    justify-content: flex-end;
  }
</style>
