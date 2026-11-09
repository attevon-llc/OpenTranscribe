/**
 * Module doubles shared by the gallery toolbar tests.
 *
 * `vi.mock` factories are hoisted above every import, so a test pulls these in
 * through a dynamic import inside the factory:
 *
 *   vi.mock('$stores/locale', async () =>
 *     (await import('$testMocks/galleryToolbarMocks')).keyOnlyLocale()
 *   );
 *
 * The module is evaluated once per test file, so the gallery double below is a
 * per-file singleton: the copy the factory hands to the component is the same
 * one the test drives through `selectFiles` and inspects through
 * `galleryTriggers`.
 */
import { readable, writable } from 'svelte/store';
import { vi } from 'vitest';
import english from '$lib/i18n/locales/en.json';

type Options = Record<string, unknown>;

/** `$stores/locale` whose translator answers with the key it was asked for. */
export function keyOnlyLocale() {
  return { t: readable((key: string) => key) };
}

/**
 * `$stores/locale` backed by the real English catalogue, resolved the way
 * i18next resolves it: a numeric `count` picks the `_one` / `_other` variant
 * when the catalogue has one, and every `{{name}}` placeholder takes the option
 * of that name. A key the catalogue lacks comes back as the key.
 */
export function englishLocale() {
  const catalogue = english as Record<string, string>;
  const translate = (key: string, options: Options = {}) => {
    const { count } = options;
    const pluralKey =
      typeof count === 'number' ? `${key}_${count === 1 ? 'one' : 'other'}` : undefined;
    const template = (pluralKey && catalogue[pluralKey]) ?? catalogue[key] ?? key;
    return Object.entries(options).reduce(
      (text, [name, value]) => text.replaceAll(`{{${name}}}`, String(value)),
      template
    );
  };
  return { t: readable(translate) };
}

/** Every `galleryStore` member the toolbars call, each a spy. */
export const galleryTriggers = {
  triggerTags: vi.fn(),
  triggerAddTags: vi.fn(),
  triggerRemoveTags: vi.fn(),
  triggerAddToCollection: vi.fn(),
  triggerCollections: vi.fn(),
  triggerExport: vi.fn(),
  triggerUpload: vi.fn(),
  triggerDeleteSelected: vi.fn(),
  triggerReprocess: vi.fn(),
  triggerSummarize: vi.fn(),
  triggerRedact: vi.fn(),
  triggerRetryFailed: vi.fn(),
  triggerSpeakerId: vi.fn(),
  triggerCancelProcessing: vi.fn(),
  notifyHiddenChanged: vi.fn(),
  triggerPlaceHold: vi.fn(),
  triggerQuarantine: vi.fn(),
  setSelecting: vi.fn(),
  selectAllFiles: vi.fn(),
  clearSelection: vi.fn(),
};

const selectedCount = writable(0);
const galleryState = writable({ isSelecting: true, selectedFiles: new Set<string>() });

/**
 * `$stores/gallery` with spies for the triggers and writable selection stores.
 * The real store imports `$app/environment`, which vitest cannot resolve, and
 * the double lets a test set the selection directly.
 */
export function galleryStoreMock() {
  return {
    galleryStore: galleryTriggers,
    galleryState,
    selectedCount,
    allFilesSelected: readable(false),
  };
}

/** Puts exactly these file uuids in the gallery selection, in selecting mode. */
export function selectFiles(uuids: string[]) {
  galleryState.set({ isSelecting: true, selectedFiles: new Set(uuids) });
  selectedCount.set(uuids.length);
}

/** Clears every spy and empties the selection: the per-test reset. */
export function resetGalleryDouble() {
  vi.clearAllMocks();
  selectFiles([]);
}
