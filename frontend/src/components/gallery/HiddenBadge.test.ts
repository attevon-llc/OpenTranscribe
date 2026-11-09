/**
 * The "Hidden" pill on a gallery card (issue #1102): present only for a file the caller hid,
 * and its Unhide button unhides that one file and reloads the gallery.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/svelte';

vi.mock('$stores/locale', async () =>
  (await import('$testMocks/galleryToolbarMocks')).englishLocale()
);
vi.mock('$stores/gallery', async () =>
  (await import('$testMocks/galleryToolbarMocks')).galleryStoreMock()
);
const setHidden = vi.hoisted(() => vi.fn());
vi.mock('$lib/api/hiddenMedia', () => ({ HiddenMediaApi: { setHidden } }));
const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }));
vi.mock('$stores/toast', () => ({ toastStore: toast }));

import HiddenBadge from './HiddenBadge.svelte';

const { galleryTriggers, resetGalleryDouble } = await import('$testMocks/galleryToolbarMocks');

beforeEach(resetGalleryDouble);

describe('HiddenBadge', () => {
  it('renders nothing for a file the caller has not hidden', () => {
    render(HiddenBadge, { props: { file: { uuid: 'a', is_hidden: false } as never } });
    expect(screen.queryByTestId('hidden-badge')).toBeNull();
  });

  it('unhides the file and reloads the gallery', async () => {
    setHidden.mockResolvedValue(undefined);
    render(HiddenBadge, { props: { file: { uuid: 'a', is_hidden: true } as never } });
    expect(screen.getByText('Hidden')).toBeInTheDocument();

    await fireEvent.click(screen.getByRole('button', { name: 'Unhide' }));

    await waitFor(() => expect(galleryTriggers.notifyHiddenChanged).toHaveBeenCalledTimes(1));
    expect(setHidden).toHaveBeenCalledWith('a', false);
    expect(toast.success).toHaveBeenCalledWith('The file is shown in your view again');
  });

  it('a list row shows the pill without a button inside its link', () => {
    render(HiddenBadge, {
      props: { file: { uuid: 'a', is_hidden: true } as never, showUnhide: false },
    });
    expect(screen.getByText('Hidden')).toBeInTheDocument();
    expect(screen.queryByRole('button')).toBeNull();
  });

  it('reports a failed unhide and does not reload', async () => {
    setHidden.mockRejectedValue(new Error('boom'));
    render(HiddenBadge, { props: { file: { uuid: 'a', is_hidden: true } as never } });

    await fireEvent.click(screen.getByRole('button', { name: 'Unhide' }));

    await waitFor(() => expect(toast.error).toHaveBeenCalledTimes(1));
    expect(galleryTriggers.notifyHiddenChanged).not.toHaveBeenCalled();
  });
});
