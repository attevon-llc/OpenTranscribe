<!--
  ChatSettings.svelte — per-user chat defaults (Settings → Chat).

  These are DEFAULTS for new conversations, not global switches: any individual
  chat can override them from its Chat Controls panel. The distinction matters
  because "answer as a meeting summary" is usually a per-project preference,
  while "always use my transcripts" is an account-level habit.
-->
<script lang="ts">
  import Toggle from '$components/ui/Toggle.svelte';
  import { SEARCH_MODE_KEYS } from '$lib/i18n/keyMaps';
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
    final_chunks: null,
    rerank_enabled: null,
  };

  let loading = true;
  let saving = false;
  let settings: ChatUserSettings = { ...DEFAULTS };
  let original: ChatUserSettings = { ...DEFAULTS };

  $: hasChanges = JSON.stringify(settings) !== JSON.stringify(original);
  $: settingsModalStore.setDirty('chat', hasChanges);

  onMount(() => {
    void (async () => {
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
      <Toggle
        id="chat-use-context"
        checked={settings.use_context_default}
        onchange={(next) => (settings.use_context_default = next)}
        testid="chat-settings-use-context"
      />
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
        {#each SEARCH_MODES as mode (mode)}
          <option value={mode}>{$t(SEARCH_MODE_KEYS[mode])}</option>
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

    <div class="setting-row">
      <label class="field-label" for="chat-final-chunks">
        {$t('chat.settings.excerpts')}
      </label>
      <input
        id="chat-final-chunks"
        type="number"
        min="1"
        max="50"
        placeholder={$t('chat.settings.inherit')}
        value={settings.final_chunks ?? ''}
        on:change={(e) => {
          const raw = (e.target as HTMLInputElement).value.trim();
          settings = { ...settings, final_chunks: raw === '' ? null : Number(raw) };
        }}
        data-testid="chat-settings-final-chunks"
      />
      <span class="hint">{$t('chat.settings.excerptsHint')}</span>
    </div>

    <div class="setting-row toggle-row">
      <label class="toggle-label" for="chat-rerank">
        {$t('chat.settings.rerank')}
      </label>
      <Toggle
        id="chat-rerank"
        checked={settings.rerank_enabled !== false}
        onchange={(next) => {
          // null (inherit) and true both show as on; only an explicit off is
          // stored, because a preference can never enable what the admin
          // disabled.
          settings = { ...settings, rerank_enabled: next ? null : false };
        }}
        testid="chat-settings-rerank"
      />
    </div>
    <span class="hint standalone-hint">{$t('chat.settings.rerankHint')}</span>

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
    gap: var(--space-5);
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

  .toggle-row {
    flex-direction: row;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-4);
  }

  .toggle-label {
    font-size: 0.87rem;
    font-weight: var(--font-weight-medium);
    color: var(--text-color);
    cursor: pointer;
  }

  .field-label {
    font-size: 0.87rem;
    font-weight: var(--font-weight-medium);
    color: var(--text-color);
  }

  select,
  textarea {
    width: 100%;
    resize: vertical;
  }

  .field-footer {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    gap: var(--space-4);
  }

  .hint {
    font-size: 0.78rem;
    color: var(--text-secondary);
    line-height: 1.45;
  }

  .char-count {
    flex-shrink: 0;
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
    font-variant-numeric: tabular-nums;
  }

  .standalone-hint {
    margin-top: calc(-1 * var(--space-3));
  }

  input[type='number'] {
    width: 100%;
  }

  .actions {
    display: flex;
    justify-content: flex-end;
  }
</style>
