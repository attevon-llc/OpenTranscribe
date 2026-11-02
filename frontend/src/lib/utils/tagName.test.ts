import { describe, it, expect } from 'vitest';
import {
  clampTagNameLength,
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

/**
 * Parity with the backend's `normalize_tag_name` / `clean_tag_name`. Every
 * expected value was computed by the backend functions themselves (Python
 * `str.lower`, `re` `\s`, `str.strip`, and code-point slicing), so a row that
 * fails here is a name the SPA and the server would resolve differently.
 */
describe('backend name-rule parity', () => {
  const x = (n: number) => 'x'.repeat(n);
  const grin = '\u{1F600}';

  it.each([
    ['Interview', 'interview', 'Interview'],
    ['Q3_Earnings', 'q3 earnings', 'Q3_Earnings'],
    ['  Foo__Bar--baz  ', 'foo bar baz', 'Foo__Bar--baz'],
    ['  team   alpha ', 'team alpha', 'team   alpha'],
    ['-foo-', 'foo', '-foo-'],
    ['a - b', 'a b', 'a - b'],
    ['A_b-C d', 'a b c d', 'A_b-C d'],
    ['-', '', '-'],
    ['__', '', '__'],
    ['  --__  ', '', '--__'],
    [' - _ ', '', '- _'],
    ['', '', ''],
    ['a\tb\nc', 'a b c', 'a\tb\nc'],
    ['a\u00a0b', 'a b', 'a\u00a0b'],
    ['ÉCOLE', 'école', 'ÉCOLE'],
    ['ΟΔΟΣ', 'οδος', 'ΟΔΟΣ'],
    ['İstanbul', 'i\u0307stanbul', 'İstanbul'],
    ['#tag/one!', '#tag/one!', '#tag/one!'],
    [x(60), x(60), x(50)],
    [`${x(49)} tail`, `${x(49)} tail`, x(49)],
    // Python's whitespace class includes the information separators and NEL...
    ['a\u001fb', 'a b', 'a\u001fb'],
    ['a\u001cb', 'a b', 'a\u001cb'],
    ['a\u0085b', 'a b', 'a\u0085b'],
    // ...and excludes the byte-order mark, which JavaScript's `\s` and trim() include.
    ['\ufeffabc', '\ufeffabc', '\ufeffabc'],
    // Lengths are code points: an emoji is one character, never two.
    [grin.repeat(30), grin.repeat(30), grin.repeat(30)],
    [`${x(49)}${grin}y`, `${x(49)}${grin}y`, `${x(49)}${grin}`],
  ])('%j normalizes to %j and cleans to %j', (input, normalized, cleaned) => {
    expect(normalizeTagName(input)).toBe(normalized);
    expect(cleanTagName(input)).toBe(cleaned);
  });

  it('treats exactly the code points Python calls whitespace as whitespace', () => {
    // `[c for c in range(0x110000) if chr(c).isspace()]`, from Python 3.12.
    const python = [
      0x09, 0x0a, 0x0b, 0x0c, 0x0d, 0x1c, 0x1d, 0x1e, 0x1f, 0x20, 0x85, 0xa0, 0x1680, 0x2000,
      0x2001, 0x2002, 0x2003, 0x2004, 0x2005, 0x2006, 0x2007, 0x2008, 0x2009, 0x200a, 0x2028,
      0x2029, 0x202f, 0x205f, 0x3000,
    ];
    const seen: number[] = [];
    for (let code = 0; code <= 0xffff; code++) {
      if (code >= 0xd800 && code <= 0xdfff) continue;
      if (normalizeTagName(`a${String.fromCodePoint(code)}b`) === 'a b') seen.push(code);
    }
    // Hyphen-minus and underscore become spaces too, by the separator rule.
    expect(seen).toEqual([...python, 0x2d, 0x5f].sort((p, q) => p - q));
  });

  it.each([
    [`${grin}a`, `${grin}b`, 0.5],
    [`ab${grin}`, 'ab\u{1F601}', 0.666667],
    [`${grin}${grin} earnings`, `${grin}${grin} earning`, 0.952381],
  ])('scores %s against %s over code points like difflib', (a, b, expected) => {
    expect(similarityRatio(a, b)).toBeCloseTo(expected, 6);
  });
});

describe('clampTagNameLength', () => {
  it('keeps at most the stored width in code points without trimming', () => {
    expect(clampTagNameLength('x'.repeat(60))).toBe('x'.repeat(50));
    expect(clampTagNameLength('  spaced  ')).toBe('  spaced  ');
    expect(clampTagNameLength('\u{1F600}'.repeat(30))).toBe('\u{1F600}'.repeat(30));
    expect(clampTagNameLength(`${'x'.repeat(49)}\u{1F600}\u{1F600}`)).toBe(
      `${'x'.repeat(49)}\u{1F600}`
    );
  });
});
