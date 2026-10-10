import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/svelte';

const h = vi.hoisted(() => ({
  requestGrant: vi.fn(),
  searchOrganizations: vi.fn(),
  searchUsers: vi.fn(),
}));

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string, o?: Record<string, unknown>) => string) => void) => (
      run((k) => k), () => {}
    ),
  },
  locale: { subscribe: (run: (value: string) => void) => (run('en'), () => {}) },
}));
vi.mock('$stores/toast', () => ({ toastStore: { success: vi.fn(), error: vi.fn() } }));
vi.mock('$lib/api/supportAccess', () => ({
  SupportAccessApi: { requestGrant: h.requestGrant, searchOrganizations: h.searchOrganizations },
}));
vi.mock('$lib/api/admin', () => ({ AdminApi: { searchUsers: h.searchUsers } }));

import RequestAccessModal from './RequestAccessModal.svelte';

const REASON = 'Customer reports a stuck transcript';

function open() {
  return render(RequestAccessModal, { props: { isOpen: true } });
}

const submit = () =>
  screen.getByRole('button', { name: 'supportAccess.request.submit' }) as HTMLButtonElement;

async function pick(query: string, label: string) {
  const input = screen.getByPlaceholderText(/supportAccess.request.search(Organization|User)/);
  await fireEvent.input(input, { target: { value: query } });
  const option = await screen.findByRole('option', { name: label });
  await fireEvent.click(option);
}

beforeEach(() => {
  vi.clearAllMocks();
  h.searchOrganizations.mockResolvedValue([{ uuid: 'org-1', name: 'Acme', slug: 'acme' }]);
  h.searchUsers.mockResolvedValue({
    total: 1,
    users: [{ uuid: 'user-1', email: 'jane@example.com', full_name: 'Jane Doe' }],
  });
  h.requestGrant.mockResolvedValue({ uuid: 'g1', status: 'pending' });
});

describe('RequestAccessModal', () => {
  it('cannot submit until a target is picked AND the reason has 10 characters', async () => {
    open();
    expect(submit().disabled).toBe(true);

    await fireEvent.input(screen.getByLabelText('supportAccess.request.reason'), {
      target: { value: REASON },
    });
    expect(submit().disabled).toBe(true); // reason alone is not enough

    await pick('ac', 'Acme');
    expect(submit().disabled).toBe(false);

    await fireEvent.input(screen.getByLabelText('supportAccess.request.reason'), {
      target: { value: 'too short' },
    });
    expect(submit().disabled).toBe(true); // target alone is not enough
  });

  it('posts the organization uuid and a null user uuid, with the default level and duration', async () => {
    open();
    await pick('ac', 'Acme');
    await fireEvent.input(screen.getByLabelText('supportAccess.request.reason'), {
      target: { value: `  ${REASON}  ` },
    });
    await fireEvent.click(submit());

    await waitFor(() => expect(h.requestGrant).toHaveBeenCalledTimes(1));
    expect(h.requestGrant).toHaveBeenCalledWith({
      organization_uuid: 'org-1',
      subject_user_uuid: null,
      access_level: 'read',
      reason: REASON,
      duration_minutes: 60,
    });
  });

  it('switching to a user target nulls the organization and posts only the user uuid', async () => {
    open();
    await pick('ac', 'Acme');
    await fireEvent.click(screen.getByLabelText('supportAccess.request.targetUser'));
    // Switching kind drops the previous pick: nothing is submittable until a user is chosen.
    expect(screen.queryByText('Acme')).toBeNull();
    await fireEvent.input(screen.getByLabelText('supportAccess.request.reason'), {
      target: { value: REASON },
    });
    expect(submit().disabled).toBe(true);

    await pick('jane', 'Jane Doe (jane@example.com)');
    await fireEvent.click(submit());

    await waitFor(() => expect(h.requestGrant).toHaveBeenCalledTimes(1));
    expect(h.requestGrant).toHaveBeenCalledWith(
      expect.objectContaining({ organization_uuid: null, subject_user_uuid: 'user-1' })
    );
  });

  it('renders the server 422 detail instead of a generic failure', async () => {
    h.requestGrant.mockRejectedValue({
      response: {
        status: 422,
        data: {
          detail: { code: 'support_grant_invalid_target', message: 'Cannot target yourself.' },
        },
      },
    });
    open();
    await pick('ac', 'Acme');
    await fireEvent.input(screen.getByLabelText('supportAccess.request.reason'), {
      target: { value: REASON },
    });
    await fireEvent.click(submit());

    expect((await screen.findByRole('alert')).textContent).toBe('Cannot target yourself.');
  });

  it('offers the full duration range up to 8 hours', () => {
    open();
    const values = Array.from(
      (screen.getByLabelText('supportAccess.request.duration') as HTMLSelectElement).options
    ).map((o) => o.value);
    expect(values).toEqual(['15', '30', '60', '120', '240', '480']);
  });
});
