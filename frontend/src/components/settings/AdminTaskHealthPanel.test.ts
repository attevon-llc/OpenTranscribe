import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, waitFor } from '@testing-library/svelte';

vi.mock('$stores/locale', () => {
  const t = (key: string) => key;
  return { t: { subscribe: (run: (v: typeof t) => void) => (run(t), () => {}) } };
});
const axiosMock = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock('$lib/axios', () => ({ default: axiosMock }));
vi.mock('$lib/api/adminSettings', () => ({
  AdminSettingsApi: {
    getRetryConfig: vi.fn().mockResolvedValue({ retry_limit_enabled: true, max_retries: 3 }),
    updateRetryConfig: vi.fn(),
  },
}));

import AdminTaskHealthPanel, { type ConfirmRequest } from './AdminTaskHealthPanel.svelte';

// The shape GET /tasks/system/health really returns: each list is { count, items }.
const HEALTH = {
  stuck_tasks: {
    count: 2,
    items: [
      {
        id: 11,
        task_type: 'transcription',
        status: 'in_progress',
        created_at: '2026-10-01T10:00:00Z',
      },
      { id: 12, task_type: 'summary', status: 'pending', created_at: '2026-10-01T11:00:00Z' },
    ],
  },
  inconsistent_files: {
    count: 1,
    items: [{ uuid: 'f-1', filename: 'meeting.mp4', status: 'processing' }],
  },
};

const ACTIONS = [
  {
    id: 'stuck',
    title: 'settings.taskHealth.recoverStuck',
    confirm: 'settings.taskHealth.confirmRecoverStuck',
    url: '/tasks/recover-stuck-tasks',
  },
  {
    id: 'inconsistent',
    title: 'settings.taskHealth.fixInconsistent',
    confirm: 'settings.taskHealth.confirmFixInconsistent',
    url: '/tasks/fix-inconsistent-files',
  },
  {
    id: 'startup',
    title: 'settings.taskHealth.startupRecovery',
    confirm: 'settings.taskHealth.confirmStartupRecovery',
    url: '/tasks/system/startup-recovery',
  },
  {
    id: 'all-users',
    title: 'settings.taskHealth.recoverAllUsers',
    confirm: 'settings.taskHealth.confirmRecoverAllUsers',
    url: '/tasks/system/recover-all-user-files',
  },
];

async function renderLoaded(health: unknown = HEALTH) {
  axiosMock.get.mockResolvedValue({ data: health });
  axiosMock.post.mockResolvedValue({ data: {} });
  const view = render(AdminTaskHealthPanel);
  await waitFor(() =>
    expect(view.container.querySelector('[data-testid="recovery-action-stuck"]')).not.toBeNull()
  );
  return view;
}

const row = (c: HTMLElement, id: string) =>
  c.querySelector(`[data-testid="recovery-action-${id}"]`) as HTMLElement;

beforeEach(() => {
  vi.clearAllMocks();
});

describe('Recovery tab: system recovery actions', () => {
  it('renders the four actions as standard .btn buttons, never btn-warning or btn-primary fills', async () => {
    const { container } = await renderLoaded();
    for (const a of ACTIONS) {
      const button = row(container, a.id).querySelector('button') as HTMLButtonElement;
      expect(button.classList.contains('btn')).toBe(true);
      expect(button.classList.contains('btn-secondary')).toBe(true);
    }
    expect(container.querySelector('.btn-warning')).toBeNull();
    expect(container.querySelector('.action-row .btn-primary')).toBeNull();
  });

  it('gives every action a bold title and a help line tied to its button', async () => {
    const { container } = await renderLoaded();
    const helpKeys: Record<string, string> = {
      stuck: 'settings.taskHealth.recoverStuckHelp',
      inconsistent: 'settings.taskHealth.fixInconsistentHelp',
      startup: 'settings.taskHealth.startupRecoveryHelp',
      'all-users': 'settings.taskHealth.recoverAllUsersHelp',
    };
    for (const a of ACTIONS) {
      const r = row(container, a.id);
      expect(r.querySelector('.action-title')?.textContent).toBe(a.title);
      const help = r.querySelector('.action-help') as HTMLElement;
      expect(help.textContent).toBe(helpKeys[a.id]);
      expect(r.querySelector('button')?.getAttribute('aria-describedby')).toBe(help.id);
    }
  });

  it('keeps the stuck-task and inconsistent-file counts on their buttons', async () => {
    const { container } = await renderLoaded();
    expect(row(container, 'stuck').querySelector('button')?.textContent?.trim()).toBe(
      'settings.taskHealth.recoverStuckButton (2)'
    );
    expect(row(container, 'inconsistent').querySelector('button')?.textContent?.trim()).toBe(
      'settings.taskHealth.fixInconsistentButton (1)'
    );
    expect(row(container, 'startup').querySelector('button')?.textContent).not.toMatch(/\(\d+\)/);
  });

  it.each(ACTIONS)(
    '$id asks for confirmation with its title and message, then posts',
    async (a) => {
      const requests: ConfirmRequest[] = [];
      axiosMock.get.mockResolvedValue({ data: HEALTH });
      axiosMock.post.mockResolvedValue({ data: {} });
      const view = render(AdminTaskHealthPanel, {
        events: { requestConfirm: (e: CustomEvent<ConfirmRequest>) => requests.push(e.detail) },
      } as never);
      await waitFor(() => expect(row(view.container, a.id)).not.toBeNull());

      await fireEvent.click(row(view.container, a.id).querySelector('button') as HTMLButtonElement);

      expect(requests).toHaveLength(1);
      expect(requests[0].title).toBe(a.title);
      expect(requests[0].message).toBe(a.confirm);
      // Nothing runs until the dialog's callback is invoked.
      expect(axiosMock.post).not.toHaveBeenCalled();
      requests[0].callback();
      await waitFor(() => expect(axiosMock.post).toHaveBeenCalledWith(a.url));
    }
  );
});

describe('Recovery tab: lists and page consistency', () => {
  it('lists stuck tasks and inconsistent files with standard small retry buttons', async () => {
    const { container } = await renderLoaded();
    const retry = Array.from(container.querySelectorAll('table button'));
    expect(retry).toHaveLength(3);
    for (const b of retry) {
      expect(b.classList.contains('btn')).toBe(true);
      expect(b.classList.contains('btn-secondary')).toBe(true);
      expect(b.classList.contains('btn-small')).toBe(true);
    }
    expect(container.textContent).toContain('meeting.mp4');

    await fireEvent.click(retry[0]);
    await waitFor(() =>
      expect(axiosMock.post).toHaveBeenCalledWith('/tasks/system/recover-task/11')
    );
    await fireEvent.click(retry[2]);
    await waitFor(() => expect(axiosMock.post).toHaveBeenCalledWith('/tasks/retry/f-1'));
  });

  it('draws the retries card and the actions card with the same card component', async () => {
    const { container } = await renderLoaded();
    const cards = container.querySelectorAll('.tx-form .settings-section');
    // retries + actions + stuck tasks + inconsistent files
    expect(cards).toHaveLength(4);
    for (const card of cards) {
      expect(card.querySelector('h3.section-title')).not.toBeNull();
    }
  });

  it('hides the lists and shows zero counts when nothing is stuck', async () => {
    const { container } = await renderLoaded({
      stuck_tasks: { count: 0, items: [] },
      inconsistent_files: { count: 0, items: [] },
    });
    expect(container.querySelector('table')).toBeNull();
    expect(row(container, 'stuck').querySelector('button')?.textContent).toContain('(0)');
  });
});
