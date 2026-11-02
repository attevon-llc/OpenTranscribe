import { describe, it, expect, vi } from 'vitest';
import {
  MAX_TAG_NAME_LENGTH,
  clampTagNameInput,
  clampTagNameLength,
  cleanTagName,
  findSimilarTagName,
  namesAreSimilar,
  normalizeTagName,
  similarityRatio,
} from './tagName';

/**
 * The rules where JavaScript and Python disagree: the whitespace class
 * (`str.isspace`, not `\s`) and length in code points, not UTF-16 units. Every
 * expected value here was produced by Python (`str.isspace`, `difflib`), which is
 * what the backend's `tag_service.py` runs.
 */

const GRIN = '\u{1F600}';
const GRIN_BEAM = '\u{1F601}';

describe('Python whitespace in normalizeTagName', () => {
  it.each([
    ['U+001C', 'a\u001cb'],
    ['U+001F', 'a\u001fb'],
    ['U+0085', 'a\u0085b'],
    ['U+00A0', 'a\u00a0b'],
    ['U+1680', 'a\u1680b'],
    ['U+2028', 'a\u2028b'],
    ['U+202F', 'a\u202fb'],
    ['U+205F', 'a\u205fb'],
    ['U+3000', 'a\u3000b'],
  ])('collapses %s between two letters to one space', (_name, input) => {
    expect(normalizeTagName(input)).toBe('a b');
  });

  it('keeps U+FEFF, which Python does not count as whitespace', () => {
    expect(normalizeTagName('\ufeffabc')).toBe('\ufeffabc');
    expect(normalizeTagName('\ufeff ABC')).toBe('\ufeff abc');
  });

  it.each([
    ['U+200B', 'a\u200bb'],
    ['U+180E', 'a\u180eb'],
  ])('leaves %s inside the name untouched', (_name, input) => {
    expect(normalizeTagName(input)).toBe(input);
  });

  it('strips Python whitespace at both ends', () => {
    expect(normalizeTagName('\u001f\u3000Roadmap\u0085\u2028')).toBe('roadmap');
  });

  it('collapses a mixed run of separators and whitespace to a single space', () => {
    expect(normalizeTagName('Q3 -\u00a0_  Plan')).toBe('q3 plan');
  });

  it('leaves astral characters whole and lowercases around them', () => {
    expect(normalizeTagName(`${GRIN}_Launch`)).toBe(`${GRIN} launch`);
  });
});

describe('Python whitespace in cleanTagName', () => {
  it('strips U+001F and U+0085 at the edges but keeps case and inner spacing', () => {
    expect(cleanTagName('\u001f Team  Alpha \u0085')).toBe('Team  Alpha');
  });

  it('keeps a leading U+FEFF and strips the trailing space', () => {
    expect(cleanTagName('\ufeffabc ')).toBe('\ufeffabc');
  });

  it('gives an empty string for a name made only of Python whitespace', () => {
    expect(cleanTagName('\u001c\u0085\u3000')).toBe('');
  });
});

describe('code-point length', () => {
  it('keeps 30 emoji, which are 60 UTF-16 units but only 30 characters', () => {
    const name = GRIN.repeat(30);
    expect(cleanTagName(name)).toBe(name);
    expect(normalizeTagName(name)).toBe(name);
  });

  it('cuts after the 50th code point without splitting the emoji in that position', () => {
    expect(cleanTagName(`${'x'.repeat(49)}${GRIN}y`)).toBe(`${'x'.repeat(49)}${GRIN}`);
  });

  it('cuts 60 emoji to exactly 50', () => {
    const cleaned = cleanTagName(GRIN.repeat(60));
    expect(cleaned).toBe(GRIN.repeat(MAX_TAG_NAME_LENGTH));
    expect(Array.from(cleaned)).toHaveLength(50);
  });
});

describe('clampTagNameLength', () => {
  it('cuts 60 emoji to 50 emoji', () => {
    expect(clampTagNameLength(GRIN.repeat(60))).toBe(GRIN.repeat(50));
  });

  it('keeps a leading space and counts it towards the 50', () => {
    expect(clampTagNameLength(` ${'x'.repeat(60)}`)).toBe(` ${'x'.repeat(49)}`);
  });

  it('returns a value of 50 code points or fewer unchanged, trailing spaces included', () => {
    expect(clampTagNameLength('x'.repeat(50))).toBe('x'.repeat(50));
    expect(clampTagNameLength('typing ')).toBe('typing ');
    expect(clampTagNameLength('')).toBe('');
  });

  it('never trims, even when the cut lands on whitespace', () => {
    expect(clampTagNameLength(`${'x'.repeat(49)}   tail`)).toBe(`${'x'.repeat(49)} `);
  });
});

