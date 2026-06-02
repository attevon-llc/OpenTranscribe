import { describe, it, expect } from 'vitest';
import { AppError, toAppError } from './errors';

describe('AppError', () => {
  it('carries message, status, and cause', () => {
    const cause = new Error('root');
    const err = new AppError('boom', { status: 503, cause });
    expect(err).toBeInstanceOf(AppError);
    expect(err).toBeInstanceOf(Error);
    expect(err.message).toBe('boom');
    expect(err.status).toBe(503);
    expect(err.cause).toBe(cause);
    expect(err.name).toBe('AppError');
  });
});

describe('toAppError', () => {
  it('returns an existing AppError unchanged', () => {
    const original = new AppError('keep me', { status: 418 });
    expect(toAppError(original)).toBe(original);
  });

  it('wraps a plain string', () => {
    const err = toAppError('just a string');
    expect(err).toBeInstanceOf(AppError);
    expect(err.message).toBe('just a string');
    expect(err.status).toBeUndefined();
  });

  it('extracts message + status from an axios-like error and keeps cause', () => {
    const axiosLike = {
      message: 'Request failed',
      response: { status: 404, data: { detail: 'Not found' } },
    };
    const err = toAppError(axiosLike);
    expect(err.message).toBe('Not found');
    expect(err.status).toBe(404);
    expect(err.cause).toBe(axiosLike);
  });

  it('falls back for non-error values', () => {
    expect(toAppError(null, 'fallback msg').message).toBe('fallback msg');
    expect(toAppError(undefined).message).toBe('Something went wrong');
  });
});
