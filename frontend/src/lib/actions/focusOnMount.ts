/**
 * Focus an element as soon as it is inserted into the DOM.
 *
 * Replaces the `autofocus` attribute, which Svelte's `a11y_autofocus` warning rejects
 * (https://svelte.dev/e/a11y_autofocus): a page-load autofocus steals focus before the
 * user has any context. Every caller here is an input that appears in response to a user
 * action (opening a modal, entering an inline rename), where moving focus to it is the
 * expected behaviour, so the focus belongs to the behaviour and not to a markup attribute.
 *
 * @example
 * <input use:focusOnMount bind:value={name} />
 */
export function focusOnMount(node: HTMLElement): void {
  node.focus();
}
