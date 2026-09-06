<!--
  ChatMessageMeta.svelte — expandable diagnostics for one assistant answer.

  Shows what actually happened for this turn: which model answered, how the
  question was rewritten for retrieval, how many chunks were considered vs used,
  and where the time went. Useful when an answer looks wrong and you need to know
  whether retrieval or generation is at fault.
-->
<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import type { ChatMessage } from '$lib/types/chat';

  export let message: ChatMessage;

  /**
   * `disambiguate`: the reader picked one of the ambiguous speaker-mention
   * candidates (`msg_metadata.speaker_resolution.ambiguous`, W2.2). The
   * parent is responsible for adding the name to `ChatScope.speakers` and
   * re-sending — this component only reports the pick, since scope mutation
   * and resend both live above the message list.
   */
  const dispatch = createEventDispatcher<{ disambiguate: string }>();

  let expanded = false;

  $: meta = message.msg_metadata ?? {};
  $: timings = meta.timings_ms ?? {};
  $: hasContent =
    Boolean(message.model) ||
    Boolean(message.total_tokens) ||
    meta.retrieved !== undefined ||
    Boolean(meta.rewritten_query) ||
    Boolean(meta.map_source) ||
    meta.llm_calls !== undefined ||
    Boolean(meta.legs_failed?.length) ||
    Boolean(meta.leg_count) ||
    Boolean(meta.speaker_resolution) ||
    Boolean(meta.plan?.steps?.length) ||
    Boolean(meta.router_language_unmatched) ||
    Boolean(meta.scope_files_dropped);
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

        {#if meta.scope_files_dropped}
          <dt>{$t('chat.meta.scopeFilesDropped')}</dt>
          <dd data-testid="chat-meta-scope-files-dropped">{meta.scope_files_dropped}</dd>
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

        {#if meta.map_source}
          <dt>{$t('chat.meta.mapSource')}</dt>
          <dd>{meta.map_source}</dd>
        {/if}

        {#if meta.llm_calls !== undefined}
          <dt>{$t('chat.meta.llmCalls')}</dt>
          <dd>{meta.llm_calls}</dd>
        {/if}

        {#if meta.leg_count}
          <dt>{$t('chat.meta.legCount')}</dt>
          <dd data-testid="chat-meta-leg-count">
            {meta.leg_count}
            {#if meta.leg_timings_ms}
              <span class="estimated">
                ({Object.entries(meta.leg_timings_ms)
                  .map(([name, ms]) => `${name}: ${ms}ms`)
                  .join(', ')})
              </span>
            {/if}
          </dd>
        {/if}

        {#if meta.legs_failed?.length}
          <dt>{$t('chat.meta.legsFailed')}</dt>
          <dd>{meta.legs_failed.join(', ')}</dd>
        {/if}

        {#if meta.speaker_resolution?.matched?.length}
          <dt>{$t('chat.meta.speakerResolution')}</dt>
          <dd>{meta.speaker_resolution.matched.join(', ')}</dd>
        {/if}

        {#if meta.speaker_resolution?.ambiguous?.length}
          <dt>{$t('chat.meta.speakerAmbiguous')}</dt>
          <dd>
            <div class="disambiguation-hint">{$t('chat.meta.speakerAmbiguousHint')}</div>
            <div class="disambiguation-row" data-testid="chat-speaker-disambiguation">
              {#each meta.speaker_resolution.ambiguous as candidate (candidate)}
                <button
                  type="button"
                  class="disambiguation-chip"
                  on:click={() => dispatch('disambiguate', candidate)}
                  data-testid="chat-speaker-disambiguation-chip"
                >
                  {candidate}
                </button>
              {/each}
            </div>
          </dd>
        {/if}

        {#if meta.plan?.steps?.length}
          <dt>{$t('chat.meta.plan')}</dt>
          <dd>{meta.plan.steps.join(' → ')}</dd>
        {/if}

        {#if meta.router_language_unmatched}
          <dt>{$t('chat.meta.routerLanguageUnmatched')}</dt>
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

  .disambiguation-hint {
    margin-bottom: 0.3rem;
    color: var(--text-secondary);
  }

  .disambiguation-row {
    display: flex;
    flex-wrap: wrap;
    gap: 0.35rem;
  }

  .disambiguation-chip {
    padding: 0.18rem 0.55rem;
    border: 1px solid rgba(var(--primary-color-rgb), 0.35);
    border-radius: 999px;
    background-color: rgba(var(--primary-color-rgb), 0.08);
    color: var(--primary-on-surface);
    font-size: 0.76rem;
    font-weight: 500;
    cursor: pointer;
  }

  .disambiguation-chip:hover {
    background-color: rgba(var(--primary-color-rgb), 0.18);
  }
</style>
