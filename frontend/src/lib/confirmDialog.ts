/**
 * State for one `ConfirmationModal`: what it says, what to run on confirm, what to run on cancel.
 *
 * Five screens each hand-rolled this (title/message/callback variables plus a
 * `handleConfirmModalConfirm`/`Cancel` pair). The markup stays in the component, because every
 * screen has its own button labels and classes; only the state machine lives here.
 *
 *     const confirmDialog = createConfirmDialog();
 *     const { isOpen, title, message, confirmText } = confirmDialog;
 *     confirmDialog.request('Delete?', 'Really?', () => remove(id));
 *
 *     <ConfirmationModal bind:isOpen={$isOpen} title={$title} message={$message}
 *       on:confirm={confirmDialog.confirm} on:cancel={confirmDialog.cancel}
 *       on:close={confirmDialog.cancel} />
 */
import { writable, type Writable } from 'svelte/store';

export interface ConfirmDialog {
  /** Bind to the modal's `isOpen`: the modal closes itself after confirm/cancel/close. */
  isOpen: Writable<boolean>;
  title: Writable<string>;
  message: Writable<string>;
  confirmText: Writable<string>;
  /**
   * Open the dialog. `confirmText` is the confirm button label (empty keeps the caller's
   * default); `onCancel` runs when the user backs out (cancel or close), never after a confirm.
   */
  request(
    title: string,
    message: string,
    onConfirm: () => void | Promise<void>,
    confirmText?: string,
    onCancel?: (() => void) | null
  ): void;
  confirm(): void;
  cancel(): void;
}

export function createConfirmDialog(): ConfirmDialog {
  const isOpen = writable(false);
  const title = writable('');
  const message = writable('');
  const confirmText = writable('');
  let onConfirm: (() => void | Promise<void>) | null = null;
  let onCancel: (() => void) | null = null;

  return {
    isOpen,
    title,
    message,
    confirmText,
    request(newTitle, newMessage, callback, newConfirmText = '', cancelCallback = null) {
      title.set(newTitle);
      message.set(newMessage);
      confirmText.set(newConfirmText);
      onConfirm = callback;
      onCancel = cancelCallback;
      isOpen.set(true);
    },
    confirm() {
      // Clear and close BEFORE running the callback so a throwing callback cannot leave a
      // stale callback armed or the dialog open.
      const callback = onConfirm;
      onConfirm = null;
      onCancel = null;
      isOpen.set(false);
      callback?.();
    },
    cancel() {
      const callback = onCancel;
      onConfirm = null;
      onCancel = null;
      isOpen.set(false);
      callback?.();
    },
  };
}
