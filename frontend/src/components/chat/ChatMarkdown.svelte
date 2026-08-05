<!--
  ChatMarkdown.svelte — renders assistant markdown.

  Streaming re-parses the FULL accumulated buffer each tick rather than trying to
  patch the DOM incrementally. That sounds wasteful but is what ChatGPT does and
  is the right call: markdown is not incrementally parseable (a half-written
  table or unclosed fence only resolves once more text arrives), and `marked`
  parses multi-KB in well under a millisecond. Renders are throttled to one per
  animation frame with a 100ms floor so a fast provider can't cause layout
  thrash, with one final unthrottled render when the stream completes.
-->
<script lang="ts">
  import { onDestroy, tick } from 'svelte';
  import { t } from '$stores/locale';
  import { renderChatMarkdown } from '$lib/utils/chatMarkdown';

  export let content = '';
  /** While true, renders are throttled and a caret is shown after the text. */
  export let streaming = false;

  const MIN_RENDER_INTERVAL_MS = 100;

  $: copyLabel = $t('chat.message.copy');
  $: copiedLabel = $t('chat.message.copied');

  let container: HTMLDivElement;
  let html = '';
  let lastRenderAt = 0;
  let frame: number | undefined;

  function render(): void {
    html = renderChatMarkdown(content);
    lastRenderAt = Date.now();
    frame = undefined;
    tick().then(attachCopyButtons);
  }

  function scheduleRender(): void {
    if (frame !== undefined) return;
    const elapsed = Date.now() - lastRenderAt;
    if (elapsed >= MIN_RENDER_INTERVAL_MS) {
      frame = requestAnimationFrame(render);
    } else {
      frame = window.setTimeout(
        () => requestAnimationFrame(render),
        MIN_RENDER_INTERVAL_MS - elapsed
      ) as unknown as number;
    }
  }

  $: if (content !== undefined) {
    if (streaming) {
      scheduleRender();
    } else {
      // Final state must never be a stale throttled frame.
      if (frame !== undefined) {
        cancelAnimationFrame(frame);
        clearTimeout(frame);
        frame = undefined;
      }
      render();
    }
  }

  /**
   * Attach a real copy button to every code block after each render.
   *
   * The earlier version drew the affordance with a CSS ::before and detected
   * clicks by comparing coordinates — which is invisible to screen readers and
   * unreachable by keyboard. A real <button> is injected instead. It has to be
   * (re)attached after each render because the sanitized HTML is replaced
   * wholesale on every streaming tick, so anything inside it is destroyed.
   */
  function attachCopyButtons(): void {
    if (!container) return;
    for (const pre of Array.from(container.querySelectorAll('pre'))) {
      if (pre.querySelector('.code-copy')) continue;

      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'code-copy';
      button.textContent = copyLabel;
      button.setAttribute('aria-label', copyLabel);
      button.addEventListener('click', async () => {
        const code = pre.querySelector('code')?.textContent ?? pre.textContent ?? '';
        if (!code.trim()) return;
        const { copyToClipboard } = await import('$lib/utils/clipboard');
        const result = await copyToClipboard(code);
        button.textContent = result.success ? copiedLabel : copyLabel;
        button.setAttribute('aria-label', result.success ? copiedLabel : copyLabel);
        setTimeout(() => {
          button.textContent = copyLabel;
          button.setAttribute('aria-label', copyLabel);
        }, 1500);
      });
      pre.appendChild(button);
    }
  }

  onDestroy(() => {
    if (frame !== undefined) {
      cancelAnimationFrame(frame);
      clearTimeout(frame);
    }
  });
</script>

<div class="chat-markdown" class:streaming bind:this={container}>
  <!-- Sanitized by renderChatMarkdown's dedicated DOMPurify profile. -->
  {@html html}
</div>

