/**
 * DEFECT THIS CATCHES (H5, adversarial-review follow-up): the mobile/tablet
 * (`max-width: 1200px`) rule that raises a modal above the navbar used to be
 * an unconditional `z-index: var(--z-modal) !important`, which clobbered a
 * caller's intentionally HIGHER explicit `zIndex` (e.g. `SelectiveReprocessModal`
 * passes `--z-toast`, 9999, specifically so it layers above another modal it
 * can be opened from). On any viewport ≤1200px that contract silently broke —
 * the nested modal collapsed to the same z-index tier as its parent, with no
 * visible difference on desktop to catch it.
 *
 * The fix mirrors the inline `zIndex` prop into a `--modal-instance-z-index`
 * CSS custom property so the mobile rule (`BaseModal.svelte`'s
 * `@media (max-width: 1200px)` block) can take the GREATER of the tier floor
 * and the caller's value (`max(...)`) instead of overwriting it. jsdom has no
 * real CSS cascade/media-query engine, so it cannot exercise the media query
 * itself — these tests pin the one thing that IS verifiable outside a
 * browser: the custom property the media rule reads mirrors the `zIndex`
 * prop exactly, for every value a real caller passes. A regression that
 * dropped the mirroring (reverting to the bare inline `z-index: {zIndex}`)
 * would fail every case here. The actual mobile-viewport stacking was
 * additionally confirmed by hand in a real browser (see the fix commit).
 */
import { describe, it, expect, vi } from 'vitest';
import { render } from '@testing-library/svelte';
import BaseModal from './BaseModal.svelte';

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

function backdrop(container: HTMLElement): HTMLElement {
  const el = container.querySelector('.modal-backdrop');
  if (!el) throw new Error('.modal-backdrop not rendered');
  return el as HTMLElement;
}

describe('BaseModal z-index contract', () => {
  it('mirrors the default zIndex into --modal-instance-z-index', () => {
    const { container } = render(BaseModal, { props: { isOpen: true, title: 'Default' } });
    const el = backdrop(container);
    expect(el.style.zIndex).toBe('var(--z-modal)');
    expect(el.style.getPropertyValue('--modal-instance-z-index').trim()).toBe('var(--z-modal)');
  });

  it('mirrors a caller-supplied HIGHER zIndex (e.g. the --z-toast tier a nested modal needs)', () => {
    const { container } = render(BaseModal, {
      props: { isOpen: true, title: 'Nested', zIndex: 9999 },
    });
    const el = backdrop(container);
    expect(el.style.zIndex).toBe('9999');
    expect(el.style.getPropertyValue('--modal-instance-z-index').trim()).toBe('9999');
  });

  it('mirrors a caller-supplied numeric zIndex at the base tier identically', () => {
    const { container } = render(BaseModal, {
      props: { isOpen: true, title: 'Explicit base', zIndex: 1300 },
    });
    const el = backdrop(container);
    expect(el.style.zIndex).toBe('1300');
    expect(el.style.getPropertyValue('--modal-instance-z-index').trim()).toBe('1300');
  });
});

/**
 * Some dialogs hold in-progress user work that a stray click outside must not
 * destroy — the upload wizard (#739) carries a chosen file plus tags,
 * collections and speaker settings. Those opt out of close-on-backdrop while
 * keeping the Escape key and the X button, so there is still an obvious way out.
 */
describe('BaseModal backdrop dismissal', () => {
  it('closes on a backdrop click by default', async () => {
    const onClose = vi.fn();
    const { container } = render(BaseModal, {
      props: { isOpen: true, title: 'Dismissable', onClose },
    });

    backdrop(container).click();

    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('does not close on a backdrop click when closeOnBackdropClick is false', async () => {
    const onClose = vi.fn();
    const { container } = render(BaseModal, {
      props: { isOpen: true, title: 'Protected', onClose, closeOnBackdropClick: false },
    });

    backdrop(container).click();

    expect(onClose).not.toHaveBeenCalled();
  });

  it('still closes via the X button when closeOnBackdropClick is false', async () => {
    const onClose = vi.fn();
    const { container } = render(BaseModal, {
      props: { isOpen: true, title: 'Protected', onClose, closeOnBackdropClick: false },
    });

    (container.querySelector('.modal-close-button') as HTMLElement).click();

    expect(onClose).toHaveBeenCalledTimes(1);
  });
});

describe('BaseModal height prop', () => {
  /**
   * The gallery's three big dialogs must all be the same size, so the upload
   * wizard stops resizing between its six steps (#739).
   *
   * That was first done from the call site, as
   * `.host :global(.modal-container) { height: … }`. It stopped applying: the
   * host's only child is the `<BaseModal>` COMPONENT, so the descendant half of
   * that selector crosses a component boundary, and svelte-check reported no
   * unused-selector warning to say so. Measured live, all three dialogs were
   * back to content height (548 / 462 / 655 px).
   *
   * `height` is now a prop applied inline, exactly as `maxWidth` already was —
   * no cascade, no specificity, no boundary to cross.
   */
  it('applies a supplied height inline on the container', () => {
    const { container } = render(BaseModal, {
      props: { isOpen: true, title: 'Sized', height: 'min(90vh, 780px)' },
    });
    const el = container.querySelector('.modal-container') as HTMLElement;
    expect(el.style.height).toBe('min(90vh, 780px)');
  });

  it('sets no height at all when none is supplied', () => {
    // The control: smaller consumers (confirmations, pickers) must stay
    // content-sized, so the prop has to be genuinely optional.
    const { container } = render(BaseModal, {
      props: { isOpen: true, title: 'Unsized' },
    });
    const el = container.querySelector('.modal-container') as HTMLElement;
    expect(el.style.height).toBe('');
  });

  it('still applies maxWidth alongside it', () => {
    const { container } = render(BaseModal, {
      props: { isOpen: true, title: 'Both', maxWidth: '720px', height: '780px' },
    });
    const el = container.querySelector('.modal-container') as HTMLElement;
    expect(el.style.maxWidth).toBe('720px');
    expect(el.style.height).toBe('780px');
  });
});
