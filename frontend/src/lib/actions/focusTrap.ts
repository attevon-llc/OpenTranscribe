export interface FocusTrapOptions {
  /**
   * When false the trap is inert (no focusing, no Tab cycling, no focus restore).
   * Toggle this with the open state of a dialog. Default: true.
   */
  enabled?: boolean;
}

const FOCUSABLE_SELECTOR = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
  'audio[controls]',
  'video[controls]',
  '[contenteditable]:not([contenteditable="false"])',
].join(',');

function isVisible(el: HTMLElement): boolean {
  if (el.hasAttribute('hidden')) return false;
  // Use computed style (works in jsdom) rather than layout boxes, which jsdom always
  // reports as zero-sized. Walk up to catch display:none / visibility:hidden ancestors.
  let current: HTMLElement | null = el;
  while (current) {
    const style = typeof getComputedStyle === 'function' ? getComputedStyle(current) : null;
    if (style && (style.display === 'none' || style.visibility === 'hidden')) return false;
    current = current.parentElement;
  }
  return true;
}

function getFocusable(node: HTMLElement): HTMLElement[] {
  return Array.from(node.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)).filter(
    (el) =>
      !el.hasAttribute('disabled') && el.getAttribute('aria-hidden') !== 'true' && isVisible(el)
  );
}

/**
 * Svelte action: traps keyboard focus within `node` while `enabled`.
 *
 * - On enable, remembers the previously-focused element and moves focus to the first
 *   focusable element inside `node` (or `node` itself if none qualify).
 * - While enabled, Tab / Shift+Tab cycle within the focusable set instead of escaping.
 * - On disable/destroy, restores focus to the element that was focused before enabling.
 *
 * Purely additive: it does not handle Escape or backdrop clicks — the host component keeps
 * owning those. Safe to apply to an always-mounted element by toggling `{ enabled }`.
 *
 * Usage:
 *   <div use:focusTrap={{ enabled: isOpen }}>…</div>
 */
export function focusTrap(node: HTMLElement, options: FocusTrapOptions = {}) {
  let enabled = options.enabled ?? true;
  let previouslyFocused: HTMLElement | null = null;

  function handleKeydown(event: KeyboardEvent) {
    if (!enabled || event.key !== 'Tab') return;

    const focusable = getFocusable(node);
    if (focusable.length === 0) {
      // Nothing to cycle through — keep focus on the container.
      event.preventDefault();
      node.focus();
      return;
    }

    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    const active = (document.activeElement as HTMLElement) ?? null;

    if (event.shiftKey) {
      if (active === first || !node.contains(active)) {
        event.preventDefault();
        last.focus();
      }
    } else if (active === last || !node.contains(active)) {
      event.preventDefault();
      first.focus();
    }
  }

  function activate() {
    previouslyFocused = (document.activeElement as HTMLElement) ?? null;
    const focusable = getFocusable(node);
    const target = focusable[0] ?? node;
    // Ensure the container itself can receive focus as a last resort.
    if (target === node && !node.hasAttribute('tabindex')) {
      node.setAttribute('tabindex', '-1');
    }
    // Defer to let the DOM settle (transitions/render) before focusing.
    requestAnimationFrame(() => {
      if (enabled) target.focus();
    });
  }

  function restoreFocus() {
    if (previouslyFocused && typeof previouslyFocused.focus === 'function') {
      // Only restore if the element is still in the document.
      if (document.contains(previouslyFocused)) {
        previouslyFocused.focus();
      }
    }
    previouslyFocused = null;
  }

  node.addEventListener('keydown', handleKeydown);
  if (enabled) activate();

  return {
    update(newOptions: FocusTrapOptions = {}) {
      const next = newOptions.enabled ?? true;
      if (next === enabled) return;
      enabled = next;
      if (enabled) {
        activate();
      } else {
        restoreFocus();
      }
    },
    destroy() {
      node.removeEventListener('keydown', handleKeydown);
      if (enabled) restoreFocus();
    },
  };
}
