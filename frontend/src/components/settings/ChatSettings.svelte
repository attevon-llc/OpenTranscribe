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
    <h2>{$t('chat.settings.title')}</h2>
    <p class="section-description">{$t('chat.settings.description')}</p>
  </header>

  {#if !loading}
    <div class="setting-row">
      <label class="checkbox-row">
        <input
          type="checkbox"
          bind:checked={settings.use_context_default}
          data-testid="chat-settings-use-context"
        />
        <span>{$t('chat.settings.useContextDefault')}</span>
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

  h2 {
    margin: 0 0 0.25rem;
    font-size: 1.05rem;
    font-weight: 600;
    color: var(--text-color);
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

  .checkbox-row {
    display: flex;
    align-items: center;
    gap: 0.6rem;
    font-size: 0.9rem;
    color: var(--text-color);
    cursor: pointer;
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
