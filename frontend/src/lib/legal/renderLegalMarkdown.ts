const BLOCK_START = /^\s*([-*+]\s|\d+[.)]\s|#{1,6}\s|>|\||```|~~~|-{3,}\s*$|\*{3,}\s*$)/;

/**
 * Joins hard-wrapped paragraph lines.
 *
 * The vendored documents are wrapped at about 100 columns in source. The shared chat
 * renderer turns every single newline into a `<br>` (right for chat, wrong for a
 * contract), which leaves ragged half-lines. Only soft wraps inside a paragraph or a
 * list item are joined; headings, list markers, quotes, tables, rules and fenced code
 * keep their line structure.
 */
export function unwrapParagraphs(markdown: string): string {
  const out: string[] = [];
  let inFence = false;
  for (const line of markdown.split('\n')) {
    const fence = /^\s*(```|~~~)/.test(line);
    const previous = out[out.length - 1];
    const continues =
      !inFence &&
      !fence &&
      previous !== undefined &&
      previous.trim() !== '' &&
      line.trim() !== '' &&
      !BLOCK_START.test(line) &&
      !/^\s*(#{1,6}\s|\||-{3,}\s*$|\*{3,}\s*$)/.test(previous);
    if (continues) {
      out[out.length - 1] = `${previous.trimEnd()} ${line.trim()}`;
    } else {
      out.push(line);
    }
    if (fence) inFence = !inFence;
  }
  return out.join('\n');
}

/**
 * Renders a vendored legal document through the shared chat-markdown sanitizer.
 *
 * Imported lazily on purpose: the sanitizer builds its DOMPurify instance from `window`
 * at module load, so a static import from a component the root layout mounts crashes
 * server-side rendering, and it also pulls marked and DOMPurify into the root bundle.
 */
export async function renderLegalMarkdown(markdown: string): Promise<string> {
  const { renderChatMarkdown } = await import('$lib/utils/chatMarkdown');
  return renderChatMarkdown(unwrapParagraphs(markdown));
}
