/**
 * Close-on-Escape for a container, without letting the key escape to the page.
 *
 * Factored out for the same reason `clickOutside` and `focusTrap` are: it is a
 * behaviour several panels need, and each hand-rolled copy is a chance to forget
 * the `stopPropagation` below.
 *
 * ⚠️ **`stopPropagation` is the load-bearing part.** The chat page listens for
 * Escape on `<svelte:window>` and uses it to CANCEL an in-flight generation. The
 * key bubbles from whatever is focused inside the panel up to the window, so
 * without stopping it there, closing a panel would also abort the user's answer.
 * Because both the panel and the page ultimately see the same bubbling event,
 * stopping it at the container is what orders them correctly — a second window
 * listener could not, since listener order is registration order.
 *
 * Attaching the listener here rather than as `on:keydown` on the element is also
 * what keeps the markup honest: a panel is not an interactive element, and
 * Svelte's a11y lint is right to say so. The keyboard affordance belongs to the
 * behaviour, not to the tag.
 *
 * @example
 * <aside use:escapeKey={{ enabled: open, onEscape: () => dispatch('close') }}>
 */
export interface EscapeKeyOptions {
  /** Ignore the key while false — e.g. the panel is closed. */
  enabled?: boolean;
  onEscape: () => void;
}

export function escapeKey(node: HTMLElement, options: EscapeKeyOptions) {
  let current = options;

  function handle(event: KeyboardEvent): void {
    if (current.enabled === false) return;
    if (event.key !== 'Escape') return;
    event.stopPropagation();
    current.onEscape();
  }

  node.addEventListener('keydown', handle);

  return {
    update(next: EscapeKeyOptions) {
      current = next;
    },
    destroy() {
      node.removeEventListener('keydown', handle);
    },
  };
}
