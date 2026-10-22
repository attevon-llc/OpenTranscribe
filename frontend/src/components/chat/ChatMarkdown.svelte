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
  import { loadChatMath, mathPending, renderChatMarkdown } from '$lib/utils/chatMarkdown';

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
  let destroyed = false;

  function render(): void {
    html = renderChatMarkdown(content);
    lastRenderAt = Date.now();
    frame = undefined;
    tick().then(attachCodeChrome);
    // Math shows its TeX source until KaTeX (lazy) arrives; render once more when it does,
    // since a finished message gets no further content change to trigger it.
    if (mathPending(content)) {
      loadChatMath().then(() => {
        if (!destroyed && frame === undefined) render();
      });
    }
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
   * Give every code block a header: its language label and a real copy button.
   *
   * The copy affordance was once a CSS ::before whose clicks were detected by
   * coordinates — invisible to screen readers and unreachable by keyboard — so a real
   * <button> is used. The header is injected INSIDE the <pre> (never by re-parenting it):
   * `{@html}` owns its top-level nodes and removes exactly those on the next render, so a
   * wrapper inserted around them would be orphaned and duplicated on every streaming tick.
   * The sanitized HTML is replaced wholesale per render, so this runs after each one.
   */
  function attachCodeChrome(): void {
    if (!container) return;
    for (const pre of Array.from(container.querySelectorAll('pre'))) {
      if (pre.querySelector(':scope > .code-header')) continue;
      const code = pre.querySelector('code');

      const header = document.createElement('div');
      header.className = 'code-header';

      const language = /(?:^|\s)language-([\w-]+)/.exec(code?.className ?? '')?.[1];
      if (language) {
        const label = document.createElement('span');
        label.className = 'code-lang';
        label.textContent = language;
        header.appendChild(label);
      }

      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'code-copy';
      button.textContent = copyLabel;
      button.setAttribute('aria-label', copyLabel);
      button.addEventListener('click', async () => {
        const text = code?.textContent ?? '';
        if (!text.trim()) return;
        const { copyToClipboard } = await import('$lib/utils/clipboard');
        const result = await copyToClipboard(text);
        button.textContent = result.success ? copiedLabel : copyLabel;
        button.setAttribute('aria-label', result.success ? copiedLabel : copyLabel);
        setTimeout(() => {
          button.textContent = copyLabel;
          button.setAttribute('aria-label', copyLabel);
        }, 1500);
      });
      header.appendChild(button);
      pre.insertBefore(header, pre.firstChild);
    }
  }

  onDestroy(() => {
    destroyed = true;
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
  /* Reading text is set in the system text face (`--font-family`), not the app's Poppins:
     Poppins is a geometric display face whose regular weight reads like other faces'
     medium, which is why multi-paragraph answers looked bold. Grayscale smoothing keeps
     light-on-dark text from blooming heavier than the same weight on light. */
  .chat-markdown {
    max-width: var(--measure-prose);
    color: var(--text-color);
    font-family: var(--font-family);
    font-size: var(--font-size-body);
    font-weight: var(--font-weight-regular);
    line-height: var(--line-height-body);
    overflow-wrap: anywhere;
    -webkit-font-smoothing: antialiased;
    -moz-osx-font-smoothing: grayscale;
    /* Syntax colours: blue / green / orange / teal / grey, no purple (noPurple.test.ts).
       Light values meet 4.5:1 on --surface-color; the dark set is below. */
    --hl-keyword: #0550ae;
    --hl-string: #116329;
    --hl-number: #953800;
    --hl-title: #0e6e7a;
    --hl-comment: #57606a;
    --hl-meta: #6e5600;
    --hl-deletion: #b42318;
    --hl-addition: #116329;
  }

  :global([data-theme='dark']) .chat-markdown {
    --hl-keyword: #79b8ff;
    --hl-string: #85e89d;
    --hl-number: #ffab70;
    --hl-title: #56d4dd;
    --hl-comment: #959da5;
    --hl-meta: #e3c26b;
    --hl-deletion: #fd8c73;
    --hl-addition: #85e89d;
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

  /* Code is always left-to-right, whatever the page or message direction. The <pre> clips
     and its <code> scrolls, so the header (injected inside the <pre>) stays put while a
     long line scrolls horizontally beneath it. */
  .chat-markdown :global(pre) {
    position: relative;
    direction: ltr;
    text-align: start;
    background-color: var(--surface-color);
    border: 1px solid var(--border-color);
    border-radius: 8px;
    padding: 0;
    overflow: hidden;
    margin: 0 0 0.75em;
  }

  .chat-markdown :global(pre code) {
    display: block;
    overflow-x: auto;
    padding: 0.75rem 1rem 0.85rem;
    background: none;
    border: none;
    border-radius: 0;
    font-size: var(--font-size-md);
    line-height: 1.55;
    color: var(--text-color);
  }

  .chat-markdown :global(.code-header) {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.3rem 0.4rem 0.3rem 1rem;
    border-bottom: 1px solid var(--border-color);
    background-color: var(--card-background);
    font-family: var(--font-family);
  }

  .chat-markdown :global(.code-lang) {
    font-size: var(--font-size-xs);
    color: var(--text-secondary);
    text-transform: lowercase;
  }

  .chat-markdown :global(.code-copy) {
    margin-inline-start: auto;
    padding: 0.15rem 0.5rem;
    border: 1px solid transparent;
    border-radius: 5px;
    background-color: transparent;
    /* Flat ghost control: cancel form-elements.css's global button shadow / hover scale. */
    box-shadow: none;
    color: var(--text-secondary);
    font-size: var(--font-size-xs);
    font-family: inherit;
    cursor: pointer;
  }

  .chat-markdown :global(.code-copy:hover) {
    border-color: var(--border-color);
    background-color: transparent;
    box-shadow: none;
    transform: none;
    color: var(--text-color);
  }

  .chat-markdown :global(.code-copy:focus-visible) {
    outline: 2px solid var(--primary-color);
    outline-offset: 1px;
  }

  /* highlight.js token classes (only these survive chatMarkdown's class allowlist). */
  .chat-markdown :global(:is(.hljs-keyword, .hljs-selector-tag, .hljs-type, .hljs-doctag)) {
    color: var(--hl-keyword);
  }
  .chat-markdown :global(:is(.hljs-string, .hljs-regexp, .hljs-template-tag, .hljs-symbol)) {
    color: var(--hl-string);
  }
  .chat-markdown
    :global(:is(.hljs-number, .hljs-literal, .hljs-variable, .hljs-template-variable)) {
    color: var(--hl-number);
  }
  .chat-markdown
    :global(:is(.hljs-title, .hljs-section, .hljs-name, .hljs-attr, .hljs-attribute)) {
    color: var(--hl-title);
  }
  .chat-markdown :global(:is(.hljs-built_in, .hljs-selector-class, .hljs-selector-id)) {
    color: var(--hl-number);
  }
  .chat-markdown :global(:is(.hljs-comment, .hljs-quote)) {
    color: var(--hl-comment);
    font-style: italic;
  }
  .chat-markdown :global(:is(.hljs-meta, .hljs-tag, .hljs-subst, .hljs-params)) {
    color: var(--hl-meta);
  }
  .chat-markdown :global(.hljs-deletion) {
    color: var(--hl-deletion);
  }
  .chat-markdown :global(.hljs-addition) {
    color: var(--hl-addition);
  }
  .chat-markdown :global(.hljs-emphasis) {
    font-style: italic;
  }
  .chat-markdown :global(.hljs-strong) {
    font-weight: var(--font-weight-semibold);
  }

  /* Math is native MathML (KaTeX `output: 'mathml'`), so no KaTeX stylesheet is needed. */
  .chat-markdown :global(math) {
    font-family: 'Latin Modern Math', 'STIX Two Math', 'Cambria Math', math;
    font-size: 1.05em;
  }

  .chat-markdown :global(.chat-math-display) {
    display: block;
    margin: 0.5em 0 0.75em;
    overflow-x: auto;
    overflow-y: hidden;
    direction: ltr;
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
