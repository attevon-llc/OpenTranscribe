/**
 * LegalProse owns legal-document typography.
 *
 * DEFECT THIS CATCHES: `app.html` resets `* { margin: 0; padding: 0 }`, so a list with no
 * component rule has zero `padding-inline-start` and its outside markers hang past the text
 * column; nested lists collapse onto their parent's numbers. jsdom resolves neither the
 * reset nor list layout, so the gutter is pinned at source level.
 */
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/svelte';
import LegalProse from './LegalProse.svelte';

const style = () => {
  const src = readFileSync(
    resolve(process.cwd(), 'src/components/legal/LegalProse.svelte'),
    'utf8'
  );
  return src.slice(src.indexOf('<style')).replace(/\/\*[\s\S]*?\*\//g, '');
};

/** Declarations of the first rule whose selector list mentions `selector`. */
function ruleBody(selector: string): string {
  const css = style();
  const at = css.indexOf(selector);
  expect(at, `no rule for ${selector}`).toBeGreaterThanOrEqual(0);
  return css.slice(css.indexOf('{', at) + 1, css.indexOf('}', at));
}

describe('LegalProse', () => {
  it('renders the given html inside a left-to-right prose container', () => {
    render(LegalProse, { html: '<ol><li>One</li></ol>' });
    const prose = screen.getByTestId('legal-prose');
    expect(prose.getAttribute('dir')).toBe('ltr');
    expect(prose.querySelector('ol li')?.textContent).toBe('One');
  });

  it('restores a hanging gutter for list markers (the global reset zeroes it)', () => {
    expect(ruleBody(':global(ol)')).toMatch(/padding-inline-start:\s*[1-9]/);
  });

  it('numbers nested ordered lists legal-style: (a) then (i)', () => {
    expect(ruleBody(':global(ol ol)')).toMatch(/list-style-type:\s*legal-alpha/);
    expect(ruleBody(':global(ol ol ol)')).toMatch(/list-style-type:\s*legal-roman/);
    expect(style()).toMatch(/@counter-style\s+legal-alpha\s*\{[^}]*lower-alpha/);
    expect(style()).toMatch(/@counter-style\s+legal-roman\s*\{[^}]*lower-roman/);
  });

  it('colours links with the on-surface token, which passes AA on both themes', () => {
    expect(ruleBody(':global(a)')).toMatch(/color:\s*var\(--primary-on-surface\)/);
  });

  it('caps the reading measure with the prose token', () => {
    expect(ruleBody('.legal-prose {')).toMatch(/max-inline-size:\s*var\(--measure-prose\)/);
  });
});
