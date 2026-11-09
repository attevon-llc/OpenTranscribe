import { describe, expect, it, vi } from 'vitest';
import {
  CommentEndpointsExhausted,
  fetchFileComments,
  postFileComment,
  withLegacyFallbacks,
  withUser,
} from './commentRequests';

const status = (code: number) => ({ response: { status: code } });

describe('withLegacyFallbacks', () => {
  it('returns the primary result without touching the fallbacks', async () => {
    const fallback = vi.fn();
    await expect(withLegacyFallbacks(async () => 'ok', [fallback])).resolves.toBe('ok');
    expect(fallback).not.toHaveBeenCalled();
  });

  it.each([401, 403, 500])(
    'rethrows a %i from the primary as it is, trying nothing else',
    async (code) => {
      const fallback = vi.fn();
      const error = status(code);
      await expect(withLegacyFallbacks(() => Promise.reject(error), [fallback])).rejects.toBe(
        error
      );
      expect(fallback).not.toHaveBeenCalled();
    }
  );

  it('on a 404 tries the fallbacks in order and stops at the first that works', async () => {
    const calls: string[] = [];
    const result = await withLegacyFallbacks(
      () => Promise.reject(status(404)),
      [
        async () => {
          calls.push('legacy');
          throw status(500);
        },
        async () => {
          calls.push('query');
          return 'ok';
        },
      ]
    );
    expect(result).toBe('ok');
    expect(calls).toEqual(['legacy', 'query']);
  });

  it('reports the last fallback error when every endpoint fails', async () => {
    const last = status(500);
    const failure = await withLegacyFallbacks(
      () => Promise.reject(status(404)),
      [() => Promise.reject(status(404)), () => Promise.reject(last)]
    ).catch((e) => e);
    expect(failure).toBeInstanceOf(CommentEndpointsExhausted);
    expect(failure.cause).toBe(last);
  });

  it('shows the primary failure to the logger even when a fallback then succeeds', async () => {
    const seen = vi.fn();
    await withLegacyFallbacks(() => Promise.reject(status(404)), [async () => 1], seen);
    expect(seen).toHaveBeenCalledWith(status(404));
  });
});

describe('the endpoint tables', () => {
  const client = () => ({
    get: vi.fn().mockRejectedValue(status(404)),
    post: vi.fn().mockRejectedValue(status(404)),
  });

  it('lists comments from the current endpoint, then the legacy path, then the query form', async () => {
    const c = client();
    await fetchFileComments(c as never, 'f1').catch(() => {});
    expect(c.get.mock.calls).toEqual([
      ['/comments/files/f1/comments'],
      ['/files/f1/comments'],
      ['/comments', { params: { media_file_id: 'f1' } }],
    ]);
  });

  it('posts the same body, with media_file_id, to each endpoint in the same order', async () => {
    const c = client();
    await postFileComment(c as never, 'f1', 'hi', 12).catch(() => {});
    const body = { text: 'hi', timestamp: 12, media_file_id: 'f1' };
    expect(c.post.mock.calls).toEqual([
      ['/comments/files/f1/comments', body],
      ['/files/f1/comments', body],
      ['/comments', body],
    ]);
  });
});

describe('withUser', () => {
  it('keeps a comment that already has a user', () => {
    const comment = { user: { uuid: 'u' }, user_id: 'u' };
    expect(withUser(comment, { email: 'a@x.com' }, 'u')).toBe(comment);
  });

  it('builds the user from the signed-in user, falling back from name to email to the id', () => {
    expect(withUser({ user_id: '7' }, { email: 'a@x.com', full_name: 'Ann' }, '7').user).toEqual({
      uuid: '7',
      email: 'a@x.com',
      full_name: 'Ann',
    });
    expect(withUser({ user_id: '7' }, { email: 'a@x.com' }, '7').user).toMatchObject({
      full_name: 'a@x.com',
    });
    expect(withUser({ user_id: '7' }, null, undefined).user).toMatchObject({ full_name: 'User 7' });
  });
});
