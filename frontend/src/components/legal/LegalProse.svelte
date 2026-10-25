<!--
  LegalProse.svelte — the one place legal documents are typeset.

  Takes HTML already sanitized by `renderLegalMarkdown` and gives it document typography:
  paragraph rhythm, a heading scale, hanging-indent list numbers, legal-style nesting
  (1. / (a) / (i)), scrollable tables. The global reset in `app.html` zeroes every margin and
  padding, so nothing here can rely on browser defaults: a list with no gutter rule hangs its
  numbers outside the text column and nests flat (the defect this component replaced).

  The text is English, the official version, so it stays `dir="ltr"` in every UI locale; the
  rules still use logical properties so a translated document would mirror correctly.
-->
<script lang="ts">
  /** Sanitized HTML from `renderLegalMarkdown`. */
  export let html = '';
  /** `compact` is the in-dialog excerpt (smaller type, no measure cap). */
  export let density: 'comfortable' | 'compact' = 'comfortable';
</script>

<!-- eslint-disable-next-line svelte/no-at-html-tags -- sanitized by renderLegalMarkdown -->
<div class="legal-prose" class:compact={density === 'compact'} dir="ltr" data-testid="legal-prose">{@html html}</div>

<style>
  @counter-style legal-alpha {
    system: extends lower-alpha;
    prefix: '(';
    suffix: ') ';
  }

  @counter-style legal-roman {
    system: extends lower-roman;
    prefix: '(';
    suffix: ') ';
  }

  .legal-prose {
    max-inline-size: var(--measure-prose);
    color: var(--text-primary);
    font-size: var(--font-size-body);
    line-height: var(--line-height-body);
    text-align: start;
    overflow-wrap: break-word;
  }

  .legal-prose.compact {
    max-inline-size: none;
    font-size: var(--font-size-md);
  }

  .legal-prose > :global(:first-child) {
    margin-block-start: 0;
  }

  .legal-prose > :global(:last-child) {
    margin-block-end: 0;
  }

  .legal-prose :global(p) {
    margin: 0 0 0.85em;
  }

  .legal-prose :global(h1),
  .legal-prose :global(h2),
  .legal-prose :global(h3),
  .legal-prose :global(h4),
  .legal-prose :global(h5),
  .legal-prose :global(h6) {
    color: var(--text-primary);
    font-weight: var(--font-weight-semibold);
    line-height: var(--line-height-tight);
  }

  .legal-prose :global(h1) {
    margin: 0 0 0.75em;
    font-size: var(--font-size-xl);
  }

  /* More space above a section than below it, so a heading reads as belonging to its text. */
  .legal-prose :global(h2) {
    margin: 1.75em 0 0.6em;
    font-size: var(--font-size-lg);
  }

  .legal-prose :global(h3) {
    margin: 1.4em 0 0.45em;
    font-size: var(--font-size-body);
  }

  .legal-prose :global(h4),
  .legal-prose :global(h5),
  .legal-prose :global(h6) {
    margin: 1.2em 0 0.4em;
    font-size: var(--font-size-md);
  }

  .legal-prose.compact :global(h1) {
    font-size: var(--font-size-lg);
  }

  .legal-prose.compact :global(h2) {
    font-size: var(--font-size-body);
  }

  .legal-prose.compact :global(h3) {
    font-size: var(--font-size-md);
  }

  .legal-prose :global(strong),
  .legal-prose :global(b) {
    font-weight: var(--font-weight-semibold);
  }

  /* Hanging indent: markers sit outside, right-aligned in the gutter, so "9." and "10."
     line up on the period and wrapped lines align with the first line of text. */
  .legal-prose :global(ol),
  .legal-prose :global(ul) {
    margin: 0 0 0.85em;
    padding-inline-start: 2.25em;
    list-style-position: outside;
  }

  .legal-prose :global(ol) {
    list-style-type: decimal;
  }

  .legal-prose :global(ul) {
    list-style-type: disc;
  }

  .legal-prose :global(ol ol) {
    list-style-type: legal-alpha;
    padding-inline-start: 2.5em;
  }

  .legal-prose :global(ol ol ol) {
    list-style-type: legal-roman;
    padding-inline-start: 2.75em;
  }

  .legal-prose :global(ul ul) {
    list-style-type: circle;
  }

  .legal-prose :global(li) {
    margin: 0 0 0.5em;
    padding-inline-start: 0.25em;
  }

  .legal-prose :global(li::marker) {
    color: var(--text-secondary);
    font-variant-numeric: tabular-nums;
  }

  .legal-prose :global(ol > li::marker) {
    color: var(--text-primary);
    font-weight: var(--font-weight-medium);
  }

  .legal-prose :global(li > p) {
    margin: 0 0 0.5em;
  }

  .legal-prose :global(li > :last-child) {
    margin-block-end: 0;
  }

  .legal-prose :global(li > ol),
  .legal-prose :global(li > ul) {
    margin-block: 0.5em 0;
  }

  .legal-prose :global(a) {
    color: var(--primary-on-surface);
    text-decoration: underline;
    text-decoration-thickness: 1px;
    text-underline-offset: 0.18em;
  }

  .legal-prose :global(a:hover) {
    text-decoration-thickness: 2px;
  }

  .legal-prose :global(blockquote) {
    margin: 0 0 0.85em;
    padding-block: 0.15em;
    padding-inline-start: 1em;
    border-inline-start: 3px solid var(--border-color);
    color: var(--text-secondary);
  }

  .legal-prose :global(hr) {
    margin: 1.75em 0;
    border: none;
    border-block-start: 1px solid var(--border-color);
  }

  .legal-prose :global(code) {
    padding: 0.1em 0.35em;
    border-radius: 4px;
    background-color: var(--surface-secondary);
    font-family: var(--font-mono);
    font-size: 0.9em;
    overflow-wrap: anywhere;
  }

  /* A block that scrolls sideways on a phone instead of breaking words mid-cell. */
  /* `max-content` so the block hugs the columns; the global table rule's card shadow, radius and
     full width would otherwise frame an empty strip beside a narrow table. */
  .legal-prose :global(table) {
    display: block;
    width: max-content;
    max-width: 100%;
    border-radius: 0;
    background-color: transparent;
    box-shadow: none;
    margin: 0 0 1em;
    overflow-x: auto;
    border-collapse: collapse;
    font-size: var(--font-size-md);
  }

  .legal-prose :global(th),
  .legal-prose :global(td) {
    min-width: 7em;
    padding: 0.5em 0.75em;
    border: 1px solid var(--border-color);
    text-align: start;
    vertical-align: top;
    word-break: normal;
    overflow-wrap: normal;
  }

  .legal-prose :global(th) {
    background-color: var(--bg-secondary);
    font-weight: var(--font-weight-semibold);
  }
</style>
