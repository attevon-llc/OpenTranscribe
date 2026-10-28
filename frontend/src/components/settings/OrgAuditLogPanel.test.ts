/**
 * The organization audit panel (#1277): what it asks the server for, what it shows, and which
 * event types it offers. The server owns scope and names; the panel must send the picked member
 * by uuid, never invent a label, and offer the content-access event types only when the
 * deployment can have them.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/svelte';
import { writable } from 'svelte/store';

vi.mock('$stores/locale', async () => {
  const { readable } = await import('svelte/store');
  const en = (await import('$lib/i18n/locales/en.json')).default as Record<string, string>;
  const t = (key: string, vars?: Record<string, unknown>) =>
    Object.entries(vars ?? {}).reduce(
      (text, [name, value]) => text.replace(`{{${name}}}`, String(value)),
      en[key] ?? key
    );
  return { t: readable(t), locale: readable('en') };
});

const caps = vi.hoisted(() => ({ state: null as unknown }));
vi.mock('$stores/capabilities', async () => {
  const { writable: w } = await import('svelte/store');
  caps.state = w({ loaded: true, enabled: {}, audience: {} });
  return {
    capabilities: caps.state,
    isCapabilityEnabled: (state: { enabled: Record<string, boolean> }, key: string) =>
      state.enabled[key] === true,
  };
});

const api = vi.hoisted(() => ({ listAuditLogs: vi.fn(), exportAuditLogs: vi.fn() }));
vi.mock('$lib/api/orgAdmin', () => ({ OrgAdminApi: api }));
vi.mock('$lib/api/groups', () => ({ GroupsApi: { searchUsers: vi.fn().mockResolvedValue([]) } }));

const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }));
vi.mock('$stores/toast', () => ({ toastStore: toast }));

import OrgAuditLogPanel from './OrgAuditLogPanel.svelte';

const RESOURCE = '11111111-1111-1111-1111-111111111111';

const event = (over: Record<string, unknown> = {}) => ({
  id: 1,
  timestamp: '2026-10-25T12:00:00Z',
  event_type: 'content.file.view',
  user_id: 4,
  username: 'bob@example.com',
  outcome: 'success',
  source_ip: '10.0.0.4',
  user_agent: 'x',
  details: {},
  actor_display: 'Bob Builder',
  resource_type: 'media_file',
  resource_uuid: RESOURCE,
  resource_display: 'Retail review',
  ...over,
});

function setContentAudit(on: boolean) {
  (caps.state as ReturnType<typeof writable>).set({
    loaded: true,
    enabled: { 'audit.content_access': on },
    audience: {},
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  setContentAudit(true);
  api.listAuditLogs.mockResolvedValue({ logs: [event()], total: 1 });
});

describe('OrgAuditLogPanel', () => {
  it('loads the first page and shows the resolved names, not the stored ids', async () => {
    render(OrgAuditLogPanel);

    expect(await screen.findByText('Bob Builder')).toBeInTheDocument();
    expect(screen.getByText('Retail review')).toBeInTheDocument();
    expect(
      screen.getByText('Opened a recording', { selector: '.event-label' })
    ).toBeInTheDocument();
    expect(api.listAuditLogs).toHaveBeenCalledWith(
      expect.objectContaining({ limit: 50, offset: 0 })
    );
  });

  it('shows a short id when the server could not resolve a title', async () => {
    api.listAuditLogs.mockResolvedValue({
      logs: [event({ resource_display: null, actor_display: null })],
      total: 1,
    });
    render(OrgAuditLogPanel);

    expect(await screen.findByText(RESOURCE.slice(0, 8))).toBeInTheDocument();
    expect(screen.getByText('bob@example.com')).toBeInTheDocument();
  });

  it('shows an event this build has no string for as its raw identifier', async () => {
    api.listAuditLogs.mockResolvedValue({
      logs: [event({ event_type: 'something.new.happened' })],
      total: 1,
    });
    render(OrgAuditLogPanel);

    expect(await screen.findByText('something.new.happened')).toBeInTheDocument();
  });

  it('applies the resource filter to the query', async () => {
    render(OrgAuditLogPanel);
    await screen.findByText('Bob Builder');

    const input = screen.getByPlaceholderText('Paste an ID to see its history');
    await fireEvent.input(input, { target: { value: RESOURCE } });
    await fireEvent.click(screen.getByRole('button', { name: 'Apply' }));

    await waitFor(() =>
      expect(api.listAuditLogs).toHaveBeenLastCalledWith(
        expect.objectContaining({ resource_uuid: RESOURCE, offset: 0 })
      )
    );
  });

  it('refuses to apply a malformed resource id and says so', async () => {
    render(OrgAuditLogPanel);
    await screen.findByText('Bob Builder');
    const calls = api.listAuditLogs.mock.calls.length;

    const input = screen.getByPlaceholderText('Paste an ID to see its history');
    await fireEvent.input(input, { target: { value: 'not-a-uuid' } });

    expect(input).toHaveAttribute('aria-invalid', 'true');
    expect(screen.getByRole('button', { name: 'Apply' })).toBeDisabled();
    expect(api.listAuditLogs.mock.calls.length).toBe(calls);
  });

  it('pre-fills the resource filter when opened from a file', async () => {
    render(OrgAuditLogPanel, { props: { resourceUuid: RESOURCE } });
    await screen.findByText('Bob Builder');

    expect(api.listAuditLogs).toHaveBeenCalledWith(
      expect.objectContaining({ resource_uuid: RESOURCE })
    );
  });

  it('offers the content-access event types only while the deployment can record them', async () => {
    const { unmount } = render(OrgAuditLogPanel);
    await screen.findByText('Bob Builder');
    expect(screen.getByRole('option', { name: 'Opened a recording' })).toBeInTheDocument();
    expect(
      screen.getByRole('option', { name: 'Changed what is in a collection' })
    ).toBeInTheDocument();
    unmount();

    setContentAudit(false);
    render(OrgAuditLogPanel);
    await screen.findByText('Bob Builder');
    // The control: the other types are still offered, so this is not an empty list.
    expect(
      screen.getByRole('option', { name: 'Changed what is in a collection' })
    ).toBeInTheDocument();
    expect(screen.queryByRole('option', { name: 'Opened a recording' })).toBeNull();
  });

  it('offers the privacy policy and purge events, whether or not content access is audited', async () => {
    setContentAudit(false);
    render(OrgAuditLogPanel);
    await screen.findByText('Bob Builder');

    for (const name of [
      'Changed the privacy policy',
      'Deleted stored voiceprints',
      'Deleted inferred gender data',
    ]) {
      expect(screen.getByRole('option', { name })).toBeInTheDocument();
    }
  });

  it('exports with the active filters and confirms', async () => {
    api.exportAuditLogs.mockResolvedValue(new Blob(['x']));
    URL.createObjectURL = vi.fn(() => 'blob:x');
    URL.revokeObjectURL = vi.fn();
    render(OrgAuditLogPanel);
    await screen.findByText('Bob Builder');

    await fireEvent.click(screen.getByRole('button', { name: 'CSV' }));

    await waitFor(() =>
      expect(api.exportAuditLogs).toHaveBeenCalledWith('csv', expect.any(Object))
    );
    expect(toast.success).toHaveBeenCalled();
  });

  it('hides the export buttons when the deployment answers 404', async () => {
    api.exportAuditLogs.mockRejectedValue({ response: { status: 404 } });
    render(OrgAuditLogPanel);
    await screen.findByText('Bob Builder');

    await fireEvent.click(screen.getByRole('button', { name: 'JSON' }));

    await waitFor(() => expect(screen.queryByRole('button', { name: 'CSV' })).toBeNull());
    expect(toast.error).toHaveBeenCalled();
  });
});
