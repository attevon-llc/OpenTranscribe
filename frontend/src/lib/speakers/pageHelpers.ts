/** Pure helpers for the /speakers page and its section components. */

export const CLUSTER_PAGE_SIZE = 20;

const COLLAPSE_STORAGE_KEY = 'speakers-cluster-sections-collapse';
const AVATAR_ALLOWED_TYPES = ['image/jpeg', 'image/png', 'image/gif', 'image/webp'];
const AVATAR_MAX_BYTES = 2 * 1024 * 1024;

/**
 * Clamp the current page for a list that just lost `removed` entries.
 *
 * Do this BEFORE reloading: clamping afterwards meant deleting the last item on the last
 * page fetched that page, discovered it was now out of range, and fetched again.
 */
export function clampPageAfterRemoval(
  page: number,
  total: number,
  removed: number,
  pageSize: number = CLUSTER_PAGE_SIZE
): number {
  const remaining = Math.max(0, total - removed);
  const pages = Math.max(1, Math.ceil(remaining / pageSize));
  return page > pages ? pages : page;
}

/** A copy of `set` with `uuid` added, or removed if it was present. */
export function toggleMember(set: Set<string>, uuid: string): Set<string> {
  const next = new Set(set);
  if (next.has(uuid)) next.delete(uuid);
  else next.add(uuid);
  return next;
}

export interface CollapseState {
  identified: boolean;
  unidentified: boolean;
}

/** Saved collapse state of the two cluster sections; both open when nothing usable is saved. */
export function loadCollapseState(): CollapseState {
  try {
    const saved = localStorage.getItem(COLLAPSE_STORAGE_KEY);
    if (saved) {
      const parsed = JSON.parse(saved);
      return { identified: parsed.identified ?? false, unidentified: parsed.unidentified ?? false };
    }
  } catch {
    /* ignore */
  }
  return { identified: false, unidentified: false };
}

export function saveCollapseState(state: CollapseState): void {
  try {
    localStorage.setItem(COLLAPSE_STORAGE_KEY, JSON.stringify(state));
  } catch {
    /* ignore */
  }
}

/** The i18n key of why `file` is not an acceptable avatar, or null if it is. */
export function avatarFileError(file: { type: string; size: number }): string | null {
  if (!AVATAR_ALLOWED_TYPES.includes(file.type)) return 'speakers.avatar.invalidType';
  if (file.size > AVATAR_MAX_BYTES) return 'speakers.avatar.tooLarge';
  return null;
}

export type InboxShortcut = 'accept' | 'skip' | 'preview';

/** The inbox action a key press means for the first inbox item, or null for no action. */
export function resolveInboxShortcut(
  key: string,
  first: { suggested_name?: string | null } | undefined
): InboxShortcut | null {
  if ((key === 'a' || key === 'A') && first?.suggested_name) return 'accept';
  if (key === 's' || key === 'S' || key === 'n' || key === 'N') return first ? 'skip' : null;
  if (key === 'p' || key === 'P') return first ? 'preview' : null;
  return null;
}
