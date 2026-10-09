import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/svelte';

const h = vi.hoisted(() => ({
  breakGlass: vi.fn(),
  searchOrganizations: vi.fn(),
}));

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string, o?: Record<string, unknown>) => string) => void) => (
      run((k, o) => (o ? `${k} ${JSON.stringify(o)}` : k)), () => {}
    ),
  },
  locale: { subscribe: (run: (value: string) => void) => (run('en'), () => {}) },
}));
vi.mock('$lib/api/supportAccess', () => ({
  SupportAccessApi: {
    breakGlass: h.breakGlass,
    searchOrganizations: h.searchOrganizations,
  },
}));
vi.mock('$lib/api/admin', () => ({ AdminApi: { searchUsers: vi.fn() } }));

import BreakGlassModal from './BreakGlassModal.svelte';

const REASON = 'Production outage affecting this customer';

function open(extra: Record<string, unknown> = {}) {
  return render(BreakGlassModal, { props: { isOpen: true, ...extra } });
}

const reviewBtn = () =>
  screen.getByRole('button', { name: 'supportAccess.breakGlass.review' }) as HTMLButtonElement;
const confirmBtn = () =>
  screen.getByRole('button', { name: 'supportAccess.breakGlass.confirm' }) as HTMLButtonElement;

async function fillStepOne(ticket = 'INC-42') {
  await fireEvent.input(
    screen.getByPlaceholderText(/supportAccess.request.search(Organization|User)/),
    { target: { value: 'ac' } }
  );
  await fireEvent.click(await screen.findByRole('option', { name: 'Acme' }));
  await fireEvent.input(screen.getByLabelText('supportAccess.request.reason'), {
    target: { value: REASON },
  });
  if (ticket) {
    await fireEvent.input(screen.getByLabelText('supportAccess.breakGlass.ticket'), {
      target: { value: ticket },
    });
  }
}

beforeEach(() => {
  vi.clearAllMocks();
  h.searchOrganizations.mockResolvedValue([{ uuid: 'org-1', name: 'Acme', slug: 'acme' }]);
  h.breakGlass.mockResolvedValue({ uuid: 'g1', status: 'active' });
});

describe('BreakGlassModal', () => {
  it('caps the duration at 4 hours: 240 is offered, 480 is not', () => {
    open();
    const values = Array.from(
      (screen.getByLabelText('supportAccess.request.duration') as HTMLSelectElement).options
    ).map((o) => o.value);
    expect(values).toContain('240');
    expect(values).not.toContain('480');
  });

  it('will not move to review without a ticket reference', async () => {
    open();
    await fillStepOne('');
    expect(reviewBtn().disabled).toBe(true);
    await fireEvent.input(screen.getByLabelText('supportAccess.breakGlass.ticket'), {
      target: { value: 'INC-42' },
    });
    expect(reviewBtn().disabled).toBe(false);
  });

  it('step 2 summarises the target, level and duration', async () => {
    open();
    await fillStepOne();
    await fireEvent.click(reviewBtn());
    const summary = await screen.findByText(/supportAccess\.breakGlass\.confirmSummary/);
    expect(summary.textContent).toContain('"target":"Acme"');
    expect(summary.textContent).toContain('supportAccess.level.read');
  });

  it('keeps the confirm button disabled until the exact name is typed (case matters)', async () => {
    open();
    await fillStepOne();
    await fireEvent.click(reviewBtn());
    const typed = await screen.findByLabelText(/supportAccess\.breakGlass\.confirmTypeName/);

    expect(confirmBtn().disabled).toBe(true);
    await fireEvent.input(typed, { target: { value: 'acme' } });
    expect(confirmBtn().disabled).toBe(true);
    await fireEvent.input(typed, { target: { value: 'Acme ' } });
    expect(confirmBtn().disabled).toBe(true);
    await fireEvent.input(typed, { target: { value: 'Acme' } });
    expect(confirmBtn().disabled).toBe(false);
    expect(confirmBtn().getAttribute('aria-describedby')).toBe('bg-typed-label');
  });

  it('Back returns to step 1 with every value intact', async () => {
    open();
    await fillStepOne('INC-77');
    await fireEvent.click(reviewBtn());
    await fireEvent.click(
      await screen.findByRole('button', { name: 'supportAccess.breakGlass.back' })
    );

    expect(
      (screen.getByLabelText('supportAccess.breakGlass.ticket') as HTMLInputElement).value
    ).toBe('INC-77');
    expect(
      (screen.getByLabelText('supportAccess.request.reason') as HTMLTextAreaElement).value
    ).toBe(REASON);
    expect(screen.getByText('Acme')).toBeTruthy();
  });

  it('posts to the break-glass endpoint with the ticket, then offers to start the session', async () => {
    const started = vi.fn();
    const created = vi.fn();
    render(BreakGlassModal, {
      props: { isOpen: true },
      events: {
        start: (e: CustomEvent) => started(e.detail),
        created: (e: CustomEvent) => created(e.detail),
      },
    });
    await fillStepOne('INC-42');
    await fireEvent.click(reviewBtn());
    await fireEvent.input(await screen.findByLabelText(/confirmTypeName/), {
      target: { value: 'Acme' },
    });
    await fireEvent.click(confirmBtn());

    await waitFor(() => expect(h.breakGlass).toHaveBeenCalledTimes(1));
    expect(h.breakGlass).toHaveBeenCalledWith({
      organization_uuid: 'org-1',
      subject_user_uuid: null,
      access_level: 'read',
      reason: REASON,
      ticket_ref: 'INC-42',
      duration_minutes: 60,
    });
    expect(created).toHaveBeenCalledWith({ uuid: 'g1', status: 'active' });

    await fireEvent.click(
      await screen.findByRole('button', { name: 'supportAccess.breakGlass.startNow' })
    );
    expect(started).toHaveBeenCalledWith({ uuid: 'g1', status: 'active' });
  });

  it('shows the server failure on step 2 and does not report success', async () => {
    h.breakGlass.mockRejectedValue({
      response: { status: 403, data: { detail: 'super_admin required' } },
    });
    open();
    await fillStepOne();
    await fireEvent.click(reviewBtn());
    await fireEvent.input(await screen.findByLabelText(/confirmTypeName/), {
      target: { value: 'Acme' },
    });
    await fireEvent.click(confirmBtn());

    expect((await screen.findByRole('alert')).textContent).toBe('super_admin required');
    expect(screen.queryByRole('button', { name: 'supportAccess.breakGlass.startNow' })).toBeNull();
  });
});
