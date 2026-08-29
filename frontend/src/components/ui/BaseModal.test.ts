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
