/**
 * KaTeX is loaded lazily (it is most of the math cost and most answers have no math).
 * Before it arrives, a math span must still show its source rather than vanish, and the
 * renderer must say a re-render is worth doing once it lands. Separate file on purpose:
 * vitest isolates modules per file, so KaTeX is genuinely not loaded here. The two tests
 * run in order: the first must see KaTeX unloaded, the second loads it.
 */
import { describe, expect, it } from 'vitest';

import { loadChatMath, mathPending, renderChatMarkdown } from './chatMarkdown';

describe('math before KaTeX has loaded', () => {
  it('never reports pending for text without math, but does for text with it', () => {
    expect(mathPending('It costs $5 and $10.')).toBe(false);
    expect(mathPending('plain answer')).toBe(false);
    expect(mathPending('a `$x$` code span')).toBe(false);
    expect(mathPending('ratio $a^2$ here')).toBe(true);
  });

  it('shows the TeX source, then renders MathML once loaded', async () => {
    const before = renderChatMarkdown('ratio $a^2$ here');
    expect(before).not.toContain('<math');
    expect(before).toContain('a^2');

    await loadChatMath();

    expect(mathPending('ratio $a^2$ here')).toBe(false);
    expect(renderChatMarkdown('ratio $a^2$ here')).toContain('<msup>');
  });
});
