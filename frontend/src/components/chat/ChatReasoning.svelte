<!--
  ChatReasoning.svelte — collapsed-by-default "thinking" section for one answer.

  Renders a provider's separately-streamed reasoning/thinking content above the
  final answer (Open WebUI's pattern): collapsed by default, a "Thinking… Ns" /
  "Thought for Ns" header, expandable to the full reasoning text. Only rendered
  by the caller when the message actually has reasoning content — an ordinary
  answer from a non-reasoning provider must show nothing extra at all.

  Reuses the shared `ExpandableSection` primitive for the collapse chrome and
  `ChatMarkdown` for the body: reasoning text is still model-authored text, so it
  goes through the exact same sanitized markdown pipeline as the final answer —
  never a second, untrusted rendering path.
-->
<script lang="ts">
  import { onDestroy } from 'svelte';
  import { t } from '$stores/locale';
  import ExpandableSection from '$components/ui/ExpandableSection.svelte';
  import ChatMarkdown from './ChatMarkdown.svelte';

  export let content = '';
  /** True while reasoning deltas are still arriving, before the first answer token. */
  export let streaming = false;
  /** Wall-clock `Date.now()` when the first reasoning delta arrived, for a live counter. */
  export let startedAt: number | undefined = undefined;
  /** Frozen elapsed ms once reasoning ended. Undefined for messages loaded from history. */
  export let durationMs: number | undefined = undefined;

  let expanded = false;
  let liveElapsedMs = 0;
  let interval: ReturnType<typeof setInterval> | undefined;

  function tick(): void {
    if (startedAt !== undefined) liveElapsedMs = Date.now() - startedAt;
  }

  function syncTimer(isStreaming: boolean, start: number | undefined): void {
    if (isStreaming && start !== undefined) {
      if (!interval) {
        tick();
        interval = setInterval(tick, 1000);
      }
    } else if (interval) {
      clearInterval(interval);
      interval = undefined;
    }
  }

  // Referencing only `streaming`/`startedAt` at the statement's top level keeps
  // `interval` out of Svelte's dependency tracking for this block — mutating it
  // inside `syncTimer` must not itself re-trigger the reaction.
  $: syncTimer(streaming, startedAt);

  onDestroy(() => {
    if (interval) clearInterval(interval);
  });

  $: seconds = Math.max(0, Math.round((streaming ? liveElapsedMs : (durationMs ?? 0)) / 1000));
  $: title = streaming
    ? seconds > 0
      ? $t('chat.reasoning.thinkingFor', { seconds })
      : $t('chat.reasoning.thinking')
    : durationMs !== undefined
      ? $t('chat.reasoning.thoughtFor', { seconds })
      : $t('chat.reasoning.label');
</script>

{#if content}
  <div class="chat-reasoning" data-testid="chat-reasoning">
    <ExpandableSection {title} bind:expanded>
      <div class="reasoning-body" data-testid="chat-reasoning-body">
        <ChatMarkdown {content} {streaming} />
      </div>
    </ExpandableSection>
  </div>
{/if}

<style>
  .chat-reasoning {
    margin-bottom: 0.5rem;
    max-width: min(80%, 42rem);
    border: 1px solid var(--border-color);
    border-radius: 10px;
    background-color: var(--surface-color);
  }

  /* ExpandableSection is a generic list-style primitive (border-bottom, wider
     padding); trim it down to fit inside a chat bubble's much smaller footprint. */
  .chat-reasoning :global(.expandable) {
    border-bottom: none;
  }

  .chat-reasoning :global(.expandable-header) {
    padding: 0.45rem 0.7rem;
    font-size: 0.8rem;
    font-weight: 500;
    color: var(--text-secondary);
  }

  .chat-reasoning :global(.expandable-header:hover) {
    color: var(--primary-on-surface);
  }

  .chat-reasoning :global(.expandable-content) {
    padding: 0 0.7rem 0.7rem;
    border-top: 1px solid var(--border-color);
  }

  .reasoning-body {
    padding-top: 0.6rem;
    color: var(--text-secondary);
  }

  .reasoning-body :global(.chat-markdown) {
    font-size: 0.85rem;
  }
</style>
