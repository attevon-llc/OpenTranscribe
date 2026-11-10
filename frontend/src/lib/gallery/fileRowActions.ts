import { resolve } from '$app/paths';
import { goto } from '$app/navigation';
import { galleryStore } from '$stores/gallery';
import { formatDate } from '$lib/utils/formatting';
import { prefetchFileDetails } from '$lib/prefetch';
import type { MediaFile } from '$lib/types/media';

/** The server's `formatted_upload_date` is English-only; format the date in the app language. */
export function uploadDate(file: MediaFile, lang: string): string {
  return file.upload_time ? formatDate(file.upload_time, lang) : file.formatted_upload_date ?? '';
}

/**
 * Click on a gallery card or list row. Returns the uuid now navigating (or the
 * unchanged `navigatingTo`) so the caller can keep its double-click guard.
 */
export function activateFileRow(
  file: MediaFile,
  e: MouseEvent,
  isSelecting: boolean,
  navigatingTo: string | null
): string | null {
  e.preventDefault();
  const modifier = e.shiftKey || e.ctrlKey || e.metaKey;
  if (isSelecting) {
    if (modifier) {
      galleryStore.handleMultiSelect(file.uuid, e.shiftKey);
    } else {
      galleryStore.toggleFileSelection(file.uuid);
    }
    return navigatingTo;
  }
  if (modifier) {
    galleryStore.handleMultiSelect(file.uuid, e.shiftKey);
    return navigatingTo;
  }
  if (navigatingTo) return navigatingTo;
  void goto(resolve('/files/[id]', { id: file.uuid }));
  return file.uuid;
}

/** Prefetch on mousedown, slightly earlier than click, to head-start detail loading. */
export function prefetchOnMouseDown(file: MediaFile, isSelecting: boolean): void {
  if (!isSelecting) prefetchFileDetails(file.uuid, file.status);
}

export function handleCheckboxChange(fileId: string, e: Event): void {
  e.stopPropagation();
  e.preventDefault();
  galleryStore.toggleFileSelection(fileId);
}

export function handleCheckboxLabelClick(fileId: string, e: MouseEvent): void {
  if (e.shiftKey || e.ctrlKey || e.metaKey) {
    e.preventDefault();
    galleryStore.handleMultiSelect(fileId, e.shiftKey);
  }
}
