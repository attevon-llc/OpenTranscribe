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

import createDOMPurify from 'dompurify';
import { marked } from 'marked';

import type { ChatSourceKind } from '$lib/types/chat';

/**
 * A DEDICATED DOMPurify instance.
 *
 * `DOMPurify.addHook` registers on the shared singleton, so a hook added here
 * would silently apply to every other caller in the app (notably
 * `sanitizeHtml.ts`, used for search snippets). An isolated instance keeps the
 * link-hardening below genuinely scoped to chat output.
 */
const chatPurify = createDOMPurify(window);

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
 * Force safe link attributes on anchors, and keep `class` to `language-*`.
 *
 * Registered on the chat-only instance above, so it cannot affect other
 * DOMPurify callers. The class restriction matters because `.btn` and friends
 * are GLOBAL classes in form-elements.css — without it, model output could
 * render text styled as real application chrome.
 */
function installHook(): void {
  if (hookInstalled) return;
  chatPurify.addHook('afterSanitizeAttributes', (node: Element) => {
    if (node.tagName === 'A' && node.hasAttribute('href')) {
      node.setAttribute('target', '_blank');
      node.setAttribute('rel', 'noopener noreferrer nofollow');
    }
    const className = node.getAttribute?.('class');
    if (className && !/^language-[\w-]+$/.test(className)) {
      node.removeAttribute('class');
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

  return chatPurify.sanitize(parsed, {
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
 * Kinds `citationHref` routes on — a strict superset of `ChatSourceKind`.
 * `document` is not yet a value `ChatSource.kind` can hold (that is the next
 * lane's own widening of the type, alongside its retrieval-side wiring); the
 * branch below is written now so landing it needs no `citationHref` edit.
 */
export type CitationLinkKind = ChatSourceKind | 'document';

/** The subset of `ChatSource` a citation link needs to decide its shape. */
export interface CitationLinkSource {
  file_uuid: string;
  kind?: CitationLinkKind;
  start_time?: number | null;
  digest_section?: number | null;
  chunk_index?: number;
  /** `kind === 'recurrence'` only — see `ChatSource.file_uuids`. */
  file_uuids?: string[] | null;
}

/**
 * Build a citation deep link from STRUCTURED data only, kind-aware (#464).
 *
 * Never call this with anything parsed out of model prose — that is precisely
 * what the relative-URL block above exists to prevent.
 *
 * - `summary`: no single moment to seek to — a summary describes the whole
 *   recording, not a turn in it — so this deep-links to the file's summary
 *   view (`?view=summary[&section=N]`, amendment c) instead of the player.
 * - `recurrence` (W2.5): spans MULTIPLE recordings, so there is no single
 *   file this could deep-link into meaningfully. Lands on the FIRST
 *   recording's own page (`file_uuids[0]`, falling back to `file_uuid`) with
 *   no `t=`/`view=` — "go look at one of the recordings this recurred in" is
 *   honest; a fabricated timestamp or view into one file would imply the
 *   whole group lives there.
 * - `document` (a later lane's kind, handled here so THAT lane needs no
 *   `citationHref` edit): `/documents/{uuid}?chunk=N`, **never**
 *   `start_time=0` — a document chunk has no timestamp, and a fabricated
 *   `t=0` would look like a working "jump to the start" link that lands
 *   nowhere meaningful in a player that isn't even showing.
 * - everything else (`chunk`, `digest`, and an absent `kind` for messages
 *   persisted before #403 Stage 4): unchanged — `/files/{uuid}?t={seconds}`.
 */
export function citationHref(source: CitationLinkSource): string {
  const uuid = encodeURIComponent(source.file_uuid);
  const kind = source.kind ?? 'chunk';

  if (kind === 'summary') {
    const section = source.digest_section;
    return section != null
      ? `/files/${uuid}?view=summary&section=${section}`
      : `/files/${uuid}?view=summary`;
  }
  if (kind === 'recurrence') {
    const first =
      source.file_uuids && source.file_uuids.length > 0 ? source.file_uuids[0] : source.file_uuid;
    return `/files/${encodeURIComponent(first)}`;
  }
  if (kind === 'document') {
    return `/documents/${uuid}?chunk=${source.chunk_index ?? 0}`;
  }

  const seconds = Math.max(0, Math.floor(source.start_time || 0));
  return `/files/${uuid}?t=${seconds}`;
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
