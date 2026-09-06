/**
 * Move an element to `document.body` (or another target) for the lifetime of the
 * component.
 *
 * Why this exists (#740): `SegmentSpeakerDropdown` renders inside a transcript
 * segment's `<button class="segment-content">`, several levels deep in the
 * transcript DOM. A dialog rendered there is broken two ways:
 *
 *  1. **Invalid HTML.** A `<button>` may not contain interactive content, so a
 *     modal with a text input inside one is undefined behaviour.
 *  2. **`position: fixed` stops meaning "the viewport".** The backdrop's
 *     `width/height: 100%` resolved against the 548px segment row instead of the
 *     1600px viewport, so the "modal" rendered as a 548x61 strip wedged inside the
 *     transcript row — visible, but unusable, and with no backdrop.
 *
 * Portaling to `document.body` fixes both: the dialog becomes a top-level element
 * with the viewport as its containing block.
 *
 * Svelte's scoped-CSS classes travel with the node, so component styles still
 * apply after the move.
 */
export function portal(node: HTMLElement, target: HTMLElement | null = null) {
  const destination = target ?? document.body;
  destination.appendChild(node);

  return {
    destroy() {
      // The node was moved out of its Svelte-managed parent, so Svelte will not
      // remove it for us on teardown.
      node.remove();
    },
  };
}
