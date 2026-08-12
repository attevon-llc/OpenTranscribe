/**
 * Tests for `$lib/utils/sanitizeHtml` — the last line of defence for 36 `{@html}`
 * sites.
 *
 * DEFECT THESE CATCH: this module had NO test file at all, while
 * `sanitizeHighlightHtml` is the sanitizer for every `{@html}` in the search,
 * summary and topics surfaces. Its whole value is the ALLOWED_TAGS /
 * ALLOWED_ATTR pair, and a one-word edit to either list — adding `a`, adding
 * `onclick`, dropping `data-match-index` — is invisible in review and breaks
 * either security or the highlight UI with nothing failing.
 *
 * Two directions matter equally:
 *   - Nothing executable survives (event handlers, script, img/onerror, iframe).
 *   - The highlight pipeline's own markup DOES survive (`mark`, `class`,
 *     `data-match-index`, `data-cat`) — over-stripping silently breaks
 *     click-to-seek on a search hit, which no type-check catches.
 *
 * NOTE ON MODEL-AUTHORED HTML: `SummaryDisplay.svelte` and `TopicsList.svelte`
 * render LLM-generated summaries, key decisions, follow-ups and topics through
 * `sanitizeHighlightHtml` — NOT through `renderChatMarkdown`'s dedicated chat
 * profile. frontend/CLAUDE.md's claim that assistant chat output is "the only
 * model-authored HTML in the app" is therefore wrong. The saving grace is that
 * this profile allows no `a`/`href`/`src` at all, so model text cannot mint a
 * link here either; the tests below pin that, because it is the property the
 * doc's guarantee actually rests on.
 */

import { describe, it, expect } from 'vitest';
import { sanitizeHighlightHtml, sanitizeToPlainText } from '$lib/utils/sanitizeHtml';

describe('sanitizeHighlightHtml — blocks execution', () => {
  it('strips an <img onerror> payload entirely', () => {
    const out = sanitizeHighlightHtml('<img src=x onerror="alert(1)">');

    expect(out).not.toContain('onerror');
    expect(out).not.toContain('<img');
    expect(out).not.toContain('alert');
  });

  it('drops onclick from an ALLOWED tag — the tag survives, the handler does not', () => {
    const out = sanitizeHighlightHtml('<span class="hit" onclick="steal()">text</span>');

    // The span is allowed, so this is the dangerous case: a surviving element
    // that carries a surviving handler.
    expect(out).toContain('<span');
    expect(out).toContain('text');
    expect(out).not.toContain('onclick');
    expect(out).not.toContain('steal');
  });

  it.each([
    ['onmouseover', '<span onmouseover="x()">t</span>'],
    ['onfocus', '<span onfocus="x()" tabindex="0">t</span>'],
    ['onload', '<div onload="x()">t</div>'],
    ['onanimationstart', '<div onanimationstart="x()">t</div>'],
  ])('drops the %s handler', (handler, html) => {
    expect(sanitizeHighlightHtml(html)).not.toContain(handler);
  });

  it('removes <script> and its contents', () => {
    const out = sanitizeHighlightHtml('before<script>alert(1)</script>after');

    expect(out).not.toContain('script');
    expect(out).not.toContain('alert(1)');
    expect(out).toContain('before');
    expect(out).toContain('after');
  });

  it('removes an <iframe> without keeping its src', () => {
    const out = sanitizeHighlightHtml('<iframe src="https://evil.example"></iframe>');

    expect(out).not.toContain('iframe');
    expect(out).not.toContain('evil.example');
  });

  it('cannot mint a link: <a href> is not in the allowlist, so it is unwrapped', () => {
    // This is what makes routing LLM summaries through the WEAKER profile
    // tolerable — model text can never produce a clickable app-internal or
    // external URL here.
    const out = sanitizeHighlightHtml('<a href="/admin/users">Click me</a>');

    expect(out).not.toContain('<a');
    expect(out).not.toContain('href');
    // KEEP_CONTENT: true, so the visible text is preserved.
    expect(out).toContain('Click me');
  });

  it('drops javascript: URLs along with the element carrying them', () => {
    const out = sanitizeHighlightHtml('<a href="javascript:alert(1)">x</a>');

    expect(out.toLowerCase()).not.toContain('javascript:');
  });

  it('drops href/src even when placed on an ALLOWED tag', () => {
    const out = sanitizeHighlightHtml('<span href="/x" src="/y" style="color:red">t</span>');

    expect(out).toContain('<span');
    expect(out).not.toContain('href');
    expect(out).not.toContain('src');
    // `style` is not in ALLOWED_ATTR either — inline style is a CSS-injection surface.
    expect(out).not.toContain('style');
  });

  it('strips <style>, which can exfiltrate via attribute selectors', () => {
    const out = sanitizeHighlightHtml('<style>span{background:url(//evil.example)}</style>hi');

    expect(out).not.toContain('style');
    expect(out).not.toContain('evil.example');
  });
});

