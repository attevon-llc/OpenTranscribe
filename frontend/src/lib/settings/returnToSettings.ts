/**
 * Getting back to Settings from a full page that a Settings panel linked to (issue #1244).
 *
 * The modal closes itself on every route change, so "Open full screen" used to be a one-way
 * door. Two independent ways back, both without any component importing the other:
 *
 * - Browser Back: the link stamps its own history entry with the section before navigating away;
 *   the modal reads that stamp when a popstate lands on the entry (`afterNavigate`) and reopens.
 * - A visible control on the full page: the link carries `?from=settings&section=<id>`, so the
 *   page can offer "Back to Settings" even after a reload or in a new tab; `sessionStorage`
 *   remembers which page the modal was over.
 *
 * Imported by the modal and two panels only, never by the root layout (its static size is budgeted).
 */
import { settingsModalStore, type SettingsSection } from '$stores/settingsModalStore';

const STATE_KEY = 'opentr:settingsReturn';
const STORAGE_KEY = 'opentr:settingsReturnPath';

/** Query string a link to a full page appends so that page can offer the way back. */
export function settingsReturnQuery(section: SettingsSection): string {
  return `from=settings&section=${encodeURIComponent(section)}`;
}

/** Call from the link's click handler, before the navigation happens. */
export function stampSettingsReturn(section: SettingsSection): void {
  if (typeof window === 'undefined') return;
  try {
    window.history.replaceState({ ...window.history.state, [STATE_KEY]: section }, '');
    window.sessionStorage.setItem(STORAGE_KEY, window.location.pathname + window.location.search);
  } catch {
    // Storage or history blocked: the visible control still works from the query string.
  }
}

/**
 * The section a history entry was stamped with, if any. Clears the stamp: Back reopens Settings
 * once, not again every time the entry is revisited after the user closed it.
 */
export function takeHistorySettingsReturn(): SettingsSection | null {
  if (typeof window === 'undefined') return null;
  const section = window.history.state?.[STATE_KEY];
  if (typeof section !== 'string') return null;
  const { [STATE_KEY]: _taken, ...rest } = window.history.state;
  window.history.replaceState(rest, '');
  return section as SettingsSection;
}

/** Reopen Settings if the history entry just landed on was stamped by {@link stampSettingsReturn}. */
export function reopenSettingsAfterBack(): void {
  const section = takeHistorySettingsReturn();
  if (section) settingsModalStore.open(section);
}

export interface SettingsReturnTarget {
  section: SettingsSection;
  /** The page the modal was over. */
  path: string;
}

/** The target named by the current URL's query, or null when the user did not come from Settings. */
export function settingsReturnFromUrl(url: URL): SettingsReturnTarget | null {
  if (url.searchParams.get('from') !== 'settings') return null;
  const section = url.searchParams.get('section') ?? '';
  if (!/^[a-z][a-z-]*$/.test(section)) return null;
  let path = '/';
  try {
    const remembered = window.sessionStorage.getItem(STORAGE_KEY);
    // Same-origin paths only: this string came from storage, not from the user, but it is
    // handed to goto().
    if (remembered && remembered.startsWith('/') && !remembered.startsWith('//')) path = remembered;
  } catch {
    // Storage blocked: fall back to the gallery.
  }
  return { section: section as SettingsSection, path };
}
