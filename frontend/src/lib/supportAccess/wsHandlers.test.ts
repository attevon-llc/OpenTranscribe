import { describe, it, expect, vi, beforeEach } from 'vitest';

const h = vi.hoisted(() => ({
  end: vi.fn(),
  open: vi.fn(),
  info: vi.fn(),
  warning: vi.fn(),
  session: { grantUuid: null as string | null },
}));

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string, o?: Record<string, unknown>) => string) => void) => (
      run((k, o) => (o ? `${k} ${JSON.stringify(o)}` : k)), () => {}
    ),
  },
  locale: { subscribe: (run: (value: string) => void) => (run('en-GB'), () => {}) },
}));
vi.mock('$stores/toast', () => ({ toastStore: { info: h.info, warning: h.warning } }));
vi.mock('$stores/settingsModalStore', () => ({ settingsModalStore: { open: h.open } }));
vi.mock('$stores/supportSession', async () => {
  const { readable } = await import('svelte/store');
  return {
    supportSession: {
      subscribe: (run: (v: { grantUuid: string | null }) => void) =>
        readable(h.session).subscribe(run),
      end: h.end,
    },
  };
});

import { capabilities } from '$stores/capabilities';
import { handleSupportAccessMessage } from './wsHandlers';
import { SUPPORT_ACCESS_EVENT } from './events';

function setMode(mode: 'multi' | 'single' | undefined) {
  capabilities.update((s) => ({ ...s, tenancyMode: mode }));
}

beforeEach(() => {
  vi.clearAllMocks();
  h.session.grantUuid = null;
  setMode('multi');
});

describe('handleSupportAccessMessage', () => {
  it('re-emits every message as a window event for the open panels', async () => {
    const seen: unknown[] = [];
    const listener = (e: Event) => seen.push((e as CustomEvent).detail);
    window.addEventListener(SUPPORT_ACCESS_EVENT, listener);
    await handleSupportAccessMessage({
      type: 'support_access_decided',
      data: { grant_uuid: 'g', status: 'active' },
    });
    window.removeEventListener(SUPPORT_ACCESS_EVENT, listener);
    expect(seen).toEqual([
      { type: 'support_access_decided', data: { grant_uuid: 'g', status: 'active' } },
    ]);
  });

  it('does nothing at all outside multi-tenant mode (fail closed)', async () => {
    setMode(undefined);
    const seen: unknown[] = [];
    const listener = (e: Event) => seen.push(e);
    window.addEventListener(SUPPORT_ACCESS_EVENT, listener);
    await handleSupportAccessMessage({
      type: 'support_access_requested',
      data: { grant_uuid: 'g', grantee_name: 'Sam' },
    });
    window.removeEventListener(SUPPORT_ACCESS_EVENT, listener);
    expect(seen).toEqual([]);
    expect(h.info).not.toHaveBeenCalled();
  });

  it('announces a new request by name', async () => {
    await handleSupportAccessMessage({
      type: 'support_access_requested',
      data: { grant_uuid: 'g', grantee_name: 'Sam Support' },
    });
    expect(h.info).toHaveBeenCalledWith('supportAccess.notify.requested {"grantee":"Sam Support"}');
  });

  it('raises a PERSISTENT warning with a Review action for break-glass, which opens the requests section', async () => {
    await handleSupportAccessMessage({
      type: 'support_access_break_glass',
      data: {
        grant_uuid: 'g',
        grantee_name: 'Sam',
        target_name: 'Acme',
        expires_at: '2026-10-09T13:00:00Z',
      },
    });
    expect(h.warning).toHaveBeenCalledTimes(1);
    const [message, duration, options] = h.warning.mock.calls[0];
    expect(message).toContain('supportAccess.notify.breakGlass');
    expect(message).toContain('"target":"Acme"');
    expect(duration).toBe(0); // 0 = never auto-dismiss
    expect(options.action.label).toBe('supportAccess.notify.review');
    options.action.onClick();
    expect(h.open).toHaveBeenCalledWith('support-access-requests');
  });

  it('ends the live session when its grant is revoked', async () => {
    h.session.grantUuid = 'g-live';
    await handleSupportAccessMessage({
      type: 'support_access_revoked',
      data: { grant_uuid: 'g-live', status: 'revoked' },
    });
    expect(h.end).toHaveBeenCalledWith('revoked');
  });

  it('leaves the session alone when a DIFFERENT grant is revoked', async () => {
    h.session.grantUuid = 'g-live';
    await handleSupportAccessMessage({
      type: 'support_access_revoked',
      data: { grant_uuid: 'g-other', status: 'revoked' },
    });
    expect(h.end).not.toHaveBeenCalled();
  });

  it('a decision that leaves the grant active does not end the session', async () => {
    h.session.grantUuid = 'g-live';
    await handleSupportAccessMessage({
      type: 'support_access_decided',
      data: { grant_uuid: 'g-live', status: 'active' },
    });
    expect(h.end).not.toHaveBeenCalled();
  });
});
