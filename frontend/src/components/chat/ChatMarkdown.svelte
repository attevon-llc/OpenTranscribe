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

<!-- dir="auto": an English answer in the ar (RTL) UI keeps its own direction. -->
<div class="chat-markdown" class:streaming dir="auto" bind:this={container}>
  <!-- Sanitized by renderChatMarkdown's dedicated DOMPurify profile. -->
  {@html html}
</div>

<style>
  /* Shared prose layer for model-authored text. Sizes, weights and measure come from the
     type tokens in styles/theme.css; see frontend/CLAUDE.md "Type and weight rules".
     Logical (inline-start/end) properties throughout, so lists and quotes mirror in RTL. */
  .chat-markdown {
    max-width: var(--measure-prose);
    color: var(--text-color);
    font-size: var(--font-size-body);
    font-weight: var(--font-weight-regular);
    line-height: var(--line-height-body);
    overflow-wrap: anywhere;
  }

  .chat-markdown :global(p) {
    margin: 0 0 0.75em;
  }

  .chat-markdown :global(p:last-child) {
    margin-bottom: 0;
  }

  .chat-markdown :global(h1),
  .chat-markdown :global(h2),
  .chat-markdown :global(h3),
  .chat-markdown :global(h4),
  .chat-markdown :global(h5),
  .chat-markdown :global(h6) {
    margin: 1.25em 0 0.5em;
    font-weight: var(--font-weight-semibold);
    line-height: var(--line-height-tight);
  }

  .chat-markdown :global(:is(h1, h2, h3, h4, h5, h6):first-child) {
    margin-top: 0;
  }

  .chat-markdown :global(h1) {
    font-size: var(--font-size-xl);
  }
  .chat-markdown :global(h2) {
    font-size: var(--font-size-lg);
  }
  .chat-markdown :global(h3),
  .chat-markdown :global(h4),
  .chat-markdown :global(h5),
  .chat-markdown :global(h6) {
    font-size: var(--font-size-body);
  }

  .chat-markdown :global(strong) {
    font-weight: var(--font-weight-semibold);
  }

  .chat-markdown :global(ul),
  .chat-markdown :global(ol) {
    margin: 0 0 0.75em;
    padding-inline-start: 1.5em;
  }

  .chat-markdown :global(li) {
    margin-bottom: 0.25em;
  }

  .chat-markdown :global(li > p) {
    margin: 0;
  }

  .chat-markdown :global(li::marker) {
    color: var(--text-secondary);
  }

  .chat-markdown :global(blockquote) {
    margin: 0.75em 0;
    padding-block: 0.25em;
    padding-inline: 0.85em 0;
    border-inline-start: 3px solid rgba(var(--primary-color-rgb), 0.45);
    color: var(--text-secondary);
  }

  .chat-markdown :global(code) {
    background-color: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: 4px;
    padding: 0.1em 0.35em;
    font-size: 0.875em;
    font-family: var(--font-mono);
  }

  /* Code is always left-to-right, whatever the page or message direction. */
  .chat-markdown :global(pre) {
    position: relative;
    direction: ltr;
    text-align: start;
    background-color: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: 8px;
    padding: 0.85rem 1rem;
    overflow-x: auto;
    margin: 0 0 0.75em;
  }

  .chat-markdown :global(.code-copy) {
    position: absolute;
    top: 0.35rem;
    inset-inline-end: 0.4rem;
    padding: 0.1rem 0.4rem;
    border: 1px solid var(--border-color);
    border-radius: 5px;
    background-color: var(--card-background);
    color: var(--text-secondary);
    font-size: var(--font-size-2xs);
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
    font-size: var(--font-size-md);
  }

  .chat-markdown :global(table) {
    border-collapse: collapse;
    margin: 0 0 0.75em;
    display: block;
    width: max-content;
    overflow-x: auto;
    max-width: 100%;
  }

  .chat-markdown :global(th),
  .chat-markdown :global(td) {
    border: 1px solid var(--border-color);
    padding: 0.4rem 0.65rem;
    text-align: start;
    font-size: var(--font-size-md);
  }

  .chat-markdown :global(th) {
    background-color: var(--surface-color);
    font-weight: var(--font-weight-semibold);
  }

  .chat-markdown :global(a) {
    color: var(--primary-on-surface);
    text-decoration: underline;
  }

  .chat-markdown :global(hr) {
    border: none;
    border-top: 1px solid var(--border-color);
    margin: 1em 0;
  }

  /* Blinking caret while tokens are still arriving. */
  .chat-markdown.streaming :global(> *:last-child)::after {
    content: '';
    display: inline-block;
    width: 0.5em;
    height: 1em;
    margin-inline-start: 0.15em;
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
