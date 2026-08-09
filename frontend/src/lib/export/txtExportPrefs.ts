/**
 * localStorage persistence for TXT export options (timestamps / speakers toggles).
 *
 * Pure presentational helper extracted from the file-detail page. Behavior is
 * locked: defaults are both-on, and any stored values are merged over the defaults
 * so a partial blob never drops a toggle.
 */

const TXT_PREF_KEY = 'opentranscribe.txtExportPrefs';

export function loadTxtPrefs(): { includeTimestamps: boolean; includeSpeakers: boolean } {
  try {
    const raw = localStorage.getItem(TXT_PREF_KEY);
    if (raw) return { includeTimestamps: true, includeSpeakers: true, ...JSON.parse(raw) };
  } catch {}
  return { includeTimestamps: true, includeSpeakers: true };
}

export function saveTxtPrefs(prefs: { includeTimestamps: boolean; includeSpeakers: boolean }) {
  try {
    localStorage.setItem(TXT_PREF_KEY, JSON.stringify(prefs));
  } catch {}
}
