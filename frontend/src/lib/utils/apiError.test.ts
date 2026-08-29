import { describe, it, expect, beforeAll } from 'vitest';
import { initI18n } from '$lib/i18n';
import { getErrorMessage, withAsync } from './apiError';

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
