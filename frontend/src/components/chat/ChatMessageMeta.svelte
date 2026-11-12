<!--
  ChatMessageMeta.svelte — expandable diagnostics for one assistant answer.

  Shows what actually happened for this turn: which model answered, how the
  question was rewritten for retrieval, how many chunks were considered vs used,
  and where the time went. Useful when an answer looks wrong and you need to know
  whether retrieval or generation is at fault.
-->
<script lang="ts">
  import MenuIcon from '$components/ui/MenuIcon.svelte';
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import type { ChatMessage } from '$lib/types/chat';

  export let message: ChatMessage;

  /**
   * `disambiguate`: the reader picked one of the ambiguous speaker-mention
   * candidates (`msg_metadata.speaker_resolution.ambiguous`). The
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
          <dd>
            <ol class="plan-steps" data-testid="chat-meta-plan-steps">
              {#each meta.plan.steps as step, i (i)}
                <li>
                  {#if i > 0}<MenuIcon name="chevron-right" size="xs" />{/if}<span dir="auto">{step}</span>
                </li>
              {/each}
            </ol>
          </dd>
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
    margin-top: var(--space-2);
  }

  .meta-toggle {
    background: none;
    border: none;
    padding: var(--space-1) var(--space-1-5);
    margin-left: calc(-1 * var(--space-1-5));
    border-radius: var(--radius-md);
    color: var(--text-secondary);
    font-size: var(--font-size-xs);
    cursor: pointer;
  }

  .meta-toggle:hover {
    background-color: var(--button-hover);
    color: var(--text-color);
  }

  .meta-grid {
    display: grid;
    grid-template-columns: auto 1fr;
    gap: var(--space-1) var(--space-3-5);
    margin: var(--space-2) 0 0;
    padding: var(--space-2-5) var(--space-3);
    border: 1px solid var(--border-color);
    border-radius: var(--radius-lg);
    background-color: var(--surface-color);
    font-size: var(--font-size-sm);
  }

  dt {
    color: var(--text-secondary);
    font-weight: var(--font-weight-medium);
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

  .plan-steps {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-1);
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .plan-steps li {
    display: inline-flex;
    align-items: center;
    gap: var(--space-1);
    color: inherit;
  }

  .plan-steps li :global(.menu-icon) {
    color: var(--text-secondary);
  }

  .estimated {
    color: var(--text-secondary);
    font-size: var(--font-size-xs);
    margin-left: var(--space-1);
  }

  .disambiguation-hint {
    margin-bottom: var(--space-1);
    color: var(--text-secondary);
  }

  .disambiguation-row {
    display: flex;
    flex-wrap: wrap;
    gap: var(--space-1-5);
  }

  .disambiguation-chip {
    padding: var(--space-0-5) var(--space-2);
    border: 1px solid rgba(var(--primary-color-rgb), 0.35);
    border-radius: var(--radius-pill);
    background-color: rgba(var(--primary-color-rgb), 0.08);
    color: var(--primary-on-surface);
    font-size: var(--font-size-sm);
    font-weight: var(--font-weight-medium);
    cursor: pointer;
  }

  .disambiguation-chip:hover {
    background-color: rgba(var(--primary-color-rgb), 0.18);
  }
</style>
