/**
 * CommentSection — the media comment path.
 *
 * Pins the two invariants the form depends on: a marked timestamp is required
 * before submit enables, and comments are fetched from the media nested route.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/svelte';

vi.mock('../lib/axios', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
  isRequestCancelled: () => false,
}));

vi.mock('../stores/auth', () => ({
  authStore: {
    subscribe: (run: (value: { user: { uuid: string; email: string } | null }) => void) => {
      run({ user: { uuid: 'user-1', email: 'me@example.com' } });
      return () => {};
    },
  },
}));

vi.mock('../stores/toast', () => ({
  toastStore: { error: vi.fn(), success: vi.fn() },
}));

vi.mock('../stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string, opts?: Record<string, unknown>) => string) => void) => {
      run((key: string, opts?: Record<string, unknown>) =>
        opts ? `${key}:${JSON.stringify(opts)}` : key
      );
      return () => {};
    },
  },
  locale: {
    subscribe: (run: (value: string) => void) => {
      run('en');
      return () => {};
    },
  },
}));

import axiosInstance from '../lib/axios';
import { toastStore } from '$stores/toast';
import CommentSection from './CommentSection.svelte';

describe('CommentSection — media mode', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(axiosInstance.get).mockResolvedValue({ data: [] } as never);
  });

  it('still requires a marked timestamp before submit enables', async () => {
    render(CommentSection, { props: { fileId: 'file-uuid-1' } });
    await waitFor(() => expect(axiosInstance.get).toHaveBeenCalled());

    const textarea = screen.getByPlaceholderText('comments.placeholder');
    await fireEvent.input(textarea, { target: { value: 'a note' } });

    const submit = screen.getByText('comments.addComment').closest('button') as HTMLButtonElement;
    expect(submit.disabled).toBe(true);
  });

  it('still fetches from the media nested route', async () => {
    render(CommentSection, { props: { fileId: 'file-uuid-1' } });

    await waitFor(() => {
      expect(axiosInstance.get).toHaveBeenCalledWith('/comments/files/file-uuid-1/comments');
    });
  });
});

describe('CommentSection — adding a comment', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(axiosInstance.get).mockResolvedValue({ data: [] } as never);
  });

  async function fillAndMark(currentTime = 4) {
    render(CommentSection, { props: { fileId: 'file-uuid-1', currentTime } });
    await waitFor(() => expect(axiosInstance.get).toHaveBeenCalled());
    await fireEvent.input(screen.getByPlaceholderText('comments.placeholder'), {
      target: { value: 'a note' },
    });
    await fireEvent.click(screen.getByText('comments.markCurrentTime'));
    return screen.getByText('comments.addComment').closest('button') as HTMLButtonElement;
  }

  it('posts text and the marked time to the nested route, then lists the comment', async () => {
    vi.mocked(axiosInstance.post).mockResolvedValue({
      data: {
        uuid: 'c1',
        text: 'a note',
        timestamp: 4,
        media_file_id: 'file-uuid-1',
        user_id: 'user-1',
        user: { uuid: 'user-1', email: 'me@example.com' },
        created_at: '2026-01-01T00:00:00Z',
      },
    } as never);
    const submit = await fillAndMark();
    expect(submit.disabled).toBe(false);

    await fireEvent.click(submit);

    await waitFor(() => expect(screen.getByText('a note')).toBeTruthy());
    expect(axiosInstance.post).toHaveBeenCalledTimes(1);
    expect(axiosInstance.post).toHaveBeenCalledWith('/comments/files/file-uuid-1/comments', {
      text: 'a note',
      timestamp: 4,
      media_file_id: 'file-uuid-1',
    });
    expect(toastStore.error).not.toHaveBeenCalled();
  });

  it('says on the page, not only in a tooltip, why Add stays disabled without a marked time', async () => {
    // The reason used to live only in the disabled button's `title`, which a disabled control
    // often never shows: typing a comment and pressing Add did visibly nothing.
    render(CommentSection, { props: { fileId: 'file-uuid-1' } });
    await waitFor(() => expect(axiosInstance.get).toHaveBeenCalled());
    expect(screen.queryByText('comments.mustAddTextAndTime')).toBeNull();

    await fireEvent.input(screen.getByPlaceholderText('comments.placeholder'), {
      target: { value: 'a note' },
    });

    expect(screen.getByText('comments.mustAddTextAndTime')).toBeTruthy();
    await fireEvent.click(screen.getByText('comments.markCurrentTime'));
    expect(screen.queryByText('comments.mustAddTextAndTime')).toBeNull();
  });

  it("shows the server's reason when the comment is refused, not the bare status line", async () => {
    vi.mocked(axiosInstance.post).mockRejectedValue({
      message: 'Request failed with status code 403',
      response: { status: 403, data: { detail: 'You do not have permission to access this file' } },
    });
    const submit = await fillAndMark();

    await fireEvent.click(submit);

    await waitFor(() => expect(toastStore.error).toHaveBeenCalledTimes(1));
    expect(toastStore.error).toHaveBeenCalledWith(
      'comments.addFailed:{"error":"You do not have permission to access this file"}'
    );
  });
});
