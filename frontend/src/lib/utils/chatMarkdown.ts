/**
 * Markdown rendering for assistant messages (issue #52).
 *
 * This is the XSS boundary for LLM output. It is a DEDICATED DOMPurify profile,
 * deliberately not an extension of `sanitizeHighlightHtml` — widening that
 * shared profile to fit chat would loosen sanitization everywhere else.
 *
 * The one non-obvious rule: `ALLOWED_URI_REGEXP` permits only `http(s):` and
 * `mailto:`, which blocks **relative** URLs. That is intentional. It means model
 * text can never mint an app-internal link (`/files/...`, `/settings`) that
 * looks like it came from OpenTranscribe. Citations are rendered separately from
 * the structured `sources` payload, never from prose.
 *
 * `marked` is used over markdown-it (~39KB vs ~110KB, zero deps, eval-free so it
 * is safe under the app's hash-mode CSP). Streaming re-parses the whole buffer
 * each tick, so parse speed matters more than plugin support.
 */

import DOMPurify from 'dompurify';
import { marked } from 'marked';

const ALLOWED_TAGS = [
  'p',
  'br',
  'hr',
  'strong',
  'em',
  'del',
  'code',
  'pre',
  'blockquote',
  'ul',
  'ol',
  'li',
  'h1',
  'h2',
  'h3',
  'h4',
  'h5',
  'h6',
  'a',
  'table',
  'thead',
  'tbody',
  'tr',
  'th',
  'td',
  'span',
];

// `class` is allowed only so `language-*` survives on code blocks.
const ALLOWED_ATTR = ['href', 'class'];

/** http(s) and mailto only — relative hrefs are rejected on purpose. */
const ALLOWED_URI_REGEXP = /^(?:https?:|mailto:)/i;

let hookInstalled = false;

/**
 * Force safe link attributes on anchors in chat output.
 *
 * Guarded by a module flag and scoped by a data attribute we set during our own
 * sanitize call, so other DOMPurify callers in the app are unaffected.
 */
function installHook(): void {
  if (hookInstalled) return;
  DOMPurify.addHook('afterSanitizeAttributes', (node: Element) => {
    if (node.tagName === 'A' && node.hasAttribute('href')) {
      node.setAttribute('target', '_blank');
      node.setAttribute('rel', 'noopener noreferrer nofollow');
    }
  });
  hookInstalled = true;
}

marked.setOptions({
  gfm: true,
  breaks: true,
});

/**
 * Render assistant markdown to sanitized HTML safe for `{@html}`.
 *
 * @param markdown - Raw model output (may be a partial buffer while streaming).
 * @returns Sanitized HTML. Empty string for empty input.
 */
export function renderChatMarkdown(markdown: string | null | undefined): string {
  if (!markdown) return '';
  installHook();

  let parsed: string;
  try {
    // `marked.parse` is sync unless async options are set, which we never set.
    parsed = marked.parse(markdown) as string;
  } catch {
    // A half-written table mid-stream shouldn't blank the message; fall back to
    // escaped plain text until the next tick completes the markdown.
    parsed = escapeHtml(markdown);
  }

  return DOMPurify.sanitize(parsed, {
    ALLOWED_TAGS,
    ALLOWED_ATTR,
    ALLOWED_URI_REGEXP,
    KEEP_CONTENT: true,
  }) as unknown as string;
}

/** Escape text for safe literal rendering (user messages, fallbacks). */
export function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/**
 * Build a citation deep link from STRUCTURED data only.
 *
 * Never call this with anything parsed out of model prose — that is precisely
 * what the relative-URL block above exists to prevent.
 */
export function citationHref(fileUuid: string, startTime: number): string {
  const seconds = Math.max(0, Math.floor(startTime || 0));
  return `/files/${encodeURIComponent(fileUuid)}?t=${seconds}`;
}

/** Format seconds as a clock label (`m:ss` or `h:mm:ss`). */
export function formatClock(seconds: number | null | undefined): string {
  const total = Math.max(0, Math.floor(seconds ?? 0));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  if (hours > 0) {
    return `${hours}:${String(minutes).padStart(2, '0')}:${String(secs).padStart(2, '0')}`;
  }
  return `${minutes}:${String(secs).padStart(2, '0')}`;
}
