/**
 * The gallery's UI-operation triggers: a counter the toolbar bumps (`trigger*`) and the gallery
 * page observes (`on*Trigger`). Observers skip the value the counter held at subscription, so
 * mounting never replays an old click.
 */
import { writable } from 'svelte/store';

/** A fire-and-observe counter. */
function createCounterTrigger() {
  const counter = writable<number>(0);
  return {
    fire: () => counter.update((n) => n + 1),
    /** Calls `callback` with the new count on every fire after subscription. */
    on(callback: (value: number) => void): () => void {
      let hasInitialized = false;
      return counter.subscribe((value) => {
        if (hasInitialized && value > 0) {
          callback(value);
        }
        hasInitialized = true;
      });
    },
  };
}

/** `getHeld` reads the store's current held-filter value when the held trigger fires. */
export function createGalleryTriggers(getHeld: () => boolean) {
  const upload = createCounterTrigger();
  const collections = createCounterTrigger();
  const tags = createCounterTrigger();
  const addToCollection = createCounterTrigger();
  const addTags = createCounterTrigger();
  const removeTags = createCounterTrigger();
  const deleteSelected = createCounterTrigger();
  const reprocess = createCounterTrigger();
  const summarize = createCounterTrigger();
  const redact = createCounterTrigger();
  const retryFailed = createCounterTrigger();
  const speakerId = createCounterTrigger();
  const cancelProcessing = createCounterTrigger();
  const quarantine = createCounterTrigger();
  const hiddenChanged = createCounterTrigger();
  const placeHold = createCounterTrigger();
  const heldChanged = createCounterTrigger();
  const exportFormat = writable<string>(''); // format string

  return {
    /** Bump the hidden-files counter: some files were hidden/unhidden, or the mode changed. */
    fireHiddenChanged: hiddenChanged.fire,
    fireHeldChanged: heldChanged.fire,

    triggerUpload: upload.fire,
    triggerTags: tags.fire,
    triggerCollections: collections.fire,
    triggerAddToCollection: addToCollection.fire,
    // Direct add/remove entry points into the bulk tag dialog; no UI calls them today.
    triggerAddTags: addTags.fire,
    triggerRemoveTags: removeTags.fire,
    triggerDeleteSelected: deleteSelected.fire,
    triggerReprocess: reprocess.fire,
    triggerSummarize: summarize.fire,
    triggerRedact: redact.fire,
    triggerRetryFailed: retryFailed.fire,
    triggerExport: (format: string) => exportFormat.set(format),
    triggerSpeakerId: speakerId.fire,
    triggerCancelProcessing: cancelProcessing.fire,
    triggerQuarantine: quarantine.fire,
    triggerPlaceHold: placeHold.fire,
    notifyHiddenChanged: hiddenChanged.fire,

    onUploadTrigger: upload.on,
    onTagsTrigger: tags.on,
    onCollectionsTrigger: collections.on,
    onAddToCollectionTrigger: addToCollection.on,
    onAddTagsTrigger: addTags.on,
    onRemoveTagsTrigger: removeTags.on,
    onDeleteSelectedTrigger: deleteSelected.on,
    onReprocessTrigger: reprocess.on,
    onSummarizeTrigger: summarize.on,
    onRedactTrigger: redact.on,
    onRetryFailedTrigger: retryFailed.on,
    onExportTrigger: (callback: (format: string) => void) => {
      let hasInitialized = false;
      return exportFormat.subscribe((value) => {
        if (hasInitialized && value !== '') {
          callback(value);
          // Reset so the same format can be triggered again
          exportFormat.set('');
        }
        hasInitialized = true;
      });
    },
    onSpeakerIdTrigger: speakerId.on,
    onCancelProcessingTrigger: cancelProcessing.on,
    onQuarantineTrigger: quarantine.on,
    onPlaceHoldTrigger: placeHold.on,
    /** The held filter changed; the callback gets the new value. */
    onHeldChanged: (callback: (held: boolean) => void) => heldChanged.on(() => callback(getHeld())),
    onHiddenChanged: hiddenChanged.on,
  };
}
