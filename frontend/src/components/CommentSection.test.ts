/**
 * CommentSection — the document arm (v400, #362 lane C5).
 *
 * Extended to also anchor notes to a `Document` (`mode="document"`), talking to
 * `/comments/documents/{documentId}/comments` instead of the media nested route,
 * and to only what genuinely diverges for that mode: no timestamp/"mark current
 * time" affordance (a document has no playback axis), so submit must enable on
 * text alone rather than requiring a marked timestamp the media form still does.
 * These pin the divergence without re-testing the whole (pre-existing) media path.
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

describe('CommentSection — document mode', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(axiosInstance.get).mockResolvedValue({ data: [] } as never);
  });

  it('fetches from the document nested route, not the media one', async () => {
    render(CommentSection, { props: { mode: 'document', documentId: 'doc-uuid-1' } });

    await waitFor(() => {
      expect(axiosInstance.get).toHaveBeenCalledWith('/comments/documents/doc-uuid-1/comments');
    });
  });

  it('enables submit on text alone — no marked timestamp required', async () => {
    render(CommentSection, { props: { mode: 'document', documentId: 'doc-uuid-1' } });
    await waitFor(() => expect(axiosInstance.get).toHaveBeenCalled());

    const textarea = screen.getByPlaceholderText('comments.placeholder');
    await fireEvent.input(textarea, { target: { value: 'a note' } });

    const submit = screen.getByText('comments.addComment').closest('button') as HTMLButtonElement;
    expect(submit.disabled).toBe(false);
  });

  it('never renders the "mark current time" affordance', async () => {
    render(CommentSection, { props: { mode: 'document', documentId: 'doc-uuid-1' } });
    await waitFor(() => expect(axiosInstance.get).toHaveBeenCalled());

    expect(screen.queryByText('comments.markCurrentTime')).toBeNull();
  });

  it('posts to the document route without a timestamp field on submit', async () => {
    vi.mocked(axiosInstance.post).mockResolvedValue({
      data: { uuid: 'c-1', text: 'a note', document_id: 'doc-uuid-1', user_id: 'user-1' },
    } as never);
    render(CommentSection, { props: { mode: 'document', documentId: 'doc-uuid-1' } });
    await waitFor(() => expect(axiosInstance.get).toHaveBeenCalled());

    const textarea = screen.getByPlaceholderText('comments.placeholder');
    await fireEvent.input(textarea, { target: { value: 'a note' } });
    const submit = screen.getByText('comments.addComment').closest('button') as HTMLButtonElement;
    await fireEvent.click(submit);

    await waitFor(() => {
      expect(axiosInstance.post).toHaveBeenCalledWith('/comments/documents/doc-uuid-1/comments', {
        text: 'a note',
      });
    });
  });
});

describe('CommentSection — media mode (unaffected by the document arm)', () => {
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
