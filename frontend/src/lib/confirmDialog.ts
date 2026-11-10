/**
 * State for one `ConfirmationModal`: what it says, what to run on confirm, what to run on cancel.
 *
 * Five screens each hand-rolled this (title/message/callback variables plus a
 * `handleConfirmModalConfirm`/`Cancel` pair). The markup stays in the component, because every
 * screen has its own button labels; only the state machine lives here.
 *
 *     const confirmDialog = createConfirmDialog();
 *     const { isOpen, title, message, confirmText, variant } = confirmDialog;
 *     confirmDialog.request('Delete?', 'Really?', () => remove(id), { variant: 'danger' });
 *
 *     <ConfirmationModal bind:isOpen={$isOpen} title={$title} message={$message}
 *       confirmText={$confirmText} variant={$variant}
 *       on:confirm={confirmDialog.confirm} on:cancel={confirmDialog.cancel}
 *       on:close={confirmDialog.cancel} />
 */
import { writable, type Writable } from 'svelte/store';

export type ConfirmVariant = 'primary' | 'danger' | 'warning';

export interface ConfirmRequestOptions {
  /** Confirm button label (empty keeps the modal's default). */
  confirmText?: string;
  /** Runs when the user backs out (cancel or close), never after a confirm. */
  onCancel?: () => void;
  /** Confirm button colour; `danger` also makes the modal an `alertdialog`. Default `primary`. */
  variant?: ConfirmVariant;
}

export interface ConfirmDialog {
  /** Bind to the modal's `isOpen`: the modal closes itself after confirm/cancel/close. */
  isOpen: Writable<boolean>;
  title: Writable<string>;
  message: Writable<string>;
  confirmText: Writable<string>;
  variant: Writable<ConfirmVariant>;
  request(
    title: string,
    message: string,
    onConfirm: () => void | Promise<void>,
    options?: ConfirmRequestOptions
  ): void;
  confirm(): void;
  cancel(): void;
}

export function createConfirmDialog(): ConfirmDialog {
  const isOpen = writable(false);
  const title = writable('');
  const message = writable('');
  const confirmText = writable('');
  const variant = writable<ConfirmVariant>('primary');
  let onConfirm: (() => void | Promise<void>) | null = null;
  let onCancel: (() => void) | null = null;

  return {
    isOpen,
    title,
    message,
    confirmText,
    variant,
    request(newTitle, newMessage, callback, options = {}) {
      title.set(newTitle);
      message.set(newMessage);
      confirmText.set(options.confirmText ?? '');
      variant.set(options.variant ?? 'primary');
      onConfirm = callback;
      onCancel = options.onCancel ?? null;
      isOpen.set(true);
    },
    confirm() {
      // Clear and close BEFORE running the callback so a throwing callback cannot leave a
      // stale callback armed or the dialog open.
      const callback = onConfirm;
      onConfirm = null;
      onCancel = null;
      isOpen.set(false);
      // An async callback's rejection would otherwise surface as an unhandled rejection.
      const result = callback?.();
      if (result instanceof Promise) {
        result.catch((e) => console.error('Confirmed action failed:', e));
      }
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
