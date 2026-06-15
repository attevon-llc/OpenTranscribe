/**
 * Global quota-exceeded modal trigger (cloud edition).
 *
 * The axios 402 interceptor and the FileUploader pre-flight check both need to
 * pop the same "you're out of transcription hours" modal, but neither owns a
 * component instance. This tiny store lets any code request the modal; the
 * single `<QuotaExceededModal>` mounted in the app shell renders it.
 *
 * Community/self-host never triggers it (the backend never returns 402 and the
 * pre-flight check is cloud-gated), so it stays closed.
 */
import { writable } from 'svelte/store';

export interface QuotaModalState {
  open: boolean;
  /** Optional backend-provided detail message to show in the modal. */
  message: string;
}

export const quotaModal = writable<QuotaModalState>({ open: false, message: '' });

/** Open the quota-exceeded modal, optionally with a backend detail message. */
export function showQuotaExceeded(message = ''): void {
  quotaModal.set({ open: true, message });
}

/** Close the quota-exceeded modal. */
export function dismissQuotaExceeded(): void {
  quotaModal.set({ open: false, message: '' });
}
