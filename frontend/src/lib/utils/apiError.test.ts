import { describe, it, expect } from 'vitest';
import { getErrorMessage, withAsync } from './apiError';

describe('getErrorMessage', () => {
  it('prefers the FastAPI response.data.detail', () => {
    expect(getErrorMessage({ response: { data: { detail: 'Not authorized' } } })).toBe(
      'Not authorized'
    );
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
