/**
 * On a held file a NEW comment can still be written (it is recorded as a hold annotation), with
 * a visible hint, and the speaker editor cannot be opened (legal-hold plan, Lane D).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/svelte';

const mockAxios = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
}));
vi.mock('$lib/axios', () => ({ default: mockAxios, isRequestCancelled: () => false }));
vi.mock('$stores/auth', async () => {
  const { readable } = await import('svelte/store');
  return { authStore: readable({ user: { uuid: 'u1', email: 'me@example.com' } }) };
});
vi.mock('$stores/locale', async () => {
  const mocks = await import('../../test-mocks/galleryToolbarMocks');
  const { readable } = await import('svelte/store');
  return { ...mocks.englishLocale(), locale: readable('en') };
});
vi.mock('$stores/toast', () => ({ toastStore: { success: vi.fn(), error: vi.fn() } }));

import FrozenContextTestHost from './FrozenContextTestHost.svelte';
import CommentSection from '$components/CommentSection.svelte';
import TranscriptActionsBar from '$components/transcript/TranscriptActionsBar.svelte';

beforeEach(() => {
  vi.clearAllMocks();
  mockAxios.get.mockResolvedValue({ data: [] });
});

function comments(frozen: boolean) {
  return render(FrozenContextTestHost, {
    props: { component: CommentSection, props: { fileId: 'f1', duration: 60 }, frozen },
  } as never);
}

describe('comments on a held file', () => {
  it('keep the new-comment box enabled and say that the comment is recorded', async () => {
    comments(true);
    await waitFor(() => expect(mockAxios.get).toHaveBeenCalled());
    expect(screen.getByPlaceholderText('Type your comment or note here...')).toBeEnabled();
    expect(screen.getByTestId('hold-comment-hint')).toHaveTextContent(
      'New comments are added and recorded'
    );
  });

  it('show no hold hint on a free file', async () => {
    comments(false);
    await waitFor(() => expect(mockAxios.get).toHaveBeenCalled());
    expect(screen.queryByTestId('hold-comment-hint')).toBeNull();
  });
});

describe('the speaker editor on a held file', () => {
  const props = { file: { uuid: 'f1' }, diarizationDisabled: false, isEditingSpeakers: false };

  it('cannot be opened: the button is disabled and explained', () => {
    render(FrozenContextTestHost, {
      props: { component: TranscriptActionsBar, props, frozen: true },
    } as never);
    const button = screen.getByRole('button', { name: /Edit Speakers/i });
    expect(button).toBeDisabled();
    expect(
      document.getElementById(button.getAttribute('aria-describedby') as string)
    ).toHaveTextContent('Locked by a legal hold');
  });

  it('opens normally on a free file', () => {
    render(FrozenContextTestHost, {
      props: { component: TranscriptActionsBar, props, frozen: false },
    } as never);
    expect(screen.getByRole('button', { name: /Edit Speakers/i })).toBeEnabled();
  });
});
