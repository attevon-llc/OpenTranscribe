import { describe, it, expect } from 'vitest';
import {
  cleanTagName,
  findSimilarTagName,
  namesAreSimilar,
  normalizeTagName,
  similarityRatio,
} from './tagName';

describe('normalizeTagName', () => {
  it('collapses case, separators, and whitespace the way the backend does', () => {
    expect(normalizeTagName('Interview')).toBe('interview');
    expect(normalizeTagName('Q3_Earnings')).toBe('q3 earnings');
    expect(normalizeTagName('  team   alpha ')).toBe('team alpha');
    expect(normalizeTagName('-foo-')).toBe('foo');
  });

  it('normalizes a name made only of separators to nothing', () => {
    expect(normalizeTagName('  --__  ')).toBe('');
    expect(normalizeTagName('')).toBe('');
  });
});

describe('cleanTagName', () => {
  it('trims and clamps to the stored column width', () => {
    expect(cleanTagName('  interview  ')).toBe('interview');
    expect(cleanTagName('x'.repeat(60))).toBe('x'.repeat(50));
    // The clamp can leave trailing whitespace behind, which is trimmed again.
    expect(cleanTagName(`${'x'.repeat(49)} tail`)).toBe('x'.repeat(49));
  });
});

describe('similarityRatio', () => {
  // Reference values from Python's difflib.SequenceMatcher(None, a, b).ratio(),
  // which is what the backend's fuzzy matcher uses.
  it.each([
    ['q3 earnings', 'q4 earnings', 0.909091],
    ['interview', 'interviews', 0.947368],
    ['2024 review', '2025 review', 0.909091],
    ['team alpha', 'team alpha 2', 0.909091],
    ['meeting', 'meetings', 0.933333],
    ['standup', 'stand up', 0.933333],
    ['roadmap', 'budget', 0.153846],
  ])('scores %s against %s like difflib', (a, b, expected) => {
    expect(similarityRatio(a, b)).toBeCloseTo(expected, 6);
  });

  it('scores two empty strings as identical', () => {
    expect(similarityRatio('', '')).toBe(1);
  });
});

describe('namesAreSimilar', () => {
  it('treats a normalized-exact match as similar', () => {
    expect(namesAreSimilar('Interview', 'interview')).toBe(true);
  });

  it('treats a one-character difference above the threshold as similar', () => {
    expect(namesAreSimilar('q3-earnings', 'q4 earnings')).toBe(true);
  });

  it('rejects unrelated names', () => {
    expect(namesAreSimilar('roadmap', 'budget')).toBe(false);
  });

  it('rejects a name that normalizes to nothing', () => {
    expect(namesAreSimilar('___', 'interview')).toBe(false);
  });
});

describe('findSimilarTagName', () => {
  it('returns the first near match in the order the server supplied', () => {
    expect(findSimilarTagName('interviews', ['roadmap', 'interview', 'interviewer'])).toBe(
      'interview'
    );
  });

  it('returns null when nothing is close enough', () => {
    expect(findSimilarTagName('budget', ['roadmap', 'interview'])).toBeNull();
  });
});
