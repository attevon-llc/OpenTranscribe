import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/svelte';
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join, relative } from 'node:path';
import SanitizedHtml from './SanitizedHtml.svelte';

describe('SanitizedHtml', () => {
  it('keeps highlight markup', () => {
    const { container } = render(SanitizedHtml, {
      props: { html: 'a <mark class="search-highlight">b</mark> c' },
    });
    expect(container.querySelector('mark.search-highlight')?.textContent).toBe('b');
  });

  it('strips script elements and event handlers', () => {
    const { container } = render(SanitizedHtml, {
      props: {
        html: '<span onclick="alert(1)">x</span><script>alert(2)</script><img src=x onerror=alert(3)>',
      },
    });
    expect(container.querySelector('script')).toBeNull();
    expect(container.querySelector('img')).toBeNull();
    expect(container.innerHTML).not.toMatch(/onclick|onerror/i);
    expect(container.textContent).toContain('x');
  });

  it('renders nothing for null', () => {
    const { container } = render(SanitizedHtml, { props: { html: null } });
    expect(container.textContent).toBe('');
  });
});

/**
 * `svelte/no-at-html-tags` is an error. This pins the complete set of files that may hold a
 * raw-HTML tag, so a new one cannot appear without its sanitiser being named here.
 */
describe('raw HTML sinks', () => {
  const ALLOWED = new Map<string, string>([
    ['components/ui/SanitizedHtml.svelte', 'sanitizeHighlightHtml'],
    ['components/chat/ChatMarkdown.svelte', 'renderChatMarkdown'],
    ['components/legal/LegalProse.svelte', 'renderLegalMarkdown'],
  ]);
  const SRC = join(__dirname, '..', '..');

  function walk(dir: string, out: string[]): string[] {
    for (const name of readdirSync(dir)) {
      const full = join(dir, name);
      if (statSync(full).isDirectory()) walk(full, out);
      else if (full.endsWith('.svelte')) out.push(full);
    }
    return out;
  }

  it('only the allow-listed components contain a raw HTML tag', () => {
    const found = walk(SRC, [])
      .filter((f) => /\{@html\s/.test(readFileSync(f, 'utf8')))
      .map((f) => relative(SRC, f))
      .sort();
    expect(found).toEqual([...ALLOWED.keys()].sort());
  });

  it.each([...ALLOWED])('%s names its sanitiser (%s)', (file, sanitiser) => {
    expect(readFileSync(join(SRC, file), 'utf8')).toContain(sanitiser);
  });
});