<style>
  .chat-markdown {
    color: var(--text-color);
    line-height: 1.65;
    font-size: 0.95rem;
    overflow-wrap: anywhere;
  }

  .chat-markdown :global(p) {
    margin: 0 0 0.75rem;
  }

  .chat-markdown :global(p:last-child) {
    margin-bottom: 0;
  }

  .chat-markdown :global(h1),
  .chat-markdown :global(h2),
  .chat-markdown :global(h3),
  .chat-markdown :global(h4) {
    margin: 1.25rem 0 0.5rem;
    font-weight: 600;
    line-height: 1.3;
  }

  .chat-markdown :global(h1) {
    font-size: 1.25rem;
  }
  .chat-markdown :global(h2) {
    font-size: 1.15rem;
  }
  .chat-markdown :global(h3) {
    font-size: 1.05rem;
  }

  .chat-markdown :global(ul),
  .chat-markdown :global(ol) {
    margin: 0 0 0.75rem;
    padding-left: 1.5rem;
  }

  .chat-markdown :global(li) {
    margin-bottom: 0.25rem;
  }

  .chat-markdown :global(blockquote) {
    margin: 0.75rem 0;
    padding: 0.25rem 0 0.25rem 0.85rem;
    border-left: 3px solid rgba(var(--primary-color-rgb), 0.45);
    color: var(--text-secondary);
  }

  .chat-markdown :global(code) {
    background-color: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: 4px;
    padding: 0.1em 0.35em;
    font-size: 0.875em;
    font-family: var(--font-mono, ui-monospace, 'SFMono-Regular', Menlo, monospace);
  }

  .chat-markdown :global(pre) {
    position: relative;
    background-color: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: 8px;
    padding: 0.85rem 1rem;
    overflow-x: auto;
    margin: 0 0 0.75rem;
  }

  .chat-markdown :global(.code-copy) {
    position: absolute;
    top: 0.35rem;
    right: 0.4rem;
    padding: 0.1rem 0.4rem;
    border: 1px solid var(--border-color);
    border-radius: 5px;
    background-color: var(--card-background);
    color: var(--text-secondary);
    font-size: 0.68rem;
    font-family: inherit;
    opacity: 0;
    transition: opacity 0.15s ease;
    cursor: pointer;
  }

  /* Visible on hover, and ALWAYS visible once focused — otherwise a keyboard
     user tabs onto a control they cannot see. */
  .chat-markdown :global(pre:hover .code-copy),
  .chat-markdown :global(.code-copy:focus-visible) {
    opacity: 1;
  }

  .chat-markdown :global(.code-copy:focus-visible) {
    outline: 2px solid var(--primary-color);
    outline-offset: 1px;
  }

  .chat-markdown :global(pre code) {
    background: none;
    border: none;
    padding: 0;
    font-size: 0.85rem;
  }

  .chat-markdown :global(table) {
    border-collapse: collapse;
    margin: 0 0 0.75rem;
    display: block;
    overflow-x: auto;
    max-width: 100%;
  }

  .chat-markdown :global(th),
  .chat-markdown :global(td) {
    border: 1px solid var(--border-color);
    padding: 0.4rem 0.65rem;
    text-align: left;
    font-size: 0.9rem;
  }

  .chat-markdown :global(th) {
    background-color: var(--surface-color);
    font-weight: 600;
  }

  .chat-markdown :global(a) {
    color: var(--primary-color);
    text-decoration: underline;
  }

  .chat-markdown :global(hr) {
    border: none;
    border-top: 1px solid var(--border-color);
    margin: 1rem 0;
  }

  /* Blinking caret while tokens are still arriving. */
  .chat-markdown.streaming :global(> *:last-child)::after {
    content: '';
    display: inline-block;
    width: 0.5em;
    height: 1em;
    margin-left: 0.15em;
    vertical-align: text-bottom;
    background-color: var(--primary-color);
    animation: chat-caret 1s step-end infinite;
  }

  @keyframes chat-caret {
    0%,
    100% {
      opacity: 1;
    }
    50% {
      opacity: 0;
    }
  }

  @media (prefers-reduced-motion: reduce) {
    .chat-markdown.streaming :global(> *:last-child)::after {
      animation: none;
      opacity: 1;
    }
  }
</style>
