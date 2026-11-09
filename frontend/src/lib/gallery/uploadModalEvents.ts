/**
 * Window events that open the gallery's upload modal from elsewhere (the navbar's Add Media menu,
 * the recording popup), and hand the request on to FileUploader once it has mounted.
 *
 * `window` events are not buffered, so a fixed sleep before re-dispatching is a race, not a safety
 * margin: if FileUploader's `onMount` (which subscribes to these) had not run yet, the request was
 * silently dropped. `tick()` resolves once Svelte has flushed the DOM update that mounts it (#649).
 */
import { tick } from 'svelte';

export function listenForUploadRequests(openModal: () => void): () => void {
  const onOpen = (event: Event) => {
    openModal();
    const activeTab = (event as CustomEvent).detail?.activeTab;
    void tick().then(() => {
      if (activeTab) {
        window.dispatchEvent(new CustomEvent('setFileUploaderTab', { detail: { activeTab } }));
      }
    });
  };
  const onRecordedFile = (event: Event) => {
    const file = (event as CustomEvent).detail?.file;
    if (!file) return;
    openModal();
    void tick().then(() => {
      window.dispatchEvent(new CustomEvent('directFileUpload', { detail: { file } }));
    });
  };
  window.addEventListener('openAddMediaModal', onOpen);
  window.addEventListener('uploadRecordedFile', onRecordedFile);
  return () => {
    window.removeEventListener('openAddMediaModal', onOpen);
    window.removeEventListener('uploadRecordedFile', onRecordedFile);
  };
}
