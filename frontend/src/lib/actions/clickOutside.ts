export interface ClickOutsideOptions {
  /** When false the action is inert — no `click_outside` event fires. Default: true. */
  enabled?: boolean;
  /**
   * Elements whose clicks should NOT count as "outside" (e.g. the trigger button that
   * opens the panel, so the opening click doesn't immediately close it).
   */
  ignore?: (HTMLElement | null)[];
}

/**
 * Svelte action: dispatches a `click_outside` CustomEvent when a click lands outside
 * `node`. Uses a capture-phase document listener so it works regardless of stopPropagation
 * inside the panel. Backward compatible — `use:clickOutside` with no options behaves as before.
 *
 * Usage:
 *   <div use:clickOutside on:click_outside={close}>…</div>
 *   <div use:clickOutside={{ enabled: open, ignore: [triggerEl] }} on:click_outside={close}>…</div>
 */
export function clickOutside(node: HTMLElement, options: ClickOutsideOptions = {}) {
  let enabled = options.enabled ?? true;
  let ignore = options.ignore ?? [];

  const handleClick = (event: MouseEvent) => {
    if (!enabled || event.defaultPrevented) return;
    const target = event.target as Node;
    if (node.contains(target)) return;
    if (ignore.some((el) => el && el.contains(target))) return;
    node.dispatchEvent(new CustomEvent('click_outside'));
  };

  document.addEventListener('click', handleClick, true);

  return {
    update(newOptions: ClickOutsideOptions = {}) {
      enabled = newOptions.enabled ?? true;
      ignore = newOptions.ignore ?? [];
    },
    destroy() {
      document.removeEventListener('click', handleClick, true);
    },
  };
}
