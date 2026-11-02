import { describe, it, expect, vi, beforeEach } from 'vitest';

const loginExt = vi.hoisted(() => vi.fn());
vi.mock('$stores/auth', () => ({ loginWithExternalAuth: loginExt }));
const cloud = vi.hoisted(() => ({
  loadExternalAuth: vi.fn(),
  hasExternalSession: vi.fn(),
  onAuthChange: vi.fn(),
  mountSignIn: vi.fn(),
}));
vi.mock('$lib/cloud', () => cloud);

import { createExternalSignIn } from './externalSignIn';

function hooks() {
  return {
    getNode: vi.fn(() => document.createElement('div')),
    isSignedIn: vi.fn(() => false),
    onSettled: vi.fn(),
    onSuccess: vi.fn(),
    onFailure: vi.fn(),
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  cloud.onAuthChange.mockResolvedValue(vi.fn());
  cloud.mountSignIn.mockResolvedValue(vi.fn());
});

describe('createExternalSignIn', () => {
  it('fails (settled + onFailure) when the SDK cannot load', async () => {
    cloud.loadExternalAuth.mockResolvedValue(null);
    const h = hooks();
    await createExternalSignIn(h).setup();
    expect(h.onSettled).toHaveBeenCalled();
    expect(h.onFailure).toHaveBeenCalledWith();
  });

  it('an existing session is hydrated straight away, without mounting the widget', async () => {
    cloud.loadExternalAuth.mockResolvedValue({});
    cloud.hasExternalSession.mockResolvedValue(true);
    loginExt.mockResolvedValue({ success: true });
    const h = hooks();
    await createExternalSignIn(h).setup();
    expect(h.onSuccess).toHaveBeenCalled();
    expect(cloud.mountSignIn).not.toHaveBeenCalled();
    expect(h.onSettled).not.toHaveBeenCalled();
  });

  it('mounts the widget when anonymous and tears it down on destroy', async () => {
    cloud.loadExternalAuth.mockResolvedValue({});
    cloud.hasExternalSession.mockResolvedValue(false);
    const unmount = vi.fn();
    const unlisten = vi.fn();
    cloud.mountSignIn.mockResolvedValue(unmount);
    cloud.onAuthChange.mockResolvedValue(unlisten);
    const h = hooks();
    const s = createExternalSignIn(h);
    await s.setup();
    expect(cloud.mountSignIn).toHaveBeenCalled();
    expect(h.onSettled).toHaveBeenCalled();
    s.destroy();
    expect(unmount).toHaveBeenCalled();
    expect(unlisten).toHaveBeenCalled();
  });

  it('a failed hydration reports the server message', async () => {
    cloud.loadExternalAuth.mockResolvedValue({});
    cloud.hasExternalSession.mockResolvedValue(true);
    loginExt.mockResolvedValue({ success: false, message: 'bad' });
    const h = hooks();
    await createExternalSignIn(h).setup();
    expect(h.onFailure).toHaveBeenCalledWith('bad');
  });
});
