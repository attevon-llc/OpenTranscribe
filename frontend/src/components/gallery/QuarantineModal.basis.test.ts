/**
 * The takedown must say what it acts on (issue #1254): a recorded notice, a new notice
 * recorded first, or an explicit "no notice (operator action)". The server refuses a takedown
 * with neither, so these pin the modal's side: it cannot submit without a basis, records a new
 * notice exactly once before touching any file, and emails owners once per notice.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, fireEvent, waitFor } from '@testing-library/svelte';

vi.mock('$stores/locale', async () => {
  const { readable } = await import('svelte/store');
  const en = (await import('$lib/i18n/locales/en.json')).default as Record<string, string>;
  return {
    t: readable((key: string, opts?: Record<string, unknown>) => {
      let out = en[key] ?? key;
      for (const [name, value] of Object.entries(opts ?? {})) {
        out = out.split(`{{${name}}}`).join(String(value));
      }
      return out;
    }),
  };
});

const api = vi.hoisted(() => ({ AdminApi: { quarantineFile: vi.fn() } }));
const notices = vi.hoisted(() => ({
  TakedownNoticesApi: { list: vi.fn(), create: vi.fn(), notify: vi.fn() },
}));
const toast = vi.hoisted(() => ({
  toastStore: { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() },
}));

vi.mock('$lib/api/admin', () => api);
vi.mock('$lib/api/takedownNotices', () => notices);
vi.mock('$stores/toast', () => toast);

import QuarantineModal from './QuarantineModal.svelte';
import type { MediaFile } from '$lib/types/media';

const file = (uuid: string) =>
  ({
    uuid,
    filename: `${uuid}.mp4`,
    status: 'completed',
    upload_time: '2026-01-01T00:00:00Z',
  }) as MediaFile;

const OPEN_NOTICE = {
  uuid: 'n-open',
  notice_type: 'dmca',
  received_at: '2026-10-01T00:00:00Z',
  requester_reference: 'CASE-9',
  basis: 'b',
  recorded_by_authority: 'platform',
  handled_by_user_uuid: null,
  created_at: '2026-10-01T00:00:00Z',
  status: 'open',
  links: [],
};

function mount(files = [file('f-1')]) {
  const view = render(QuarantineModal, { props: { isOpen: true, files } });
  const submit = () =>
    view.getByText(/Quarantine \d+ file/i).closest('button') as HTMLButtonElement;
  const radio = (value: string) =>
    view.container.querySelector(`input[type="radio"][value="${value}"]`) as HTMLInputElement;
  const reason = async (text = 'Removed after a notice') =>
    fireEvent.input(view.container.querySelector('#quarantine-reason')!, {
      target: { value: text },
    });
  return { ...view, submit, radio, reason };
}

describe('QuarantineModal basis', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    notices.TakedownNoticesApi.list.mockResolvedValue({ notices: [OPEN_NOTICE], total: 1 });
    api.AdminApi.quarantineFile.mockResolvedValue({});
    notices.TakedownNoticesApi.notify.mockResolvedValue({
      sent: 1,
      failed: 0,
      skipped_no_transport: 0,
      already_notified: 0,
    });
  });

  it('cannot be submitted with a reason alone: a basis is required, not defaulted', async () => {
    const m = mount();
    await waitFor(() => expect(notices.TakedownNoticesApi.list).toHaveBeenCalled());
    await m.reason();
    expect(m.submit().disabled).toBe(true);
  });

  it('an existing notice must be selected, then the takedown cites it', async () => {
    const m = mount();
    await waitFor(() => expect(m.container.querySelectorAll('option').length).toBeGreaterThan(1));
    await m.reason();
    expect(m.submit().disabled).toBe(true);

    const select = m.container.querySelector('select') as HTMLSelectElement;
    await fireEvent.change(select, { target: { value: 'n-open' } });
    expect(m.submit().disabled).toBe(false);
    await fireEvent.click(m.submit());

    await waitFor(() =>
      expect(api.AdminApi.quarantineFile).toHaveBeenCalledWith(
        'f-1',
        'Removed after a notice',
        true,
        { noticeUuid: 'n-open' }
      )
    );
    expect(notices.TakedownNoticesApi.create).not.toHaveBeenCalled();
  });

  it('a blank operator-action reason is not a basis', async () => {
    const m = mount();
    await m.reason();
    await fireEvent.click(m.radio('operator'));
    await fireEvent.input(m.container.querySelector('#quarantine-operator-reason')!, {
      target: { value: '   ' },
    });
    expect(m.submit().disabled).toBe(true);
  });

  it('a new notice needs its reference and basis before it can be submitted', async () => {
    const m = mount();
    await m.reason();
    await fireEvent.click(m.radio('new'));
    expect(m.submit().disabled).toBe(true);
    await fireEvent.input(m.container.querySelector('#quarantine-notice-reference')!, {
      target: { value: 'CASE-1' },
    });
    expect(m.submit().disabled).toBe(true);
    await fireEvent.input(m.container.querySelector('#quarantine-notice-basis')!, {
      target: { value: 'complete notice' },
    });
    expect(m.submit().disabled).toBe(false);
  });

  it('records a new notice ONCE, before any file, and cites it for every file', async () => {
    notices.TakedownNoticesApi.create.mockResolvedValue({ uuid: 'n-new' });
    const order: string[] = [];
    notices.TakedownNoticesApi.create.mockImplementation(async () => {
      order.push('create');
      return { uuid: 'n-new' };
    });
    api.AdminApi.quarantineFile.mockImplementation(async () => {
      order.push('quarantine');
      return {};
    });
    const m = mount([file('f-1'), file('f-2')]);
    await m.reason();
    await fireEvent.click(m.radio('new'));
    await fireEvent.input(m.container.querySelector('#quarantine-notice-reference')!, {
      target: { value: 'CASE-1' },
    });
    await fireEvent.input(m.container.querySelector('#quarantine-notice-basis')!, {
      target: { value: 'complete notice' },
    });
    await fireEvent.click(m.submit());

    await waitFor(() => expect(api.AdminApi.quarantineFile).toHaveBeenCalledTimes(2));
    expect(notices.TakedownNoticesApi.create).toHaveBeenCalledTimes(1);
    expect(notices.TakedownNoticesApi.create.mock.calls[0][0]).toMatchObject({
      notice_type: 'dmca',
      requester_reference: 'CASE-1',
      basis: 'complete notice',
    });
    expect(order).toEqual(['create', 'quarantine', 'quarantine']);
    for (const call of api.AdminApi.quarantineFile.mock.calls) {
      expect(call[3]).toEqual({ noticeUuid: 'n-new' });
    }
  });

  it('takes nothing down when the new notice cannot be recorded', async () => {
    notices.TakedownNoticesApi.create.mockRejectedValue(new Error('boom'));
    const m = mount();
    await m.reason();
    await fireEvent.click(m.radio('new'));
    await fireEvent.input(m.container.querySelector('#quarantine-notice-reference')!, {
      target: { value: 'CASE-1' },
    });
    await fireEvent.input(m.container.querySelector('#quarantine-notice-basis')!, {
      target: { value: 'b' },
    });
    await fireEvent.click(m.submit());

    await waitFor(() => expect(toast.toastStore.error).toHaveBeenCalled());
    expect(api.AdminApi.quarantineFile).not.toHaveBeenCalled();
  });

  it('emails the owners once for the whole notice, after the loop, only when asked', async () => {
    const m = mount([file('f-1'), file('f-2'), file('f-3')]);
    await waitFor(() => expect(m.container.querySelectorAll('option').length).toBeGreaterThan(1));
    await m.reason();
    await fireEvent.change(m.container.querySelector('select')!, { target: { value: 'n-open' } });
    const boxes = m.container.querySelectorAll('input[type="checkbox"]');
    await fireEvent.click(boxes[boxes.length - 1]);
    await fireEvent.click(m.submit());

    await waitFor(() => expect(notices.TakedownNoticesApi.notify).toHaveBeenCalled());
    // waitFor passes on the first call; give a second (per-file) call time to show up.
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(notices.TakedownNoticesApi.notify).toHaveBeenCalledTimes(1);
    expect(notices.TakedownNoticesApi.notify).toHaveBeenCalledWith('n-open');
    expect(api.AdminApi.quarantineFile).toHaveBeenCalledTimes(3);
  });

  it('does not email by default, and offers no email for an operator action', async () => {
    const m = mount();
    await waitFor(() => expect(m.container.querySelectorAll('option').length).toBeGreaterThan(1));
    await m.reason();
    await fireEvent.change(m.container.querySelector('select')!, { target: { value: 'n-open' } });
    await fireEvent.click(m.submit());
    await waitFor(() => expect(api.AdminApi.quarantineFile).toHaveBeenCalled());
    expect(notices.TakedownNoticesApi.notify).not.toHaveBeenCalled();

    const again = mount();
    await fireEvent.click(again.radio('operator'));
    expect(again.container.querySelectorAll('input[type="checkbox"]').length).toBe(1);
  });

  it('with no open notice it starts on "record a new notice"', async () => {
    notices.TakedownNoticesApi.list.mockResolvedValue({ notices: [], total: 0 });
    const m = mount();
    await waitFor(() => expect(m.radio('new').checked).toBe(true));
  });
});
