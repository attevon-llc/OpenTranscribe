/**
 * FedRAMP AC-8: `get_current_active_user` refuses every non-exempt route with
 * `detail.code === "banner_acknowledgment_required"` until consent is recorded
 * server-side. The SPA used to fake that consent with a `sessionStorage` flag
 * the server never saw, which bricked any deployment with the banner enabled.
 *
 * These tests pin the two halves of the real contract: the third lifecycle code
 * is classified (with its `reason`, which drives the copy), and
 * `acknowledgeBanner()` posts to the one endpoint that clears the gate.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { get } from 'svelte/store';

vi.mock('$lib/axios', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn() },
  abortAllRequests: vi.fn(),
}));

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

vi.mock('$lib/session/clearUserState', () => ({ clearUserState: vi.fn() }));
vi.mock('$lib/edition', () => ({ isCloudEdition: false }));

import axiosInstance from '$lib/axios';
import {
  readAccountLifecycle,
  handleAccountLifecycleError,
  clearAccountLifecycle,
  acknowledgeBanner,
  accountLifecycle,
  isAuthenticated,
  authStore,
} from './auth';

const bannerRefusal = (reason?: string) => ({
  response: {
    status: 403,
    data: {
      detail: {
        code: 'banner_acknowledgment_required',
        message: 'You must acknowledge the login banner before continuing.',
        ...(reason === undefined ? {} : { reason }),
      },
    },
  },
});

describe('readAccountLifecycle — banner_acknowledgment_required', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    clearAccountLifecycle();
  });

  it('classifies the banner refusal and keeps the reason', () => {
    expect(readAccountLifecycle(bannerRefusal('never_acknowledged'))).toEqual({
      code: 'banner_acknowledgment_required',
      message: 'You must acknowledge the login banner before continuing.',
      reason: 'never_acknowledged',
    });
  });

  it('carries banner_text_changed through, so the UI can say the notice was updated', () => {
    // This user DID acknowledge — different wording. Presenting it as a plain
    // failure would look like a bug rather than a re-consent request.
    expect(readAccountLifecycle(bannerRefusal('banner_text_changed'))?.reason).toBe(
      'banner_text_changed'
    );
  });

  it('tolerates a missing reason rather than inventing one', () => {
    const state = readAccountLifecycle(bannerRefusal());
    expect(state?.code).toBe('banner_acknowledgment_required');
    expect(state?.reason).toBeUndefined();
  });

  it('drops an unrecognised reason instead of passing it to the copy switch', () => {
    expect(readAccountLifecycle(bannerRefusal('something_new'))?.reason).toBeUndefined();
  });

  it('still ignores an ordinary 403 with a string detail', () => {
    expect(
      readAccountLifecycle({
        response: { status: 403, data: { detail: 'Not enough permissions' } },
      })
    ).toBeNull();
  });
});

describe('handleAccountLifecycleError — banner hold keeps the session', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    clearAccountLifecycle();
    authStore.setToken('cookie');
  });

  it('publishes the hold without logging out', async () => {
    const state = await handleAccountLifecycleError(bannerRefusal('never_acknowledged'));

    expect(state?.code).toBe('banner_acknowledgment_required');
    expect(get(accountLifecycle)?.reason).toBe('never_acknowledged');
    // POST /auth/banner/acknowledge is the remedy and it is authenticated —
    // tearing the session down here would make the gate unclearable.
    expect(get(isAuthenticated)).toBe(true);
    expect(vi.mocked(axiosInstance.post)).not.toHaveBeenCalled();
  });
});

describe('acknowledgeBanner', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    clearAccountLifecycle();
    authStore.setToken('cookie');
  });

  it('posts to the exempt endpoint and releases the banner hold', async () => {
    vi.mocked(axiosInstance.post).mockResolvedValue({ data: { acknowledged: true } } as never);
    accountLifecycle.set({
      code: 'banner_acknowledgment_required',
      message: '',
      reason: 'never_acknowledged',
    });

    const result = await acknowledgeBanner();

    expect(result.success).toBe(true);
    expect(vi.mocked(axiosInstance.post)).toHaveBeenCalledWith('/auth/banner/acknowledge');
    expect(get(accountLifecycle)).toBeNull();
  });

  it('leaves a password-change hold alone', async () => {
    // The server checks the banner BEFORE the password gate, so a caller owing
    // both is refused again straight after acknowledging. Clearing that hold
    // here would hide the screen that fixes it.
    vi.mocked(axiosInstance.post).mockResolvedValue({ data: { acknowledged: true } } as never);
    accountLifecycle.set({ code: 'password_change_required', message: 'change it' });

    await acknowledgeBanner();

    expect(get(accountLifecycle)?.code).toBe('password_change_required');
  });

  it('reports failure instead of pretending consent was recorded', async () => {
    vi.mocked(axiosInstance.post).mockRejectedValue({
      response: { status: 500, data: {} },
    });
    accountLifecycle.set({ code: 'banner_acknowledgment_required', message: '' });

    const result = await acknowledgeBanner();

    expect(result.success).toBe(false);
    expect(result.message).toBe('loginBanner.acknowledgeFailed');
    // The hold must survive: the user is still confined until the server agrees.
    expect(get(accountLifecycle)?.code).toBe('banner_acknowledgment_required');
  });

  it("surfaces the server's own message when it sends one", async () => {
    vi.mocked(axiosInstance.post).mockRejectedValue({
      response: { status: 429, data: { detail: 'Too many requests' } },
    });

    expect((await acknowledgeBanner()).message).toBe('Too many requests');
  });
});
