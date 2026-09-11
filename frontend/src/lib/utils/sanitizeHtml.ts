/**
 * HTML sanitization utility backed by DOMPurify.
 *
 * Use this for any `{@html ...}` directive that renders content derived from:
 * - API responses (LLM summaries, transcripts, speaker names, file titles, tags)
 * - User input (search queries, comments)
 * - Any string built via concatenation/interpolation with untrusted sources
 *
 * The existing search-highlight pipeline escapes text before wrapping it in
 * `<span>`/`<mark>` tags, which is safe when done correctly. Running everything
 * through DOMPurify as a final pass is defense-in-depth — if a future code
 * change forgets to escape, DOMPurify catches it.
 *
 * Do NOT use this for static literal HTML hard-coded in components (e.g., icon
 * SVGs defined inline). That's trusted content and doesn't need sanitization.
 */
import DOMPurify from 'dompurify';

/**
 * Allowed tags for search-highlighted content.
 * Covers the markup produced by the highlight pipeline plus common
 * inline formatting that may appear in LLM-generated summaries.
 */
const HIGHLIGHT_ALLOWED_TAGS = ['mark', 'span', 'br', 'ul', 'li', 'em', 'strong', 'div', 'p'];
const HIGHLIGHT_ALLOWED_ATTR = ['class', 'data-match-index', 'data-cat'];

/**
 * Sanitize an HTML string containing search highlight markup.
 * Safe to use with any `{@html}` directive that renders highlighted content.
 *
 * @param html - The HTML string to sanitize (may contain <mark>, <span>, etc.)
 * @returns A sanitized HTML string safe to render via `{@html}`
 */
export function sanitizeHighlightHtml(html: string | null | undefined): string {
  if (!html) return '';
  return DOMPurify.sanitize(html, {
    ALLOWED_TAGS: HIGHLIGHT_ALLOWED_TAGS,
    ALLOWED_ATTR: HIGHLIGHT_ALLOWED_ATTR,
    KEEP_CONTENT: true,
  }) as unknown as string;
}

/**
 * Sanitize to plain text — strips all HTML tags but keeps the text content.
 * Use when you want to guarantee no HTML is rendered, but still want the
 * safety of going through DOMPurify.
 *
 * @param html - The HTML string to sanitize
 * @returns Plain text with all tags removed
 */
export function sanitizeToPlainText(html: string | null | undefined): string {
  if (!html) return '';
  return DOMPurify.sanitize(html, {
    ALLOWED_TAGS: [],
    ALLOWED_ATTR: [],
    KEEP_CONTENT: true,
  }) as unknown as string;
}

/**
 * Escape HTML-significant characters for safe literal rendering inside a
 * `{@html}` string (e.g. wrapping matched text in a `<mark>`/`<span>` before
 * handing the whole string to `sanitizeHighlightHtml`).
 *
 * This is the single canonical implementation — issue #840 found five
 * near-duplicate copies across `TranscriptModal.svelte`, `SummaryDisplay.svelte`,
 * `SearchTranscriptModal.svelte`, `searchHighlight.ts`, and `chatMarkdown.ts`.
 * Two divergent shapes existed:
 *   - A DOM-based version (`div.textContent = text; return div.innerHTML`) used
 *     by `TranscriptModal.svelte`/`SummaryDisplay.svelte`/`searchHighlight.ts`.
 *     The browser's HTML text-serialization only escapes `&`/`<`/`>` — it does
 *     NOT escape quotes, because quotes are only meaningful inside an attribute
 *     value, never in text content.
 *   - This regex chain, which additionally escapes `"` and `'`. All of this
 *     module's callers only ever place the result in TEXT CONTENT (inside a
 *     `<span>`/`<mark>`, never inside an attribute value), so escaping quotes is
 *     a no-op for rendered output there — entities decode back to the literal
 *     character regardless of position. This shape was chosen as the survivor
 *     because it has no DOM dependency (testable without jsdom, and correct if
 *     this ever runs somewhere `document` is unavailable) and is the shape
 *     `chatMarkdown.ts` already exported.
 *
 * @param text - The text to escape.
 * @returns HTML-safe text with `&`, `<`, `>`, `"`, `'` entity-encoded.
 */
export function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}
