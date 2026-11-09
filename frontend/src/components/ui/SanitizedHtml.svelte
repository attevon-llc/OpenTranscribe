<!--
  SanitizedHtml.svelte — the one place highlight / locale-rich-text HTML is injected.

  Call sites pass their markup as `html` instead of writing an inline raw-HTML tag around
  sanitizeHighlightHtml(). The DOMPurify pass lives in this component, so a caller cannot
  forget it, and the `svelte/no-at-html-tags` rule can stay at `error` everywhere else. It
  renders no wrapper element: the DOM is identical to the inline tag it replaced.

  Only for the highlight profile (mark/span/br/ul/li/em/strong/div/p). Markdown with tables
  or code blocks has its own dedicated profile (renderChatMarkdown, renderLegalMarkdown) and
  must not be re-sanitised with this narrower one.
-->
<script lang="ts">
  import { sanitizeHighlightHtml } from '$lib/utils/sanitizeHtml';

  export let html: string | null | undefined = '';

  $: clean = sanitizeHighlightHtml(html);
</script>

<!-- eslint-disable-next-line svelte/no-at-html-tags -- `clean` is the output of sanitizeHighlightHtml (DOMPurify), computed above -->
{@html clean}
