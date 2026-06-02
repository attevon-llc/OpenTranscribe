import { writable, derived } from 'svelte/store';
import { uploadService, type UploadItem, type UploadEvent } from '../lib/services/uploadService';
import type { ExtractedAudioMetadata } from '$lib/types/audioExtraction';

// Upload store state
interface UploadStoreState {
  uploads: UploadItem[];
  isExpanded: boolean;
  hasNewActivity: boolean;
}

// Create the writable store
function createUploadStore() {
  const initialState: UploadStoreState = {
    uploads: [],
    isExpanded: false,
    hasNewActivity: false,
  };

  const { subscribe, set, update } = writable<UploadStoreState>(initialState);

  // Track event listener for cleanup
  let eventListenerCleanup: (() => void) | null = null;

  // Listen to upload service events
  eventListenerCleanup = uploadService.addEventListener((event: UploadEvent) => {
    update((state) => {
      const uploads = uploadService.getAllUploads();
      let hasNewActivity = state.hasNewActivity;

      // Mark new activity for certain events
      if (['added', 'completed', 'failed'].includes(event.type)) {
        hasNewActivity = true;
      }

      return {
        ...state,
        uploads,
        hasNewActivity,
      };
    });
  });

  // Initialize with current uploads
  update((state) => ({
    ...state,
    uploads: uploadService.getAllUploads(),
  }));

  return {
    subscribe,

    // Actions
    expand() {
      update((state) => ({
        ...state,
        isExpanded: true,
        hasNewActivity: false, // Clear new activity when expanded
      }));
    },

    collapse() {
      update((state) => ({
        ...state,
        isExpanded: false,
      }));
    },

    toggle() {
      update((state) => ({
        ...state,
        isExpanded: !state.isExpanded,
        hasNewActivity: state.isExpanded ? state.hasNewActivity : false, // Clear if expanding
      }));
    },

    clearNewActivity() {
      update((state) => ({
        ...state,
        hasNewActivity: false,
      }));
    },

    // Upload actions (delegate to service)
    addFile(
      file: File,
      speakerParams?: {
        minSpeakers?: number | null;
        maxSpeakers?: number | null;
        numSpeakers?: number | null;
      },
      collectionIds?: string[],
      tagNames?: string[]
    ) {
      return uploadService.addUpload(
        'file',
        file,
        undefined,
        speakerParams,
        collectionIds,
        tagNames
      );
    },

    addFiles(files: File[], collectionIds?: string[], tagNames?: string[]) {
      return uploadService.addMultipleFiles(files, collectionIds, tagNames);
    },

    addUrl(url: string, collectionIds?: string[], tagNames?: string[]) {
      return uploadService.addUpload('url', url, undefined, undefined, collectionIds, tagNames);
    },

    addRecording(blob: Blob, name?: string, collectionIds?: string[], tagNames?: string[]) {
      return uploadService.addUpload('recording', blob, name, undefined, collectionIds, tagNames);
    },

    addExtractedAudio(
      audioBlob: Blob,
      filename: string,
      extractionMetadata: ExtractedAudioMetadata,
      compressionRatio: number
    ) {
      return uploadService.addExtractedAudio(
        audioBlob,
        filename,
        extractionMetadata,
        compressionRatio
      );
    },

    retry(uploadId: string) {
      uploadService.retryUpload(uploadId);
    },

    cancel(uploadId: string) {
      uploadService.cancelUpload(uploadId);
      update((state) => ({
        ...state,
        uploads: uploadService.getAllUploads(),
      }));
    },

    remove(uploadId: string) {
      uploadService.removeUpload(uploadId);
      update((state) => ({
        ...state,
        uploads: uploadService.getAllUploads(),
      }));
    },

    clearCompleted() {
      uploadService.clearCompleted();
      update((state) => ({
        ...state,
        uploads: uploadService.getAllUploads(),
      }));
    },

    /**
     * Full reset — cancels all uploads, clears persisted queue, and
     * resets the store. Called on logout to prevent User A's upload
     * state from leaking into User B's session.
     */
    reset() {
      uploadService.reset();
      set({ uploads: [], isExpanded: false, hasNewActivity: false });
    },

    // Cleanup
    destroy() {
      if (eventListenerCleanup) {
        eventListenerCleanup();
        eventListenerCleanup = null;
      }
    },
  };
}

// Create the store instance
export const uploadsStore = createUploadStore();

// Derived stores for convenience
export const activeUploads = derived(uploadsStore, ($store) =>
  $store.uploads.filter(
    (upload) =>
      upload.status === 'uploading' ||
      upload.status === 'processing' ||
      upload.status === 'preparing'
  )
);

export const queuedUploads = derived(uploadsStore, ($store) =>
  $store.uploads.filter((upload) => upload.status === 'queued')
);

export const completedUploads = derived(uploadsStore, ($store) =>
  $store.uploads.filter((upload) => upload.status === 'completed')
);

export const failedUploads = derived(uploadsStore, ($store) =>
  $store.uploads.filter((upload) => upload.status === 'failed')
);

export const uploadCount = derived(uploadsStore, ($store) => $store.uploads.length);

export const activeUploadCount = derived(activeUploads, ($uploads) => $uploads.length);

export const totalProgress = derived(activeUploads, ($uploads) => {
  if ($uploads.length === 0) return 0;

  const totalProgress = $uploads.reduce((sum, upload) => sum + upload.progress, 0);
  return Math.round(totalProgress / $uploads.length);
});

export const hasActiveUploads = derived(activeUploadCount, ($count) => $count > 0);

export const isExpanded = derived(uploadsStore, ($store) => $store.isExpanded);

export const hasNewActivity = derived(uploadsStore, ($store) => $store.hasNewActivity);

// Overall upload statistics
export const uploadStats = derived(uploadsStore, ($store) => {
  const uploads = $store.uploads;
  return {
    total: uploads.length,
    active: uploads.filter((u) => ['uploading', 'processing', 'preparing'].includes(u.status))
      .length,
    queued: uploads.filter((u) => u.status === 'queued').length,
    completed: uploads.filter((u) => u.status === 'completed').length,
    failed: uploads.filter((u) => u.status === 'failed').length,
    cancelled: uploads.filter((u) => u.status === 'cancelled').length,
  };
});

// Estimated time remaining for all active uploads
export const estimatedTimeRemaining = derived(activeUploads, ($uploads) => {
  const timesRemaining = $uploads
    .map((upload) => upload.estimatedTime)
    .filter((time) => time && time !== '');

  if (timesRemaining.length === 0) return '';

  // Return the longest estimated time (most conservative estimate)
  return timesRemaining.reduce((longest, current) => {
    if (!longest) return current;
    if (!current) return longest;

    // Simple comparison - in a real app you'd parse and compare properly
    return current.length > longest.length ? current : longest;
  }, '');
});
