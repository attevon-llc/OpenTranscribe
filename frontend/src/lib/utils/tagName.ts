/**
 * Client-side mirror of the backend's tag name rules
 * (`backend/app/services/tag_service.py`: `normalize_tag_name`,
 * `clean_tag_name` and the fuzzy matcher).
 *
 * The server alone decides which tag row a name becomes. These helpers only let
 * the UI ask, before a request, whether a typed name would land on a tag that is
 * already present, or merely resembles one. Every function must agree with the
 * backend for every input, so the two rules below follow Python, not JavaScript:
 *
 * - whitespace is Python's `str.isspace` class (JavaScript's `\s` and `trim()`
 *   also treat U+FEFF as whitespace and miss U+001C..U+001F and U+0085);
 * - lengths are counted in code points, as Python counts them, so an emoji is
 *   one character and is never split in half.
 */

/** Stored width of `tag.name` (`VARCHAR(50)`), in code points. */
export const MAX_TAG_NAME_LENGTH = 50;

/** Backend `FUZZY_MATCH_THRESHOLD`. */
const SIMILARITY_THRESHOLD = 0.85;

// Python's whitespace set: `[c for c in range(0x110000) if chr(c).isspace()]`.
const PY_SPACE =
  '\\u0009-\\u000d\\u001c-\\u0020\\u0085\\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000';
const SPACE_RUN = new RegExp(`[${PY_SPACE}]+`, 'gu');
const EDGE_SPACE = new RegExp(`^[${PY_SPACE}]+|[${PY_SPACE}]+$`, 'gu');
const SEPARATOR_RUN = /[-_]+/g;

/** Python's `str.strip()`: removes only the Python whitespace class at both ends. */
function stripPython(value: string): string {
  return value.replace(EDGE_SPACE, '');
}

/** The first `limit` code points of `value`. */
function takeCodePoints(value: string, limit: number): string {
  const points = Array.from(value);
  return points.length <= limit ? value : points.slice(0, limit).join('');
}

/**
 * The comparison key two names share when the server treats them as one tag:
 * lower-cased, every run of hyphens/underscores and of whitespace collapsed to
 * one space, then stripped. Nothing else is removed.
 */
export function normalizeTagName(name: string): string {
  if (!name) return '';
  return stripPython(name.toLowerCase().replace(SEPARATOR_RUN, ' ').replace(SPACE_RUN, ' '));
}

/** The display name the server would store: stripped, cut to the column width, stripped again. */
export function cleanTagName(name: string): string {
  if (!name) return '';
  return stripPython(takeCodePoints(stripPython(name), MAX_TAG_NAME_LENGTH));
}

/** Cuts live input to the column width without trimming, so a typed space survives. */
export function clampTagNameLength(value: string): string {
  return takeCodePoints(value, MAX_TAG_NAME_LENGTH);
}

/**
 * `on:input` handler for a tag-name field bound one way (`value={...}`).
 *
 * The clamped value has to be written back into the element: when the clamp
 * leaves the caller's state unchanged, Svelte has nothing to re-render, and the
 * over-long text would otherwise stay visible in the field.
 */
export function clampTagNameInput(event: Event): string {
  const field = event.currentTarget as HTMLInputElement;
  const clamped = clampTagNameLength(field.value);
  if (clamped !== field.value) field.value = clamped;
  return clamped;
}

/**
 * Longest common contiguous block of `a[aLo:aHi]` and `b[bLo:bHi]`, chosen the
 * way difflib's `find_longest_match` chooses it when nothing is junk: the first
 * maximum met while scanning `a` forward, and `b` forward within each position.
 */
function longestBlock(
  a: string[],
  b: string[],
  aLo: number,
  aHi: number,
  bLo: number,
  bHi: number
): [number, number, number] {
  let bestA = aLo;
  let bestB = bLo;
  let bestSize = 0;
  // Keyed by j: length of the common run ending at a[i - 1] and b[j].
  let previous = new Map<number, number>();
  for (let i = aLo; i < aHi; i++) {
    const current = new Map<number, number>();
    for (let j = bLo; j < bHi; j++) {
      if (a[i] !== b[j]) continue;
      const size = (previous.get(j - 1) ?? 0) + 1;
      current.set(j, size);
      if (size > bestSize) {
        bestA = i - size + 1;
        bestB = j - size + 1;
        bestSize = size;
      }
    }
    previous = current;
  }
  return [bestA, bestB, bestSize];
}

/**
 * Ratcliff/Obershelp similarity, equal to Python's
 * `difflib.SequenceMatcher(None, a, b).ratio()` over code points.
 */
export function similarityRatio(a: string, b: string): number {
  const left = Array.from(a);
  const right = Array.from(b);
  const total = left.length + right.length;
  if (total === 0) return 1;

  let matched = 0;
  const pending: [number, number, number, number][] = [[0, left.length, 0, right.length]];
  while (pending.length > 0) {
    const [aLo, aHi, bLo, bHi] = pending.pop() as [number, number, number, number];
    const [i, j, size] = longestBlock(left, right, aLo, aHi, bLo, bHi);
    if (size === 0) continue;
    matched += size;
    if (aLo < i && bLo < j) pending.push([aLo, i, bLo, j]);
    if (i + size < aHi && j + size < bHi) pending.push([i + size, aHi, j + size, bHi]);
  }
  return (2 * matched) / total;
}

/** Whether two names are the same tag or a near match of one another. */
export function namesAreSimilar(
  a: string,
  b: string,
  threshold: number = SIMILARITY_THRESHOLD
): boolean {
  const left = normalizeTagName(a);
  const right = normalizeTagName(b);
  if (!left || !right) return false;
  if (left === right) return true;
  return similarityRatio(left, right) >= threshold;
}

/**
 * The first candidate (in the given order, the server's most-used-first order)
 * that `name` resembles, or `null`. First hit wins, not the closest.
 */
export function findSimilarTagName(
  name: string,
  candidates: string[],
  threshold: number = SIMILARITY_THRESHOLD
): string | null {
  if (!normalizeTagName(name)) return null;
  return candidates.find((candidate) => namesAreSimilar(name, candidate, threshold)) ?? null;
}
