<!--
  ChatMessageMeta.svelte — expandable diagnostics for one assistant answer.

  Shows what actually happened for this turn: which model answered, how the
  question was rewritten for retrieval, how many chunks were considered vs used,
  and where the time went. Useful when an answer looks wrong and you need to know
  whether retrieval or generation is at fault.
-->
<script lang="ts">
  import { t } from '$stores/locale';
  import type { ChatMessage } from '$lib/types/chat';

  export let message: ChatMessage;

  let expanded = false;

  $: meta = message.msg_metadata ?? {};
  $: timings = meta.timings_ms ?? {};
  $: hasContent =
    Boolean(message.model) ||
    Boolean(message.total_tokens) ||
    meta.retrieved !== undefined ||
    Boolean(meta.rewritten_query);
</script>

{#if hasContent}
  <div class="message-meta">
    <button
      type="button"
      class="meta-toggle"
      on:click={() => (expanded = !expanded)}
      aria-expanded={expanded}
      data-testid="chat-meta-toggle"
    >
      {$t('chat.meta.title')}
    </button>

    {#if expanded}
      <dl class="meta-grid" data-testid="chat-meta-grid">
        {#if message.model}
          <dt>{$t('chat.meta.model')}</dt>
          <dd>{message.provider ? `${message.provider} / ${message.model}` : message.model}</dd>
        {/if}

        {#if message.prompt_tokens != null}
          <dt>{$t('chat.meta.promptTokens')}</dt>
          <dd>{message.prompt_tokens.toLocaleString()}</dd>
        {/if}

        {#if message.completion_tokens != null}
          <dt>{$t('chat.meta.completionTokens')}</dt>
          <dd>{message.completion_tokens.toLocaleString()}</dd>
        {/if}

        {#if message.total_tokens != null}
          <dt>{$t('chat.meta.totalTokens')}</dt>
          <dd>
            {message.total_tokens.toLocaleString()}
            {#if message.tokens_estimated}
              <span class="estimated">{$t('chat.meta.estimated')}</span>
            {/if}
          </dd>
        {/if}

        {#if meta.retrieved !== undefined}
          <dt>{$t('chat.meta.retrievedChunks')}</dt>
          <dd>{meta.chunks_used ?? 0} / {meta.retrieved}</dd>
        {/if}

        {#if meta.files_searched !== undefined}
          <dt>{$t('chat.meta.filesSearched')}</dt>
          <dd>
            {meta.files_searched === 'all'
              ? $t('chat.context.allTranscripts')
              : meta.files_searched}
          </dd>
        {/if}

        {#if meta.rewritten_query}
          <dt>{$t('chat.meta.rewrittenQuery')}</dt>
          <dd class="rewritten">{meta.rewritten_query}</dd>
        {/if}

        {#if timings.total !== undefined}
          <dt>{$t('chat.meta.searchTime')}</dt>
          <dd>{timings.total} ms</dd>
        {/if}

        {#if meta.cache_hit}
          <dt>{$t('chat.meta.cacheHit')}</dt>
          <dd>{$t('common.yes')}</dd>
        {/if}
      </dl>
    {/if}
  </div>
{/if}

<style>
  .message-meta {
    margin-top: 0.5rem;
  }

  .meta-toggle {
    background: none;
    border: none;
    padding: 0.2rem 0.35rem;
    margin-left: -0.35rem;
    border-radius: 6px;
    color: var(--text-secondary);
    font-size: 0.75rem;
    cursor: pointer;
  }

  .meta-toggle:hover {
    background-color: var(--button-hover);
    color: var(--text-color);
  }

  .meta-grid {
    display: grid;
    grid-template-columns: auto 1fr;
    gap: 0.25rem 0.85rem;
    margin: 0.5rem 0 0;
    padding: 0.65rem 0.75rem;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    background-color: var(--surface-color);
    font-size: 0.78rem;
  }

  dt {
    color: var(--text-secondary);
    font-weight: 500;
  }

  dd {
    margin: 0;
    color: var(--text-color);
    font-variant-numeric: tabular-nums;
    overflow-wrap: anywhere;
  }

  .rewritten {
    font-style: italic;
  }

  .estimated {
    color: var(--text-secondary);
    font-size: 0.72rem;
    margin-left: 0.25rem;
  }
</style>
