<!--
  TokenUsagePanel.svelte — how much of the model's context the last turn used.

  Only appears once a turn has reported usage, and only expands on request:
  token counts matter when answers start losing earlier context, and are noise
  the rest of the time. The colour bands are the actionable part — green while
  there is headroom, amber approaching the limit, red once older turns are being
  dropped from the conversation.
-->
<script lang="ts">
  import { t } from '$stores/locale';
  import ProgressBar from '$components/ui/ProgressBar.svelte';
  import type { TokenUsage } from '$lib/types/chat';

  export let usage: TokenUsage | null = null;
  /** The active model's context window, from its LLM configuration. */
  export let contextWindow = 0;

  let expanded = false;

  $: pct = contextWindow > 0 && usage ? Math.min(100, (usage.total_tokens / contextWindow) * 100) : 0;
  $: level = pct >= 90 ? 'over' : pct >= 70 ? 'near' : 'ok';
</script>

{#if usage && usage.total_tokens > 0}
  <div class="token-panel" data-testid="chat-token-panel">
    <button
      type="button"
      class="token-toggle"
      on:click={() => (expanded = !expanded)}
      aria-expanded={expanded}
    >
      <span class="dot" class:near={level === 'near'} class:over={level === 'over'}></span>
      {$t('chat.tokens.title')}
      <span class="token-total">{usage.total_tokens.toLocaleString()}</span>
      {#if usage.estimated}
        <span class="estimated">{$t('chat.meta.estimated')}</span>
      {/if}
    </button>

    {#if expanded}
      <div class="token-detail">
        {#if contextWindow > 0}
          <ProgressBar percent={pct} />
          <p class="token-line">
            {$t('chat.tokens.used', {
              used: usage.total_tokens.toLocaleString(),
              limit: contextWindow.toLocaleString(),
            })}
          </p>
          {#if level === 'over'}
            <p class="warning over">{$t('chat.tokens.overLimit')}</p>
          {:else if level === 'near'}
            <p class="warning">{$t('chat.tokens.nearLimit')}</p>
          {/if}
        {/if}

        <dl class="token-breakdown">
          <dt>{$t('chat.meta.promptTokens')}</dt>
          <dd>{(usage.prompt_tokens ?? 0).toLocaleString()}</dd>
          <dt>{$t('chat.meta.completionTokens')}</dt>
          <dd>{(usage.completion_tokens ?? 0).toLocaleString()}</dd>
        </dl>
      </div>
    {/if}
  </div>
{/if}

<style>
  .token-panel {
    padding: 0.25rem 0;
  }

  .token-toggle {
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    padding: 0.2rem 0.4rem;
    margin-left: -0.4rem;
    border: none;
    border-radius: 6px;
    background: none;
    color: var(--text-secondary);
    font-size: 0.74rem;
    cursor: pointer;
  }

  .token-toggle:hover {
    background-color: var(--button-hover);
    color: var(--text-color);
  }

  .dot {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background-color: var(--success-color, #22c55e);
  }

  .dot.near {
    background-color: var(--warning-color, #ffc107);
  }

  .dot.over {
    background-color: var(--error-color, #dc3545);
  }

  .token-total {
    font-variant-numeric: tabular-nums;
    color: var(--text-color);
  }

  .estimated {
    font-size: 0.7rem;
  }

  .token-detail {
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
    margin-top: 0.45rem;
    padding: 0.6rem 0.7rem;
    border: 1px solid var(--border-color);
    border-radius: 8px;
    background-color: var(--surface-color);
  }

  .token-line {
    margin: 0;
    font-size: 0.76rem;
    color: var(--text-secondary);
    font-variant-numeric: tabular-nums;
  }

  .warning {
    margin: 0;
    font-size: 0.76rem;
    color: var(--warning-color, #ffc107);
  }

  .warning.over {
    color: var(--error-color, #dc3545);
    font-weight: 600;
  }

  .token-breakdown {
    display: grid;
    grid-template-columns: auto 1fr;
    gap: 0.15rem 0.75rem;
    margin: 0;
    font-size: 0.75rem;
  }

  dt {
    color: var(--text-secondary);
  }

  dd {
    margin: 0;
    color: var(--text-color);
    font-variant-numeric: tabular-nums;
  }
</style>