describe('clampTagNameInput', () => {
  /** Dispatches an input event on a field whose value writes are recorded. */
  function fireOn(initial: string) {
    const field = document.createElement('input');
    let current = initial;
    const writes = vi.fn();
    Object.defineProperty(field, 'value', {
      get: () => current,
      set: (next: string) => {
        writes(next);
        current = next;
      },
    });
    let returned: string | undefined;
    field.addEventListener('input', (event) => {
      returned = clampTagNameInput(event);
    });
    field.dispatchEvent(new Event('input'));
    return { returned, writes, value: () => current };
  }

  it('writes the clamped value back into an over-long field and returns it', () => {
    const run = fireOn(GRIN.repeat(55));
    expect(run.returned).toBe(GRIN.repeat(50));
    expect(run.value()).toBe(GRIN.repeat(50));
    expect(run.writes).toHaveBeenCalledTimes(1);
    expect(run.writes).toHaveBeenCalledWith(GRIN.repeat(50));
  });

  it('leaves a field within the limit alone and returns its text as typed', () => {
    const run = fireOn('  draft ');
    expect(run.returned).toBe('  draft ');
    expect(run.writes).not.toHaveBeenCalled();
  });
});

describe('similarityRatio against Python difflib', () => {
  it.each([
    ['q3 earnings', 'q4 earnings', 0.909091],
    ['interview', 'interviews', 0.947368],
    ['2024 review', '2025 review', 0.909091],
    ['team alpha', 'team alpha 2', 0.909091],
    ['meeting', 'meetings', 0.933333],
    ['standup', 'stand up', 0.933333],
    ['roadmap', 'budget', 0.153846],
  ])('pins %s / %s at %s', (a, b, expected) => {
    expect(similarityRatio(a, b)).toBeCloseTo(expected, 6);
  });

  it.each([
    // The longest block is taken earliest in `a`; a later pick would score higher.
    ['aba', 'bca', 0.333333],
    ['aaaa', 'abaa', 0.5],
    ['abcxbc', 'bcabc', 0.545455],
    ['abab', 'baba', 0.75],
  ])('breaks ties as find_longest_match does: %s / %s is %s', (a, b, expected) => {
    expect(similarityRatio(a, b)).toBeCloseTo(expected, 6);
  });

  it('scores two empty strings 1', () => {
    expect(similarityRatio('', '')).toBe(1);
  });

  it('does not let two different emoji match on their shared high surrogate', () => {
    expect(similarityRatio(GRIN, GRIN_BEAM)).toBe(0);
    expect(similarityRatio(`a${GRIN}`, `a${GRIN_BEAM}`)).toBeCloseTo(0.5, 6);
    expect(similarityRatio(`${GRIN}${GRIN}x`, `${GRIN}x`)).toBeCloseTo(0.8, 6);
  });
});

describe('namesAreSimilar and findSimilarTagName with the pinned pairs', () => {
  it('treats the 0.9+ pairs as similar and the unrelated pair as not', () => {
    expect(namesAreSimilar('Interview', 'interviews')).toBe(true);
    expect(namesAreSimilar('Stand-Up', 'standup')).toBe(true);
    expect(namesAreSimilar('roadmap', 'budget')).toBe(false);
  });

  it('is false for a name made only of Python whitespace', () => {
    expect(namesAreSimilar('\u0085\u001f', '\u0085\u001f')).toBe(false);
    expect(findSimilarTagName('\u3000\u0085', ['anything'])).toBeNull();
  });

  it('compares a U+FEFF name as a real character', () => {
    expect(findSimilarTagName('\ufeff', ['\ufeff'])).toBe('\ufeff');
  });

  it('returns the first of several similar candidates', () => {
    expect(findSimilarTagName('meeting', ['roadmap', 'Meetings', 'meeting'])).toBe('Meetings');
  });
});
