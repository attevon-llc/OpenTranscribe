/**
 * Tag-name normalization and near-match scoring, mirrored from the backend.
 *
 * The backend owns tag resolution — every applied name goes through
 * `POST /tags` or `POST /files/{uuid}/tags` and the *server* decides which row
 * it becomes. These helpers exist only for the two questions a surface has to
 * answer **before** it is allowed to call the server:
 *
 * 1. "Would this name resolve to a tag that is already here?" — asking the
 *    server would create the tag the caller may be about to decline, and on the
 *    bulk *remove* path would create the very tag the user wants gone.
 * 2. "Is this only a *near* match?" — a near match may never be applied on a
 *    human-supplied path (R18). At the backend's 0.85 threshold `q3-earnings`
 *    and `q4-earnings` score 0.909, and nothing in the product can split two
 *    tags back apart, so the user has to accept or decline it first.
 *
 * Both mirror `backend/app/services/tag_service.py` exactly:
 * `normalize_tag_name`, `clean_tag_name`, `names_are_similar`, and
 * `suggest_similar_tag`. If that module's rules change, change these with it.
 */

/**
 * Storage width of `tag.name` (`VARCHAR(50)`), mirroring
 * `tag_service.MAX_TAG_NAME_LENGTH`. The backend clamps rather than rejects.
 */
export const MAX_TAG_NAME_LENGTH = 50;

/** Mirrors `app.core.constants.FUZZY_MATCH_THRESHOLD`. */
export const TAG_FUZZY_MATCH_THRESHOLD = 0.85;

/**
 * Normalize a tag name the way the backend does
 * (`tag_service.normalize_tag_name`): lowercase, hyphens and underscores become
 * spaces, whitespace runs collapse, ends trimmed. A name made only of
 * separators normalizes to the empty string — which is exactly the name the
 * backend refuses.
 */
export function normalizeTagName(name: string): string {
  if (!name) return '';
  return name.toLowerCase().replace(/[-_]+/g, ' ').replace(/\s+/g, ' ').trim();
}

/**
 * Trim a supplied name and clamp it to the stored column width, matching
 * `tag_service.clean_tag_name` — so the name a surface shows is the name that
 * will be stored, rather than the longer one that was typed.
 */
export function cleanTagName(name: string): string {
  if (!name) return '';
  return name.trim().slice(0, MAX_TAG_NAME_LENGTH).trim();
}

/**
 * Total characters Python's `difflib.SequenceMatcher` would count as matching.
 *
 * The recursive longest-matching-block walk (Ratcliff/Obershelp) that
 * `get_matching_blocks` performs, with no junk heuristic: `SequenceMatcher`'s
 * autojunk only engages on sequences of 200+ elements and tag names are capped
 * at {@link MAX_TAG_NAME_LENGTH}, so it can never apply here.
 */
function matchingCount(
  a: string,
  b: string,
  b2j: Map<string, number[]>,
  alo: number,
  ahi: number,
  blo: number,
  bhi: number
): number {
  let besti = alo;
  let bestj = blo;
  let bestsize = 0;
  let j2len = new Map<number, number>();

  for (let i = alo; i < ahi; i++) {
    const newj2len = new Map<number, number>();
    for (const j of b2j.get(a[i]) ?? []) {
      if (j < blo) continue;
      if (j >= bhi) break;
      const k = (j2len.get(j - 1) ?? 0) + 1;
      newj2len.set(j, k);
      if (k > bestsize) {
        besti = i - k + 1;
        bestj = j - k + 1;
        bestsize = k;
      }
    }
    j2len = newj2len;
  }

  if (bestsize === 0) return 0;
  return (
    bestsize +
    matchingCount(a, b, b2j, alo, besti, blo, bestj) +
    matchingCount(a, b, b2j, besti + bestsize, ahi, bestj + bestsize, bhi)
  );
}

/**
 * Similarity of two strings on the same scale as Python's
 * `difflib.SequenceMatcher(None, a, b).ratio()` — `2 * matches / total length`,
 * so two empty strings score 1.
 */
export function similarityRatio(a: string, b: string): number {
  const total = a.length + b.length;
  if (total === 0) return 1;

  const b2j = new Map<string, number[]>();
  for (let j = 0; j < b.length; j++) {
    const indices = b2j.get(b[j]);
    if (indices) indices.push(j);
    else b2j.set(b[j], [j]);
  }

  return (2 * matchingCount(a, b, b2j, 0, a.length, 0, b.length)) / total;
}

/**
 * Whether two names name the same topic, mirroring
 * `tag_service.names_are_similar`: normalized-equal, or scoring at or above
 * the threshold. A name that normalizes to nothing matches nothing.
 */
export function namesAreSimilar(
  a: string,
  b: string,
  threshold: number = TAG_FUZZY_MATCH_THRESHOLD
): boolean {
  const normA = normalizeTagName(a);
  const normB = normalizeTagName(b);
  if (!normA || !normB) return false;
  if (normA === normB) return true;
  return similarityRatio(normA, normB) >= threshold;
}

/**
 * The first candidate that is a near match for `name`, or null.
 *
 * Mirrors `tag_service.suggest_similar_tag`, including its "first hit wins"
 * behavior: fuzzy similarity is non-transitive, so there is no best answer to
 * pick — the result is only as stable as the candidate order, which is the
 * server's (`GET /tags` returns most-used first). Callers on a human-supplied
 * path must offer the result, never apply it (R18).
 */
export function findSimilarTagName(
  name: string,
  candidates: string[],
  threshold: number = TAG_FUZZY_MATCH_THRESHOLD
): string | null {
  if (!normalizeTagName(name)) return null;
  return candidates.find((candidate) => namesAreSimilar(name, candidate, threshold)) ?? null;
}
