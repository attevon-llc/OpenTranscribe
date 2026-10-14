/**
 * `FileStatusIndicator.svelte` is the single status affordance shared by `VirtualList`
 * and `VirtualGrid` (issue #749). Before it existed, `queued` and `downloading` had
 * ZERO `.status-*` CSS in either view (§13.3 of the gallery/upload UX plan) — they
 * rendered as plain, uncoloured text while the other eight statuses got a coloured
 * dot. This suite exists specifically to prove that gap is closed: every one of the
 * ten `MediaFileStatus` members gets a status class, a translated tooltip, and (for
 * the two previously-missing ones) the same "in flight" amber treatment as
 * `pending`/`processing`/`cancelling`.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, fireEvent } from '@testing-library/svelte';
import type { MediaFileStatus } from '$lib/types/media';

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

import FileStatusIndicator from './FileStatusIndicator.svelte';

const ALL_STATUSES: MediaFileStatus[] = [
  'pending',
  'queued',
  'downloading',
  'processing',
  'completed',
  'error',
  'cancelling',
  'cancelled',
  'orphaned',
  'quarantined',
];

/** Statuses styled as "actively in flight" — same amber treatment, incl. the two
 * that used to have no styling at all. */
const IN_FLIGHT: MediaFileStatus[] = [
  'pending',
  'queued',
  'downloading',
  'processing',
  'cancelling',
];

describe('FileStatusIndicator', () => {
  it.each(ALL_STATUSES)(
    'renders a status-%s class and a translated tooltip for status "%s"',
    (status) => {
      const { container } = render(FileStatusIndicator, { props: { status } });

      const el = container.querySelector('.status-indicator') as HTMLElement;
      expect(el).toBeTruthy();
      expect(el.classList.contains(`status-${status}`)).toBe(true);
      expect(el.getAttribute('title')).toBe(`common.${status}`);
      expect(el.getAttribute('aria-label')).toBe(`common.${status}`);
    }
  );

  it('prefers file.display_status over the local i18n fallback when supplied', () => {
    const { container } = render(FileStatusIndicator, {
      props: { status: 'processing', displayStatus: 'Transcribing (42%)' },
    });

    const el = container.querySelector('.status-indicator') as HTMLElement;
    expect(el.getAttribute('title')).toBe('Transcribing (42%)');
  });

  it.each(IN_FLIGHT)(
    'gives the previously-unstyled "%s" the same in-flight treatment class group',
    (status) => {
      // Regression guard for §13.3: queued/downloading must render with a real status
      // class like every other in-flight status, not fall through to unstyled text.
      const { container } = render(FileStatusIndicator, { props: { status } });
      const el = container.querySelector('.status-indicator') as HTMLElement;
      expect(el.className).toContain(`status-${status}`);
      expect(el.getAttribute('title')).toBe(`common.${status}`);
    }
  );

  it('shows the word inline only when showLabel is set', () => {
    const { container: withoutLabel } = render(FileStatusIndicator, {
      props: { status: 'completed' },
    });
    expect(withoutLabel.querySelector('.status-text')).toBeNull();

    const { container: withLabel } = render(FileStatusIndicator, {
      props: { status: 'completed', showLabel: true },
    });
    expect(withLabel.querySelector('.status-text')?.textContent).toBe('common.completed');
  });

  it('dispatches click only when clickable is true, and never navigates the parent link', async () => {
    const handler = vi.fn();
    const { container } = render(FileStatusIndicator, {
      props: { status: 'error', clickable: true },
      events: { click: handler },
    } as never);

    const el = container.querySelector('.status-indicator') as HTMLElement;
    expect(el.classList.contains('status-clickable')).toBe(true);
    await fireEvent.click(el);
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it('does not dispatch click when clickable is false', async () => {
    const handler = vi.fn();
    const { container } = render(FileStatusIndicator, {
      props: { status: 'error', clickable: false },
      events: { click: handler },
    } as never);

    const el = container.querySelector('.status-indicator') as HTMLElement;
    expect(el.classList.contains('status-clickable')).toBe(false);
    await fireEvent.click(el);
    expect(handler).not.toHaveBeenCalled();
  });
});
