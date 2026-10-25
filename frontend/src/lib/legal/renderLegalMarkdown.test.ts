import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

import { renderLegalMarkdown, unwrapParagraphs } from './renderLegalMarkdown';

const COMPONENTS = [
  'src/components/FirstRunWizard.svelte',
  'src/components/legal/LegalDocumentViewer.svelte',
];

describe('unwrapParagraphs', () => {
  it('joins soft-wrapped lines inside a paragraph and a list item', () => {
    const md =
      'These points summarize\nthe Terms, which apply\nto you.\n\n1. **Item.** You run\n   this deployment.';
    expect(unwrapParagraphs(md)).toBe(
      'These points summarize the Terms, which apply to you.\n\n1. **Item.** You run this deployment.'
    );
  });

  it('keeps headings, list markers, quotes and fenced code line by line', () => {
    const md = '# Title\ntext\n- one\n- two\n> quoted\n> more\n\n```\na\nb\n```';
    expect(unwrapParagraphs(md)).toBe(
      '# Title\ntext\n- one\n- two\n> quoted\n> more\n\n```\na\nb\n```'
    );
  });
});

describe('renderLegalMarkdown', () => {
  it('does not emit a line break for a soft wrap', async () => {
    const html = await renderLegalMarkdown('first half\nsecond half');
    expect(html).not.toContain('<br');
    expect(html).toContain('first half second half');
  });

  it('renders markdown to sanitized html', async () => {
    const html = await renderLegalMarkdown('# Title\n\n<script>alert(1)</script>text');
    expect(html).toContain('Title');
    expect(html).not.toContain('<script');
  });

  // A list interrupted by a quote or table resumes at its own number; the sanitizer used to
  // strip `start`, so a tenth clause was printed as "1.".
  it('keeps the start number of a resumed ordered list', async () => {
    const html = await renderLegalMarkdown('1. first\n\n> aside\n\n10. tenth');
    expect(html).toMatch(/<ol start="10">/);
  });

  // chatMarkdown builds DOMPurify from `window` at import time. A static import from a
  // component the root layout mounts therefore crashes server-side rendering (every page
  // answered 500 on the dev server) and drags marked and DOMPurify into the root bundle.
  it.each(COMPONENTS)('%s does not statically import the chat markdown module', (file) => {
    const source = readFileSync(resolve(process.cwd(), file), 'utf8');
    expect(source).not.toMatch(/import\s[^;]*from\s+['"]\$lib\/utils\/chatMarkdown['"]/);
  });
});
