/**
 * Pull the acceptance sentence out of a legal document.
 *
 * The document ends with a blockquote introduced by "Checkbox text:". The checkbox label is
 * taken from the served text, not a locale file, so what the operator ticks is always exactly
 * what the accepted (hashed) document says.
 */
export function extractCheckboxText(markdown: string | null | undefined): string | null {
  if (!markdown) return null;
  const match = /checkbox text:?\**/i.exec(markdown);
  if (!match) return null;
  const lines = markdown.slice(match.index + match[0].length).split('\n');
  const collected: string[] = [];
  for (const raw of lines) {
    const line = raw.replace(/^\s*>\s?/, '').trim();
    if (!line) {
      if (collected.length) break;
      continue;
    }
    collected.push(line);
  }
  const text = collected
    .join(' ')
    .replace(/^[*_"“]+|[*_"”]+$/g, '')
    .trim();
  return text || null;
}
