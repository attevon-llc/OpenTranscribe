import { describe, it, expect, vi } from 'vitest';
import { render, fireEvent, waitFor } from '@testing-library/svelte';

vi.mock('$stores/locale', async () => (await import('$testMocks/localeMocks')).keyLocale());
vi.mock('$stores/toast', async () => (await import('$testMocks/serviceMocks')).toastStoreMock());

const axios = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock('$lib/axios', () => ({ default: axios, isRequestCancelled: () => false }));

import UserFileStatus from './UserFileStatus.svelte';

const summary = {
  data: {
    status_counts: { total: 0, completed: 0, processing: 0, pending: 0, error: 0 },
    problem_files: { count: 0 },
    has_problems: false,
  },
};
const emptyPage = { data: { items: [], total_pages: 1 } };

describe('UserFileStatus filter changed while the first task load is in flight', () => {
  it('requests the tasks again with the new filter once the first load returns', async () => {
    let releaseFirst: (value: typeof emptyPage) => void = () => {};
    const firstTasks = new Promise<typeof emptyPage>((resolve) => (releaseFirst = resolve));
    let taskCalls = 0;
    axios.get.mockImplementation((url: string) => {
      if (!url.startsWith('/tasks')) return Promise.resolve(summary);
      taskCalls += 1;
      return taskCalls === 1 ? firstTasks : Promise.resolve(emptyPage);
    });

    const { getByLabelText } = render(UserFileStatus);
    await waitFor(() => expect(taskCalls).toBe(1));

    await fireEvent.change(getByLabelText('fileStatus.allAges'), { target: { value: 'week' } });
    releaseFirst(emptyPage);

    await waitFor(() => expect(taskCalls).toBe(2));
    const urls = axios.get.mock.calls
      .map((c) => String(c[0]))
      .filter((u) => u.startsWith('/tasks'));
    expect(urls[1]).toContain('age_filter=week');
  });
});
