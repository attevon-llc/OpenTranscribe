/**
 * The privacy policy as it applies to one file or to the caller (issue #1280).
 *
 * Speaker surfaces ask this to say WHY suggestions or voice-derived clusters are missing, instead
 * of looking mysteriously empty. The server already hides the data (an off feature returns empty
 * lists and null inferred gender), so a failed read changes nothing but the explanation: it
 * resolves to null and the caller renders no notice. Answers are shared for a short while so a
 * page of speaker rows makes one request per file, and a change saved in Settings shows up on
 * the next mount.
 *
 * Not imported by the root layout or `keyMaps.ts` (bundle budget).
 */
import { PrivacyApi, type EffectivePrivacy } from '$lib/api/privacy';

const FRESH_FOR_MS = 15_000;
const ME = 'me';

const cache = new Map<string, { at: number; answer: Promise<EffectivePrivacy | null> }>();

/** `fileUuid` scopes the answer to that file's owner and organization; omitted means the caller. */
export function loadEffectivePrivacy(fileUuid?: string): Promise<EffectivePrivacy | null> {
  const key = fileUuid ?? ME;
  const hit = cache.get(key);
  if (hit && Date.now() - hit.at < FRESH_FOR_MS) return hit.answer;
  const answer = PrivacyApi.getEffective(fileUuid).catch((error) => {
    // Forget a failure at once so the next mount retries.
    cache.delete(key);
    console.error('Failed to read the effective privacy policy:', error);
    return null;
  });
  cache.set(key, { at: Date.now(), answer });
  return answer;
}

/** Forget every cached answer (after a privacy switch changes, and between tests). */
export function resetEffectivePrivacy(): void {
  cache.clear();
}
