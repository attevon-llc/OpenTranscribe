/**
 * Wires the gallery store's action triggers (toolbar and menu buttons that live in other
 * components) to the page's handlers, and returns one function that unsubscribes them all.
 */
import { galleryStore } from '$stores/gallery';

export interface GalleryTriggerHandlers {
  upload: () => void;
  collections: () => void;
  addToCollection: () => void;
  addTags: () => void;
  /** One Tags button, two modes: a selection means "apply to these", none means "manage the library". */
  tags: () => void;
  removeTags: () => void;
  quarantine: () => void;
  /** A hide, an unhide or a new hidden-files mode (#1102) changes which files this list shows. */
  hiddenChanged: () => void;
  heldChanged: (held: boolean) => void;
  deleteSelected: () => void;
  reprocess: () => void;
  summarize: () => void;
  redact: () => void;
  retryFailed: () => void;
  exportSelected: (format: string) => void;
  speakerId: () => void;
  cancelProcessing: () => void;
}

export function bindGalleryTriggers(h: GalleryTriggerHandlers): () => void {
  const unsubscribers = [
    galleryStore.onUploadTrigger(() => h.upload()),
    galleryStore.onCollectionsTrigger(() => h.collections()),
    galleryStore.onAddToCollectionTrigger(() => h.addToCollection()),
    galleryStore.onAddTagsTrigger(() => h.addTags()),
    galleryStore.onTagsTrigger(() => h.tags()),
    galleryStore.onRemoveTagsTrigger(() => h.removeTags()),
    galleryStore.onQuarantineTrigger(() => h.quarantine()),
    galleryStore.onHiddenChanged(() => h.hiddenChanged()),
    galleryStore.onHeldChanged(h.heldChanged),
    galleryStore.onDeleteSelectedTrigger(() => h.deleteSelected()),
    galleryStore.onReprocessTrigger(() => h.reprocess()),
    galleryStore.onSummarizeTrigger(() => h.summarize()),
    galleryStore.onRedactTrigger(() => h.redact()),
    galleryStore.onRetryFailedTrigger(() => h.retryFailed()),
    galleryStore.onExportTrigger(h.exportSelected),
    galleryStore.onSpeakerIdTrigger(() => h.speakerId()),
    galleryStore.onCancelProcessingTrigger(() => h.cancelProcessing()),
  ];
  return () => unsubscribers.forEach((unsubscribe) => unsubscribe());
}

/**
 * Files trashed or restored by another tab, another person or the Undo (`$lib/websocket/frameRouting`
 * re-dispatches those frames as window events). Returns the stop.
 */
export function listenForTrashEvents(
  onTrashed: (fileId: string) => void,
  onRestored: () => void
): () => void {
  const trashed = (event: Event) => {
    const id = (event as CustomEvent<{ file_id?: string }>).detail?.file_id;
    if (id) onTrashed(String(id));
  };
  const restored = () => onRestored();
  window.addEventListener('file-trashed', trashed);
  window.addEventListener('file-restored', restored);
  return () => {
    window.removeEventListener('file-trashed', trashed);
    window.removeEventListener('file-restored', restored);
  };
}
