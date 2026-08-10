/**
 * Markdown/XSS boundary for assistant output (issue #52).
 *
 * Assistant messages are the only place in the app where model-authored text is
 * rendered as HTML, so these tests are the guard on that boundary.
 */

import { describe, expect, it } from 'vitest';

import { citationHref, escapeHtml, formatClock, renderChatMarkdown } from './chatMarkdown';

describe('renderChatMarkdown — XSS', () => {
  it('strips script tags', () => {
    const html = renderChatMarkdown('Hello <script>alert(1)</script> world');
    expect(html).not.toContain('<script');
    expect(html).not.toContain('alert(1)');
  });

  it('strips event handlers', () => {
    const html = renderChatMarkdown('<img src=x onerror="alert(1)">');
    expect(html).not.toContain('onerror');
  });

  it('blocks javascript: hrefs', () => {
    const html = renderChatMarkdown('[click me](javascript:alert(1))');
    expect(html).not.toContain('javascript:');
  });

  it('blocks data: hrefs', () => {
    const html = renderChatMarkdown('[x](data:text/html;base64,PHNjcmlwdD4=)');
    expect(html).not.toContain('data:text/html');
  });

  it('blocks RELATIVE hrefs so model text cannot mint app-internal links', () => {
    // The whole point: an answer must not be able to fabricate a link that looks
    // like it came from OpenTranscribe.
    const html = renderChatMarkdown('[your settings](/settings) and [a file](/files/abc)');
    expect(html).not.toContain('href="/settings"');
    expect(html).not.toContain('href="/files/abc"');
    // Link text survives; only the destination is dropped.
    expect(html).toContain('your settings');
  });

  it('allows https and mailto links', () => {
    const html = renderChatMarkdown('[docs](https://example.com) [mail](mailto:a@b.com)');
    expect(html).toContain('https://example.com');
    expect(html).toContain('mailto:a@b.com');
  });

  it('forces target and rel on allowed links', () => {
    const html = renderChatMarkdown('[docs](https://example.com)');
    expect(html).toContain('target="_blank"');
    expect(html).toContain('rel="noopener noreferrer nofollow"');
  });

  it('strips iframes and objects', () => {
    const html = renderChatMarkdown('<iframe src="https://evil.test"></iframe><object></object>');
    expect(html).not.toContain('<iframe');
    expect(html).not.toContain('<object');
  });

  it('strips style attributes and tags', () => {
    const html = renderChatMarkdown('<div style="position:fixed">x</div><style>body{}</style>');
    expect(html).not.toContain('style=');
    expect(html).not.toContain('<style');
  });

  it('does not allow form elements', () => {
    const html = renderChatMarkdown('<form><input name="password"></form>');
    expect(html).not.toContain('<form');
    expect(html).not.toContain('<input');
  });
});

describe('renderChatMarkdown — formatting', () => {
  it('renders emphasis and strong', () => {
    const html = renderChatMarkdown('**bold** and *italic*');
    expect(html).toContain('<strong>bold</strong>');
    expect(html).toContain('<em>italic</em>');
  });

  it('renders lists', () => {
    const html = renderChatMarkdown('- one\n- two');
    expect(html).toContain('<ul>');
    expect(html).toContain('<li>one</li>');
  });

  it('renders GFM tables', () => {
    const html = renderChatMarkdown('| a | b |\n| --- | --- |\n| 1 | 2 |');
    expect(html).toContain('<table>');
    expect(html).toContain('<td>1</td>');
  });

  it('renders fenced code with a language class', () => {
    const html = renderChatMarkdown('```python\nprint("hi")\n```');
    expect(html).toContain('<pre>');
    expect(html).toContain('language-python');
  });

  it('renders blockquotes for transcript quotes', () => {
    const html = renderChatMarkdown('> we agreed to ship on Tuesday');
    expect(html).toContain('<blockquote>');
  });

  it('keeps citation markers as literal text', () => {
    // Citations render from structured data; the marker itself is just text.
    const html = renderChatMarkdown('The budget was approved [1] on Tuesday [2].');
    expect(html).toContain('[1]');
    expect(html).toContain('[2]');
  });

  it('handles partial markdown mid-stream without throwing', () => {
    expect(() => renderChatMarkdown('| a | b |\n| --- ')).not.toThrow();
    expect(() => renderChatMarkdown('**unclosed bold')).not.toThrow();
    expect(() => renderChatMarkdown('```python\nprint(')).not.toThrow();
  });

  it('returns empty string for empty input', () => {
    expect(renderChatMarkdown('')).toBe('');
    expect(renderChatMarkdown(null)).toBe('');
    expect(renderChatMarkdown(undefined)).toBe('');
  });
});

describe('escapeHtml', () => {
  it('escapes all HTML-significant characters', () => {
    expect(escapeHtml('<a href="x">&\'</a>')).toBe(
      '&lt;a href=&quot;x&quot;&gt;&amp;&#39;&lt;/a&gt;'
    );
  });
});

describe('citationHref', () => {
  it('builds a seekable file link', () => {
    expect(citationHref('abc-123', 90.7)).toBe('/files/abc-123?t=90');
  });

  it('encodes the uuid', () => {
    expect(citationHref('a/b?c', 0)).toBe('/files/a%2Fb%3Fc?t=0');
  });

  it('never emits a negative timestamp', () => {
    expect(citationHref('abc', -5)).toBe('/files/abc?t=0');
  });
});

describe('formatClock', () => {
  it('formats under an hour as m:ss', () => {
    expect(formatClock(0)).toBe('0:00');
    expect(formatClock(75)).toBe('1:15');
  });

  it('formats over an hour as h:mm:ss', () => {
    expect(formatClock(3725)).toBe('1:02:05');
  });

  it('handles null and negative input', () => {
    expect(formatClock(null)).toBe('0:00');
    expect(formatClock(-10)).toBe('0:00');
  });
});
