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
}));

import axiosInstance from '../lib/axios';
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
