/**
 * CommentSection — the media comment path.
 *
 * A comment is timed by default and takes the playback time at the moment the user starts
 * typing (it must not drift while they write); turning "Timestamp" off makes it a general
 * comment about the whole file. Add is never a dead button: empty text gets an inline message.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/svelte';

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

const FILE = 'file-uuid-1';
const ENDPOINT = `/comments/files/${FILE}/comments`;

function saved(overrides: Record<string, unknown> = {}) {
  return {
    data: {
      uuid: 'c-new',
      text: 'a note',
      timestamp: 4,
      media_file_id: FILE,
      user_id: 'user-1',
      user: { uuid: 'user-1', email: 'me@example.com' },
      created_at: '2026-01-01T00:00:00Z',
      ...overrides,
    },
  } as never;
}

function existing(uuid: string, timestamp: number | null, text: string, created_at: string) {
  return {
    uuid,
    text,
    timestamp,
    media_file_id: FILE,
    user_id: 'user-1',
    user: { uuid: 'user-1', email: 'me@example.com' },
    created_at,
  };
}

async function mount(
  props: Record<string, unknown> = {},
  events: Record<string, (event: CustomEvent) => void> = {}
) {
  const result = render(CommentSection, { props: { fileId: FILE, ...props }, events } as never);
  await waitFor(() => expect(axiosInstance.get).toHaveBeenCalled());
  return result;
}

function textarea() {
  return screen.getByPlaceholderText('comments.placeholder') as HTMLTextAreaElement;
}

function addButton() {
  return screen.getByText('comments.addComment').closest('button') as HTMLButtonElement;
}

function timeInput() {
  return screen.getByLabelText('comments.timeFieldLabel') as HTMLInputElement;
}

async function type(value: string) {
  await fireEvent.input(textarea(), { target: { value } });
}

function postedTimestamp(): unknown {
  expect(axiosInstance.post).toHaveBeenCalledTimes(1);
  const [, payload] = vi.mocked(axiosInstance.post).mock.calls[0];
  return (payload as { timestamp: unknown }).timestamp;
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(axiosInstance.get).mockResolvedValue({ data: [] } as never);
  vi.mocked(axiosInstance.post).mockResolvedValue(saved());
  vi.mocked(axiosInstance.put).mockResolvedValue({ data: {} } as never);
});

describe('CommentSection — loading', () => {
  it('fetches from the media nested route', async () => {
    await mount();
    expect(axiosInstance.get).toHaveBeenCalledWith(ENDPOINT);
  });
});

describe('CommentSection — the composer', () => {
  it('makes a timed comment at the current playback time by default', async () => {
    await mount({ currentTime: 42 });
    const toggle = screen.getByRole('checkbox', { name: 'comments.timestampToggle' });
    expect((toggle as HTMLInputElement).checked).toBe(true);
    expect(screen.getByText('comments.kindTimedSummary:{"time":"0:42"}')).toBeTruthy();

    await type('a note');
    await fireEvent.click(addButton());

    await waitFor(() => expect(screen.getByText('a note')).toBeTruthy());
    expect(axiosInstance.post).toHaveBeenCalledWith(ENDPOINT, {
      text: 'a note',
      timestamp: 42,
      media_file_id: FILE,
    });
    expect(toastStore.error).not.toHaveBeenCalled();
  });

  it('follows the player until typing starts, then pins the time of the first keystroke', async () => {
    const { rerender } = await mount({ currentTime: 3 });
    await rerender({ currentTime: 10 });
    expect(timeInput().value).toBe('0:10');

    await type('a');
    // Playback carries on while the user writes; the comment must not drift with it.
    await rerender({ currentTime: 25 });
    await type('a note about this moment');
    expect(timeInput().value).toBe('0:10');
    expect(screen.getByText('comments.kindTimedSummary:{"time":"0:10"}')).toBeTruthy();

    await fireEvent.click(addButton());
    await waitFor(() => expect(axiosInstance.post).toHaveBeenCalled());
    expect(postedTimestamp()).toBe(10);
  });

  it('keeps the exact sub-second time it pinned, not the rounded label', async () => {
    await mount({ currentTime: 12.75 });
    await type('precise');
    await fireEvent.focus(timeInput());
    await fireEvent.blur(timeInput());
    await fireEvent.click(addButton());
    await waitFor(() => expect(axiosInstance.post).toHaveBeenCalled());
    expect(postedTimestamp()).toBe(12.75);
  });

  it('posts a null timestamp when Timestamp is turned off, and says it is a general comment', async () => {
    await mount({ currentTime: 42 });
    await fireEvent.click(screen.getByRole('checkbox', { name: 'comments.timestampToggle' }));
    expect(screen.getByText('comments.kindGeneralSummary')).toBeTruthy();
    expect(screen.queryByLabelText('comments.timeFieldLabel')).toBeNull();

    await type('about the whole file');
    await fireEvent.click(addButton());

    await waitFor(() => expect(axiosInstance.post).toHaveBeenCalled());
    expect(postedTimestamp()).toBeNull();
  });

  it('keeps Add enabled and explains an empty comment inline instead of posting', async () => {
    await mount();
    const add = addButton();
    expect(add.disabled).toBe(false);
    expect(screen.queryByText('comments.emptyTextError')).toBeNull();

    await fireEvent.click(add);

    const message = screen.getByText('comments.emptyTextError');
    expect(message.getAttribute('role')).toBe('alert');
    expect(textarea().getAttribute('aria-invalid')).toBe('true');
    expect(textarea().getAttribute('aria-describedby')).toContain(message.id);
    expect(axiosInstance.post).not.toHaveBeenCalled();

    await type('now there is text');
    expect(screen.queryByText('comments.emptyTextError')).toBeNull();
  });

  it('adds with Enter, and Shift+Enter does not', async () => {
    await mount({ currentTime: 5 });
    await type('line one');
    await fireEvent.keyDown(textarea(), { key: 'Enter', shiftKey: true });
    expect(axiosInstance.post).not.toHaveBeenCalled();

    await fireEvent.keyDown(textarea(), { key: 'Enter' });
    await waitFor(() => expect(axiosInstance.post).toHaveBeenCalledTimes(1));
  });

  it('describes the comment kind to assistive technology', async () => {
    await mount({ currentTime: 42 });
    const summary = screen.getByText('comments.kindTimedSummary:{"time":"0:42"}');
    expect(summary.id).not.toBe('');
    expect(textarea().getAttribute('aria-describedby')).toContain(summary.id);
    const toggle = screen.getByRole('checkbox', { name: 'comments.timestampToggle' });
    expect(toggle.getAttribute('aria-describedby')).toBe(summary.id);
    expect(textarea().labels?.[0]?.textContent).toBe('comments.newCommentLabel');
  });

  it('explains the two kinds behind an info button', async () => {
    await mount();
    const info = screen.getByRole('button', { name: 'comments.kindHelpTitle' });
    expect(info.getAttribute('aria-expanded')).toBe('false');
    expect(screen.queryByText('comments.kindGeneralHelp')).toBeNull();

    await fireEvent.click(info);

    expect(info.getAttribute('aria-expanded')).toBe('true');
    expect(screen.getByText('comments.kindTimedHelp')).toBeTruthy();
    expect(screen.getByText('comments.kindGeneralHelp')).toBeTruthy();
  });

  it("shows the server's reason when the comment is refused, and keeps the text", async () => {
    vi.mocked(axiosInstance.post).mockRejectedValue({
      message: 'Request failed with status code 403',
      response: { status: 403, data: { detail: 'You do not have permission to access this file' } },
    });
    await mount({ currentTime: 4 });
    await type('a note');

    await fireEvent.click(addButton());

    await waitFor(() => expect(toastStore.error).toHaveBeenCalledTimes(1));
    expect(toastStore.error).toHaveBeenCalledWith(
      'comments.addFailed:{"error":"You do not have permission to access this file"}'
    );
    expect(textarea().value).toBe('a note');
  });
});

describe('CommentSection — editing the time before posting', () => {
  it('accepts a typed time', async () => {
    await mount({ currentTime: 3, duration: 600 });
    await type('typed time');
    await fireEvent.focus(timeInput());
    await fireEvent.input(timeInput(), { target: { value: '1:05' } });
    await fireEvent.blur(timeInput());
    expect(screen.getByText('comments.kindTimedSummary:{"time":"1:05"}')).toBeTruthy();

    await fireEvent.click(addButton());
    await waitFor(() => expect(axiosInstance.post).toHaveBeenCalled());
    expect(postedTimestamp()).toBe(65);
  });

  it('refuses a malformed time inline and does not post', async () => {
    await mount({ currentTime: 3, duration: 600 });
    await type('bad time');
    await fireEvent.input(timeInput(), { target: { value: 'soon' } });

    await fireEvent.click(addButton());

    const error = screen.getByText('comments.timeInvalidFormat');
    expect(error.getAttribute('role')).toBe('alert');
    expect(timeInput().getAttribute('aria-invalid')).toBe('true');
    expect(timeInput().getAttribute('aria-describedby')).toBe(error.id);
    expect(axiosInstance.post).not.toHaveBeenCalled();
  });

  it('refuses a time past the end of the media', async () => {
    await mount({ currentTime: 3, duration: 90 });
    await type('too late');
    await fireEvent.input(timeInput(), { target: { value: '2:00' } });

    await fireEvent.click(addButton());

    expect(screen.getByText('comments.timeOutOfRange:{"max":"1:30"}')).toBeTruthy();
    expect(axiosInstance.post).not.toHaveBeenCalled();
  });

  it('nudges the time by one second either way, never below zero or past the end', async () => {
    await mount({ currentTime: 10, duration: 11 });
    await type('nudged');
    const later = screen.getByRole('button', { name: 'comments.timeNudgeForward' });
    const earlier = screen.getByRole('button', { name: 'comments.timeNudgeBack' });

    await fireEvent.click(later);
    expect(timeInput().value).toBe('0:11');
    await fireEvent.click(later);
    expect(timeInput().value).toBe('0:11');
    await fireEvent.click(earlier);
    await fireEvent.click(earlier);
    expect(timeInput().value).toBe('0:09');

    await fireEvent.click(addButton());
    await waitFor(() => expect(axiosInstance.post).toHaveBeenCalled());
    expect(postedTimestamp()).toBe(9);
  });

  it('re-syncs a pinned time to the playhead with "Use current time"', async () => {
    const { rerender } = await mount({ currentTime: 10 });
    await type('resync');
    await rerender({ currentTime: 30 });
    expect(timeInput().value).toBe('0:10');

    await fireEvent.click(screen.getByText('comments.useCurrentTime:{"time":"0:30"}'));
    expect(timeInput().value).toBe('0:30');

    await fireEvent.click(addButton());
    await waitFor(() => expect(axiosInstance.post).toHaveBeenCalled());
    expect(postedTimestamp()).toBe(30);
  });
});

describe('CommentSection — the list', () => {
  const comments = [
    existing('t2', 90, 'later remark', '2026-01-01T00:00:03Z'),
    // At 0:00 and listed before the general one: a sort that reads null as 0 keeps it first.
    existing('t0', 0, 'at the very start', '2026-01-01T00:00:00Z'),
    existing('g1', null, 'about the whole file', '2026-01-01T00:00:02Z'),
    existing('t1', 15, 'early remark', '2026-01-01T00:00:01Z'),
  ];

  beforeEach(() => {
    vi.mocked(axiosInstance.get).mockResolvedValue({ data: comments } as never);
  });

  function item(text: string): HTMLElement {
    return screen.getByText(text).closest('.comment-item') as HTMLElement;
  }

  it('shows a General label for a general comment and a time chip for a timed one', async () => {
    await mount();
    await waitFor(() => expect(screen.getByText('about the whole file')).toBeTruthy());

    expect(within(item('about the whole file')).getByText('comments.generalLabel')).toBeTruthy();
    expect(
      within(item('about the whole file')).queryByRole('button', { name: /jumpToTimestamp/ })
    ).toBeNull();
    expect(
      within(item('early remark'))
        .getByRole('button', { name: /jumpToTimestamp/ })
        .textContent?.trim()
    ).toBe('0:15');
    expect(within(item('early remark')).queryByText('comments.generalLabel')).toBeNull();
  });

  it('lists general comments first, then timed ones in playback order', async () => {
    const { container } = await mount();
    await waitFor(() => expect(screen.getByText('about the whole file')).toBeTruthy());

    const order = [...container.querySelectorAll('.comment-item .comment-body')].map(
      (el) => el.textContent?.trim()
    );
    expect(order).toEqual([
      'about the whole file',
      'at the very start',
      'early remark',
      'later remark',
    ]);
  });

  it('seeks the player when a time chip is clicked', async () => {
    const seeks: unknown[] = [];
    await mount({}, { seekTo: (event) => seeks.push(event.detail) });
    await waitFor(() => expect(screen.getByText('early remark')).toBeTruthy());

    await fireEvent.click(
      within(item('later remark')).getByRole('button', { name: /jumpToTimestamp/ })
    );

    expect(seeks).toEqual([{ time: 90 }]);
  });

  it('keeps the kind and the time when only the text is edited', async () => {
    await mount({ currentTime: 300 });
    await waitFor(() => expect(screen.getByText('early remark')).toBeTruthy());
    await fireEvent.click(within(item('early remark')).getByText('comments.edit'));

    const editor = screen.getByLabelText('comments.editHint') as HTMLTextAreaElement;
    await fireEvent.input(editor, { target: { value: 'early remark, revised' } });
    await fireEvent.click(screen.getByText('comments.save'));

    await waitFor(() => expect(axiosInstance.put).toHaveBeenCalledTimes(1));
    expect(axiosInstance.put).toHaveBeenCalledWith('/comments/t1', {
      text: 'early remark, revised',
      timestamp: 15,
    });
  });

  it('turns a timed comment into a general one when Timestamp is unticked in the editor', async () => {
    await mount();
    await waitFor(() => expect(screen.getByText('early remark')).toBeTruthy());
    await fireEvent.click(within(item('early remark')).getByText('comments.edit'));

    const editing = screen
      .getByLabelText('comments.editHint')
      .closest('.comment-item') as HTMLElement;
    const toggle = within(editing).getByRole('checkbox', { name: 'comments.timestampToggle' });
    expect((toggle as HTMLInputElement).checked).toBe(true);
    await fireEvent.click(toggle);
    expect(within(editing).getByText('comments.kindGeneralSummary')).toBeTruthy();
    await fireEvent.click(within(editing).getByText('comments.save'));

    await waitFor(() => expect(axiosInstance.put).toHaveBeenCalledTimes(1));
    expect(axiosInstance.put).toHaveBeenCalledWith('/comments/t1', {
      text: 'early remark',
      timestamp: null,
    });
    await waitFor(() =>
      expect(within(item('early remark')).getByText('comments.generalLabel')).toBeTruthy()
    );
  });

  it('gives a general comment the playback time when Timestamp is ticked, and lets it be changed', async () => {
    await mount({ currentTime: 47, duration: 600 });
    await waitFor(() => expect(screen.getByText('about the whole file')).toBeTruthy());
    await fireEvent.click(within(item('about the whole file')).getByText('comments.edit'));

    const editing = screen
      .getByLabelText('comments.editHint')
      .closest('.comment-item') as HTMLElement;
    const toggle = within(editing).getByRole('checkbox', { name: 'comments.timestampToggle' });
    expect((toggle as HTMLInputElement).checked).toBe(false);
    await fireEvent.click(toggle);
    const time = within(editing).getByLabelText('comments.timeFieldLabel') as HTMLInputElement;
    expect(time.value).toBe('0:47');
    await fireEvent.click(
      within(editing).getByRole('button', { name: 'comments.timeNudgeForward' })
    );
    await fireEvent.click(within(editing).getByText('comments.save'));

    await waitFor(() => expect(axiosInstance.put).toHaveBeenCalledTimes(1));
    expect(axiosInstance.put).toHaveBeenCalledWith('/comments/g1', {
      text: 'about the whole file',
      timestamp: 48,
    });
  });

  it('refuses an edited time that is not a time, without saving', async () => {
    await mount({ duration: 600 });
    await waitFor(() => expect(screen.getByText('early remark')).toBeTruthy());
    await fireEvent.click(within(item('early remark')).getByText('comments.edit'));

    const editing = screen
      .getByLabelText('comments.editHint')
      .closest('.comment-item') as HTMLElement;
    await fireEvent.input(within(editing).getByLabelText('comments.timeFieldLabel'), {
      target: { value: '1:75' },
    });
    await fireEvent.click(within(editing).getByText('comments.save'));

    expect(within(editing).getByText('comments.timeInvalidFormat')).toBeTruthy();
    expect(axiosInstance.put).not.toHaveBeenCalled();
  });
});
