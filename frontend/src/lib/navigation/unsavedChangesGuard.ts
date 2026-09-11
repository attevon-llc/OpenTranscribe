/**
 * Stop a page from silently discarding unsaved edits when the user leaves (issue #787).
 *
 * Speaker naming is the most manual, least re-doable work in the product — the app
 * deliberately does NOT auto-apply LLM speaker suggestions, so this is exactly the human
 * judgement it asks for. Before this, an "unsaved" dot beside the panel heading was the
 * entire protection: clicking a nav link or closing the tab mid-pass lost the work with no
 * prompt at all.
 *
 * ⚠️ **One callback covers BOTH cases, and issue #787's "both handlers are needed" is wrong
 * for SvelteKit 2.** Verified against `@sveltejs/kit@2.70.2`,
 * `src/runtime/client/client.js::_start_router`: the router installs its own `beforeunload`
 * listener, invokes every `beforeNavigate` callback with `type: 'leave'`, and turns a
 * `cancel()` into `e.preventDefault()` + `e.returnValue = ''` — the browser's native
 * "Leave site?" dialog. Adding a second `beforeunload` listener here would be a second
 * mechanism doing the same job, which this repo forbids.
 *
 * That is also why the `leave` branch must NOT call `window.confirm()`: a modal dialog inside
 * a `beforeunload` handler is ignored by every modern browser, so it would be dead code that
 * *looks* like the prompt.
 *
 * `confirm()` rather than the app's `ConfirmationModal`: `beforeNavigate` decides
 * synchronously — there is no way to await a user's answer before returning — so a custom
 * modal cannot cancel the navigation it is asking about. The alternative (let the navigation
 * happen, then offer to come back) loses the edits, which is the bug.
 */
import { beforeNavigate } from '$app/navigation';

/**
 * Register an unsaved-changes guard for the calling component.
 *
 * Must be called during component initialisation — `beforeNavigate` is a SvelteKit
 * lifecycle function and is torn down with the component automatically, so there is
 * nothing to unregister.
 *
 * @param isDirty Re-evaluated on every navigation, so a save that clears the flag clears
 *   the guard with it and produces no spurious prompt.
 * @param message Built lazily, so the caller can localise it against the current locale
 *   rather than the one that happened to be active at mount.
 */
export function guardUnsavedChanges(isDirty: () => boolean, message: () => string): void {
  beforeNavigate((navigation) => {
    if (!isDirty()) return;

    if (navigation.type === 'leave') {
      // Tab close / reload / external link. Cancelling here is what makes SvelteKit
      // call preventDefault() on its beforeunload event; the browser owns the wording.
      navigation.cancel();
      return;
    }

    if (!window.confirm(message())) {
      navigation.cancel();
    }
  });
}
