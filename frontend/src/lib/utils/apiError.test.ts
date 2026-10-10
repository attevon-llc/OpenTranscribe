import { describe, it, expect, beforeAll, vi, beforeEach } from 'vitest';
import { initI18n } from '$lib/i18n';
import { getErrorMessage, withAsync } from './apiError';

const toastWarning = vi.fn();
const toastError = vi.fn();
vi.mock('$stores/toast', () => ({
  toastStore: {
    warning: (...args: unknown[]) => toastWarning(...args),
    error: (...args: unknown[]) => toastError(...args),
  },
}));
vi.mock('$stores/auth', () => ({
  readAccountLifecycle: () => null,
}));

describe('getErrorMessage', () => {
  // The default fallback is translated, so the locale bundle must be registered
  // before asserting on it — otherwise `t` returns the raw dot-notation key.
  beforeAll(async () => {
    await initI18n('en');
  });

  it('prefers the FastAPI response.data.detail', () => {
    expect(getErrorMessage({ response: { data: { detail: 'Not authorized' } } })).toBe(
      'Not authorized'
    );
  });
  it('extracts the msg from a FastAPI 422 validation-error array detail', () => {
    expect(
      getErrorMessage({
        response: { data: { detail: [{ msg: 'field required' }] } },
      })
    ).toBe('field required');
  });
  it('joins multiple validation-error messages from an array detail', () => {
    expect(
      getErrorMessage({
        response: {
          data: { detail: [{ msg: 'field required' }, { msg: 'must be positive' }] },
        },
      })
    ).toBe('field required. must be positive');
  });
  it('still returns the object-shaped detail.message (account-lifecycle refusals)', () => {
    expect(
      getErrorMessage({
        response: {
          data: { detail: { code: 'account_expired', message: 'Your account has expired.' } },
        },
      })
    ).toBe('Your account has expired.');
  });
  it('falls back to response.data.message', () => {
    expect(getErrorMessage({ response: { data: { message: 'Bad request' } } })).toBe('Bad request');
  });
  it('falls back to error.message', () => {
    expect(getErrorMessage(new Error('boom'))).toBe('boom');
  });
  it('uses the fallback when nothing usable', () => {
    expect(getErrorMessage({}, 'Default msg')).toBe('Default msg');
    expect(getErrorMessage(null)).toBe('Something went wrong');
  });
  it('ignores empty/whitespace detail', () => {
    expect(
      getErrorMessage({ response: { data: { detail: '   ' } }, message: 'fallback-msg' })
    ).toBe('fallback-msg');
  });
});

describe('handleApiError — 429 wait hint (issue #788)', () => {
  beforeAll(async () => {
    await initI18n('en');
  });

  beforeEach(() => {
    toastWarning.mockClear();
    toastError.mockClear();
  });

  it('routes a 429 with a known retryAfterSeconds through toastStore.warning with the hint', async () => {
    const { handleApiError } = await import('./apiError');
    const error = {
      response: { status: 429, data: { detail: 'Too many requests.' } },
      retryAfterSeconds: 12,
    };

    const message = handleApiError(error);

    expect(message).toBe('Too many requests.');
    expect(toastWarning).toHaveBeenCalledWith('Too many requests.', undefined, {
      retryAfterSeconds: 12,
    });
    expect(toastError).not.toHaveBeenCalled();
  });

  it('falls back to toastStore.error for a 429 with no retryAfterSeconds', async () => {
    // Absent/unparseable Retry-After must degrade to the ordinary error toast,
    // never show a hint with a missing or NaN wait time.
    const { handleApiError } = await import('./apiError');
    const error = { response: { status: 429, data: { detail: 'Too many requests.' } } };

    handleApiError(error);

    expect(toastError).toHaveBeenCalledWith('Too many requests.');
    expect(toastWarning).not.toHaveBeenCalled();
  });

  it('uses toastStore.error for a non-429 status even if retryAfterSeconds is somehow present', async () => {
    const { handleApiError } = await import('./apiError');
    const error = {
      response: { status: 500, data: { detail: 'Server error.' } },
      retryAfterSeconds: 12,
    };

    handleApiError(error);

    expect(toastError).toHaveBeenCalledWith('Server error.');
    expect(toastWarning).not.toHaveBeenCalled();
  });

  it('is silent when opts.silent is set, even for a 429', async () => {
    const { handleApiError } = await import('./apiError');
    const error = {
      response: { status: 429, data: { detail: 'Too many requests.' } },
      retryAfterSeconds: 12,
    };

    handleApiError(error, undefined, { silent: true });

    expect(toastWarning).not.toHaveBeenCalled();
    expect(toastError).not.toHaveBeenCalled();
  });
});

describe('withAsync', () => {
  it('returns the result on success', async () => {
    const r = await withAsync(async () => 42, { errorMsg: 'fail' });
    expect(r).toBe(42);
  });
  it('returns undefined and swallows on failure', async () => {
    const r = await withAsync(
      async () => {
        throw new Error('nope');
      },
      { errorMsg: 'fail', silent: true }
    );
    expect(r).toBeUndefined();
  });
  it('runs onFinally on both paths', async () => {
    let calls = 0;
    await withAsync(async () => 1, { errorMsg: 'x', onFinally: () => calls++ });
    await withAsync(
      async () => {
        throw new Error('e');
      },
      { errorMsg: 'x', silent: true, onFinally: () => calls++ }
    );
    expect(calls).toBe(2);
  });
});