describe('sanitizeHighlightHtml — preserves highlight markup', () => {
  it('keeps <mark> with its class and data-match-index', () => {
    // Over-stripping here breaks click-to-seek on a search hit — a silent UI
    // regression, since the text still renders.
    const out = sanitizeHighlightHtml(
      '<mark class="search-hit" data-match-index="3" data-cat="speaker">hello</mark>'
    );

    expect(out).toContain('<mark');
    expect(out).toContain('class="search-hit"');
    expect(out).toContain('data-match-index="3"');
    expect(out).toContain('data-cat="speaker"');
    expect(out).toContain('hello');
  });

  it.each(['mark', 'span', 'br', 'ul', 'li', 'em', 'strong', 'div', 'p'])(
    'keeps the allowed tag <%s>',
    (tag) => {
      const html = tag === 'br' ? '<br>' : `<${tag}>x</${tag}>`;
      expect(sanitizeHighlightHtml(html)).toContain(`<${tag}`);
    }
  );

  it('keeps the nested list markup an LLM summary produces', () => {
    const out = sanitizeHighlightHtml(
      '<ul><li><strong>Decision:</strong> ship on <em>Tuesday</em></li></ul>'
    );

    expect(out).toContain('<ul>');
    expect(out).toContain('<li>');
    expect(out).toContain('<strong>');
    expect(out).toContain('<em>');
    expect(out).toContain('ship on');
  });

  it('leaves already-escaped entities as text rather than re-decoding them', () => {
    // The highlight pipeline escapes before wrapping; DOMPurify is the second
    // pass. Double-decoding here would resurrect the tag it just escaped.
    const out = sanitizeHighlightHtml('&lt;script&gt;alert(1)&lt;/script&gt;');

    expect(out).not.toContain('<script');
    expect(out).toContain('&lt;script&gt;');
  });

  it('returns empty string for null, undefined and empty input', () => {
    expect(sanitizeHighlightHtml(null)).toBe('');
    expect(sanitizeHighlightHtml(undefined)).toBe('');
    expect(sanitizeHighlightHtml('')).toBe('');
  });
});

describe('sanitizeToPlainText', () => {
  it('removes every tag but keeps the text', () => {
    const out = sanitizeToPlainText('<p>Hello <strong>world</strong></p>');

    expect(out).not.toContain('<');
    expect(out).toContain('Hello');
    expect(out).toContain('world');
  });

  it('drops script contents rather than surfacing them as visible text', () => {
    // KEEP_CONTENT would otherwise print the script body to the user.
    const out = sanitizeToPlainText('<script>alert(1)</script>visible');

    expect(out).not.toContain('alert(1)');
    expect(out).toContain('visible');
  });

  it('keeps no attributes, since it keeps no tags', () => {
    const out = sanitizeToPlainText('<span class="x" data-match-index="1">t</span>');

    expect(out.trim()).toBe('t');
  });

  it('returns empty string for null, undefined and empty input', () => {
    expect(sanitizeToPlainText(null)).toBe('');
    expect(sanitizeToPlainText(undefined)).toBe('');
    expect(sanitizeToPlainText('')).toBe('');
  });
});
