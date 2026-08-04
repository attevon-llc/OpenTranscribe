<!--
  ChatAdminSettings.svelte — platform RAG tuning (Settings → Chat & RAG, admin).

  Every knob here trades answer quality against latency and memory, so each has
  a hint saying which way. All values are DB-backed SystemSettings applied on the
  next message — there is no restart and no .env edit.
-->
<script lang="ts">
  import { onMount } from 'svelte';
  import { t } from '$stores/locale';
  import { settingsModalStore } from '$stores/settingsModalStore';
  import { toastStore } from '$stores/toast';
  import { getChatAdminSettings, updateChatAdminSettings } from '$lib/api/chatApi';
  import type { ChatAdminSettings } from '$lib/types/chat';

  const DEFAULTS: ChatAdminSettings = {
    candidate_pool: 48,
    final_chunks: 12,
    max_chunks_per_file: 4,
    rerank_enabled: true,
    rerank_max_pairs: 50,
    query_rewrite_enabled: true,
    cache_ttl_seconds: 300,
    semantic_cache_enabled: false,
    semantic_cache_threshold: 0.97,
    history_max_turns: 10,
    messages_per_hour: 120,
    max_concurrent_streams: 2,
    retention_days: 0,
  };

  let loading = true;
  let saving = false;
  let settings: ChatAdminSettings = { ...DEFAULTS };
  let original: ChatAdminSettings = { ...DEFAULTS };

  $: hasChanges = JSON.stringify(settings) !== JSON.stringify(original);
  $: settingsModalStore.setDirty('chat-admin', hasChanges);

  onMount(() => {
    (async () => {
      try {
        const loaded = await getChatAdminSettings();
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
      const saved = await updateChatAdminSettings(settings);
      settings = { ...saved };
      original = { ...saved };
      toastStore.success($t('chat.adminSettings.saved'));
    } catch {
      toastStore.error($t('chat.adminSettings.saveError'));
    } finally {
      saving = false;
    }
  }
</script>

<section class="settings-section" data-testid="chat-admin-settings">
  <p class="section-description">{$t('chat.adminSettings.description')}</p>

  {#if !loading}
    <div class="grid">
      <div class="field">
        <label for="chat-candidate-pool">{$t('chat.adminSettings.candidatePool')}</label>
        <input
          id="chat-candidate-pool"
          type="number"
          min="1"
          max="500"
          bind:value={settings.candidate_pool}
        />
        <span class="hint">{$t('chat.adminSettings.candidatePoolHint')}</span>
      </div>

      <div class="field">
        <label for="chat-final-chunks">{$t('chat.adminSettings.finalChunks')}</label>
        <input
          id="chat-final-chunks"
          type="number"
          min="1"
          max="100"
          bind:value={settings.final_chunks}
        />
        <span class="hint">{$t('chat.adminSettings.finalChunksHint')}</span>
      </div>

      <div class="field">
        <label for="chat-max-per-file">{$t('chat.adminSettings.maxChunksPerFile')}</label>
        <input
          id="chat-max-per-file"
          type="number"
          min="1"
          max="50"
          bind:value={settings.max_chunks_per_file}
        />
        <span class="hint">{$t('chat.adminSettings.maxChunksPerFileHint')}</span>
      </div>

      <div class="field">
        <label for="chat-history-turns">{$t('chat.adminSettings.historyMaxTurns')}</label>
        <input
          id="chat-history-turns"
          type="number"
          min="1"
          max="50"
          bind:value={settings.history_max_turns}
        />
      </div>
    </div>

    <div class="field checkbox-field">
      <label class="checkbox-row">
        <input type="checkbox" bind:checked={settings.rerank_enabled} />
        <span>{$t('chat.adminSettings.rerankEnabled')}</span>
      </label>
      <span class="hint">{$t('chat.adminSettings.rerankEnabledHint')}</span>
    </div>

    {#if settings.rerank_enabled}
      <div class="field indented">
        <label for="chat-rerank-pairs">{$t('chat.adminSettings.rerankMaxPairs')}</label>
        <input
          id="chat-rerank-pairs"
          type="number"
          min="1"
          max="500"
          bind:value={settings.rerank_max_pairs}
        />
      </div>
    {/if}

    <div class="field checkbox-field">
      <label class="checkbox-row">
        <input type="checkbox" bind:checked={settings.query_rewrite_enabled} />
        <span>{$t('chat.adminSettings.queryRewriteEnabled')}</span>
      </label>
      <span class="hint">{$t('chat.adminSettings.queryRewriteEnabledHint')}</span>
    </div>

    <div class="grid">
      <div class="field">
        <label for="chat-cache-ttl">{$t('chat.adminSettings.cacheTtlSeconds')}</label>
        <input
          id="chat-cache-ttl"
          type="number"
          min="0"
          max="86400"
          bind:value={settings.cache_ttl_seconds}
        />
      </div>

      <div class="field">
        <label for="chat-semantic-threshold">
          {$t('chat.adminSettings.semanticCacheThreshold')}
        </label>
        <input
          id="chat-semantic-threshold"
          type="number"
          min="0.5"
          max="1"
          step="0.01"
          bind:value={settings.semantic_cache_threshold}
          disabled={!settings.semantic_cache_enabled}
        />
      </div>
    </div>

    <div class="field checkbox-field">
      <label class="checkbox-row">
        <input type="checkbox" bind:checked={settings.semantic_cache_enabled} />
        <span>{$t('chat.adminSettings.semanticCacheEnabled')}</span>
      </label>
    </div>

    <div class="grid">
      <div class="field">
        <label for="chat-messages-hour">{$t('chat.adminSettings.messagesPerHour')}</label>
        <input
          id="chat-messages-hour"
          type="number"
          min="1"
          max="10000"
          bind:value={settings.messages_per_hour}
        />
      </div>

      <div class="field">
        <label for="chat-concurrent">{$t('chat.adminSettings.maxConcurrentStreams')}</label>
        <input
          id="chat-concurrent"
          type="number"
          min="1"
          max="20"
          bind:value={settings.max_concurrent_streams}
        />
      </div>

      <div class="field">
        <label for="chat-retention">{$t('chat.adminSettings.retentionDays')}</label>
        <input
          id="chat-retention"
          type="number"
          min="0"
          max="3650"
          bind:value={settings.retention_days}
        />
        <span class="hint">{$t('chat.adminSettings.retentionDaysHint')}</span>
      </div>
    </div>

    <div class="actions">
      <button
        type="button"
        class="btn btn-primary"
        on:click={save}
        disabled={!hasChanges || saving}
        data-testid="chat-admin-save"
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

  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(13rem, 1fr));
    gap: 1rem;
  }

  .field {
    display: flex;
    flex-direction: column;
    gap: 0.3rem;
  }

  .field.indented {
    margin-left: 1.75rem;
    max-width: 13rem;
  }

  label {
    font-size: 0.85rem;
    font-weight: 500;
    color: var(--text-color);
  }

  .checkbox-row {
    display: flex;
    align-items: center;
    gap: 0.6rem;
    cursor: pointer;
    font-weight: 500;
  }

  .checkbox-field {
    gap: 0.25rem;
  }

  input[type='number'] {
    width: 100%;
    padding: 0.45rem 0.65rem;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    background-color: var(--surface-color);
    color: var(--text-color);
    font-size: 0.88rem;
    font-variant-numeric: tabular-nums;
  }

  input[type='number']:focus {
    outline: none;
    border-color: var(--primary-color);
  }

  input[type='number']:disabled {
    opacity: 0.55;
    cursor: not-allowed;
  }

  .hint {
    font-size: 0.76rem;
    color: var(--text-secondary);
    line-height: 1.45;
  }

  .actions {
    display: flex;
    justify-content: flex-end;
  }
</style>
