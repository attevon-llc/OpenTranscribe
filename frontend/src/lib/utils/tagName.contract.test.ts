import { describe, it, expect } from 'vitest';
import {
  MAX_TAG_NAME_LENGTH,
  cleanTagName,
  findSimilarTagName,
  namesAreSimilar,
  normalizeTagName,
  similarityRatio,
} from './tagName';

/**
 * Behaviour table for the name helpers. Rows involving U+001C..U+001F, U+0085,
 * U+FEFF and astral characters are deliberately absent: they are where the
 * client and the backend currently disagree.
 */
describe('MAX_TAG_NAME_LENGTH', () => {
  it('is the stored column width', () => {
    expect(MAX_TAG_NAME_LENGTH).toBe(50);
  });
});

describe('normalizeTagName rows', () => {
  it.each([
    ['Interview', 'interview'],
    ['Q3_Earnings', 'q3 earnings'],
    ['  Foo__Bar--baz  ', 'foo bar baz'],
    ['  team   alpha ', 'team alpha'],
    ['-foo-', 'foo'],
    ['a - b', 'a b'],
    ['A_b-C d', 'a b c d'],
    ['-', ''],
    ['__', ''],
    ['  --__  ', ''],
    [' - _ ', ''],
    ['', ''],
    ['a\tb\nc', 'a b c'],
    ['a b', 'a b'],
    ['ÉCOLE', 'école'],
    ['ΟΔΟΣ', 'οδος'],
    ['İstanbul', 'i̇stanbul'],
    ['#tag/one!', '#tag/one!'],
    ['Café #1 / Q&A', 'café #1 / q&a'],
    ['😀 Party', '😀 party'],
  ])('normalizes %j to %j', (input, expected) => {
    expect(normalizeTagName(input)).toBe(expected);
  });

  it('gives an empty string for a falsy value', () => {
    expect(normalizeTagName(undefined as unknown as string)).toBe('');
    expect(normalizeTagName(null as unknown as string)).toBe('');
  });

  it('does not limit the length', () => {
    expect(normalizeTagName('X'.repeat(60))).toBe('x'.repeat(60));
  });
});

describe('cleanTagName rows', () => {
  it.each([
    ['Interview', 'Interview'],
    ['Q3_Earnings', 'Q3_Earnings'],
    ['  Foo__Bar--baz  ', 'Foo__Bar--baz'],
    ['  team   alpha ', 'team   alpha'],
    ['-foo-', '-foo-'],
    ['a - b', 'a - b'],
    ['-', '-'],
    ['__', '__'],
    ['  --__  ', '--__'],
    [' - _ ', '- _'],
    ['', ''],
    ['a\tb\nc', 'a\tb\nc'],
    ['ÉCOLE', 'ÉCOLE'],
  ])('cleans %j to %j', (input, expected) => {
    expect(cleanTagName(input)).toBe(expected);
  });

  it('gives an empty string for a falsy value', () => {
    expect(cleanTagName(undefined as unknown as string)).toBe('');
  });

  it('keeps a name of exactly the maximum width and cuts one character past it', () => {
    expect(cleanTagName('x'.repeat(50))).toBe('x'.repeat(50));
    expect(cleanTagName('x'.repeat(51))).toBe('x'.repeat(50));
  });

  it('trims again after the cut so the stored name never ends in a space', () => {
    expect(cleanTagName(`${'x'.repeat(49)} tail`)).toBe('x'.repeat(49));
    expect(cleanTagName(`${'x'.repeat(48)}  tail`)).toBe('x'.repeat(48));
  });

  it('trims before the cut so leading padding does not eat the budget', () => {
    expect(cleanTagName(`   ${'y'.repeat(50)}`)).toBe('y'.repeat(50));
  });
});

describe('similarityRatio blocks', () => {
  it.each([
    ['abxcd', 'cdabx', 0.6],
    ['abcabc', 'abc', 0.666667],
    ['xaby', 'abxy', 0.75],
    ['aabb', 'abab', 0.75],
    ['night', 'nacht', 0.6],
    ['abcdxyz', 'xyzabcd', 0.571429],
    ['kitten', 'sitting', 0.615385],
    ['ab', 'ac', 0.5],
    ['a', 'b', 0],
  ])('scores %s against %s as %s (value taken from Python difflib)', (a, b, expected) => {
    expect(similarityRatio(a, b)).toBeCloseTo(expected, 6);
  });

  it('scores identical strings 1 and a string against an empty one 0', () => {
    expect(similarityRatio('abc', 'abc')).toBe(1);
    expect(similarityRatio('', 'x')).toBe(0);
    expect(similarityRatio('x', '')).toBe(0);
  });
});

describe('namesAreSimilar thresholds', () => {
  it('compares the normalized forms, so punctuation variants of one name are equal', () => {
    expect(namesAreSimilar('Q3_Earnings', 'q3-earnings')).toBe(true);
  });

  it('uses 0.85 by default: 0.8 is not similar, 0.909 is', () => {
    expect(namesAreSimilar('abcde', 'abcdf')).toBe(false);
    expect(namesAreSimilar('q3 earnings', 'q4 earnings')).toBe(true);
  });

  it('accepts a score exactly at the threshold and rejects one just above it', () => {
    expect(namesAreSimilar('ab', 'ac', 0.5)).toBe(true);
    expect(namesAreSimilar('ab', 'ac', 0.51)).toBe(false);
  });

  it('honours a custom threshold in both directions', () => {
    expect(namesAreSimilar('q3 earnings', 'q4 earnings', 0.95)).toBe(false);
    expect(namesAreSimilar('abcde', 'abcdf', 0.8)).toBe(true);
  });

  it('is false when either side normalizes to nothing, even if both are identical', () => {
    expect(namesAreSimilar('___', '___')).toBe(false);
    expect(namesAreSimilar('interview', '--')).toBe(false);
    expect(namesAreSimilar('', '')).toBe(false);
  });
});

describe('findSimilarTagName ordering', () => {
  it('returns the first hit in candidate order, not the best one', () => {
    expect(findSimilarTagName('q3 earnings', ['q4 earnings', 'q3 earnings'])).toBe('q4 earnings');
    expect(findSimilarTagName('q3 earnings', ['q3 earnings', 'q4 earnings'])).toBe('q3 earnings');
  });

  it('returns the candidate as supplied, not its normalized form', () => {
    expect(findSimilarTagName('interview', ['Interview_Notes', 'Interviews'])).toBe('Interviews');
  });

  it('is null when the name normalizes to nothing, however the candidates look', () => {
    expect(findSimilarTagName('  --  ', ['--', 'anything'])).toBeNull();
    expect(findSimilarTagName('', ['anything'])).toBeNull();
  });

  it('is null for no candidates', () => {
    expect(findSimilarTagName('interview', [])).toBeNull();
  });

  it('passes a custom threshold through to every comparison', () => {
    expect(findSimilarTagName('abcde', ['abcdf'])).toBeNull();
    expect(findSimilarTagName('abcde', ['abcdf'], 0.8)).toBe('abcdf');
  });
});
