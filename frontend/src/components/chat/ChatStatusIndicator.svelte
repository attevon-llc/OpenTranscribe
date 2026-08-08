<!--
  ChatStatusIndicator.svelte — what the assistant is doing right now.

  This is the ONLY aria-live region in the thread. Announcing the token stream
  itself would make a screen reader read the answer character by character as it
  arrives, which is unusable; instead we announce stage changes and completion.
-->
<script lang="ts">
  import { t } from '$stores/locale';
  import type { StreamStatus } from '$lib/types/chat';

  export let status: StreamStatus = 'idle';

  $: label =
    status === 'retrieving'
      ? $t('chat.status.retrieving')
      : status === 'thinking' || status === 'submitting'
        ? $t('chat.status.thinking')
        : '';

  $: announcement =
    status === 'done'
      ? $t('chat.status.complete')
      : status === 'aborted'
        ? $t('chat.status.stopped')
        : status === 'error'
          ? $t('chat.message.errorGeneric')
          : label;
</script>

<!--
  The visible label is aria-hidden so the region announces ONCE. Previously both
  it and an identical sr-only copy sat inside the same live region, so every
  stage change was read twice.
-->
<div class="status-region" data-testid="chat-status">
  {#if label}
    <span class="shimmer" aria-hidden="true">{label}</span>
  {/if}
  <span class="sr-only" role="status" aria-live="polite">{announcement}</span>
</div>

<style>
  .status-region {
    min-height: 1.25rem;
    margin-bottom: 1rem;
  }

  .shimmer {
    display: inline-block;
    font-size: 0.85rem;
    color: var(--text-secondary);
    background: linear-gradient(
      90deg,
      var(--text-secondary) 25%,
      var(--text-color) 50%,
      var(--text-secondary) 75%
    );
    background-size: 200% 100%;
    -webkit-background-clip: text;
    background-clip: text;
    -webkit-text-fill-color: transparent;
    animation: chat-shimmer 1.6s linear infinite;
  }

  @keyframes chat-shimmer {
    from {
      background-position: 200% 0;
    }
    to {
      background-position: -200% 0;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .shimmer {
      animation: none;
      -webkit-text-fill-color: var(--text-secondary);
    }
  }

  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    margin: -1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
    border: 0;
  }
</style>
