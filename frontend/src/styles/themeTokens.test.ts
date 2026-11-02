import { describe, it, expect } from 'vitest';
import {
  compositeOver,
  contrastRatio,
  parseColour,
  parseTokenSets,
  resolveToken,
} from './themeTokens';

// The resolver carries the contrast and parity gates, so it gets its own must-fire and
// must-stay-clean cases: a parser that silently returned empty maps would make every gate
// built on it vacuously green.
const CSS = `
/* a comment with :root { --ghost: red } inside */
:root {
  --a: #112233;
  --b: var(--a);
  --c: var(--missing, rgba(1, 2, 3, 0.5));
  --d: rgba(var(--rgb), 0.25);
  --rgb: 10, 20, 30;
  --icon: url("data:image/svg+xml,%3Csvg a='1'; b='2'%3E");
  --cycle1: var(--cycle2);
  --cycle2: var(--cycle1);
}
[data-theme='dark'] {
  --a: #445566;
}
body { color: red; }
`;

describe('themeTokens resolver', () => {
  const sets = parseTokenSets(CSS);

  it('reads both blocks and ignores comments and other rules', () => {
    expect(sets.root.get('--a')).toBe('#112233');
    expect(sets.dark.get('--a')).toBe('#445566');
    expect(sets.root.has('--ghost')).toBe(false);
  });

  it('keeps a semicolon that sits inside quotes or parentheses', () => {
    expect(sets.root.get('--icon')).toContain("a='1'; b='2'");
  });

  it('follows var() chains per theme (dark overrides, the rest inherits :root)', () => {
    expect(resolveToken(sets, 'light', '--b')).toBe('#112233');
    expect(resolveToken(sets, 'dark', '--b')).toBe('#445566');
  });

  it('uses a var() fallback only when the token is undeclared', () => {
    expect(resolveToken(sets, 'light', '--c')).toBe('rgba(1, 2, 3, 0.5)');
    expect(resolveToken(sets, 'light', '--d')).toBe('rgba(10, 20, 30, 0.25)');
  });

  it('throws on a cycle and on an undeclared token', () => {
    expect(() => resolveToken(sets, 'light', '--cycle1')).toThrow(/cycle/);
    expect(() => resolveToken(sets, 'light', '--nope')).toThrow(/not declared/);
  });

  it('parses hex and rgba, composites alpha, and measures contrast', () => {
    expect(parseColour('#fff')).toEqual({ rgb: [255, 255, 255], alpha: 1 });
    expect(parseColour('rgba(0, 0, 0, 0.5)').alpha).toBe(0.5);
    expect(compositeOver({ rgb: [0, 0, 0], alpha: 0.5 }, [255, 255, 255])).toEqual([128, 128, 128]);
    expect(contrastRatio([0, 0, 0], [255, 255, 255])).toBeCloseTo(21, 5);
    expect(() => parseColour('hotpink')).toThrow();
  });
});
