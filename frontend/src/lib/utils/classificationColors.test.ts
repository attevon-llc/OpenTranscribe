import { describe, it, expect } from 'vitest';
import { classificationColors, colorsFor, unclassifiedColors } from './classificationColors';

function luminance(hex: string): number {
  const [r, g, b] = [1, 3, 5].map((i) => {
    const c = parseInt(hex.slice(i, i + 2), 16) / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function contrast(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

describe('classification palette', () => {
  it.each(Object.entries(classificationColors))(
    '%s text meets AA 4.5:1 on its colour',
    (_name, c) => {
      expect(contrast(c.bg, c.text)).toBeGreaterThanOrEqual(4.5);
    }
  );

  it('falls back to UNCLASSIFIED for an unknown level', () => {
    expect(colorsFor('NOPE')).toBe(unclassifiedColors);
  });
});
