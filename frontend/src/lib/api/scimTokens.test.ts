/**
 * `scimTokens.ts` (`ScimTokensApi`) — thin wrapper around `/admin/scim-tokens`.
 * Worth pinning: `create` is the ONLY call that ever returns a plaintext token
 * (the module's own header says the row stores only a digest afterward), and
 * `revoke` returns the token's post-revocation state, not void.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

const mockInstance = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), delete: vi.fn() }));
vi.mock('../axios', () => ({ default: mockInstance }));

import { ScimTokensApi } from './scimTokens';

beforeEach(() => {
  vi.clearAllMocks();
});

describe('list', () => {
  it('returns every token, never including a plaintext value', async () => {
    mockInstance.get.mockResolvedValue({
      data: [{ uuid: 't1', name: 'CI', revoked_at: null }],
    });
    const tokens = await ScimTokensApi.list();
    expect(mockInstance.get).toHaveBeenCalledWith('/admin/scim-tokens');
    expect(tokens[0]).not.toHaveProperty('token');
  });
});

describe('create', () => {
  it('returns the plaintext token exactly once, alongside the row', async () => {
    mockInstance.post.mockResolvedValue({
      data: { uuid: 't1', name: 'CI', token: 'test-plaintext-token-value', expires_at: null },
    });
    const created = await ScimTokensApi.create({ name: 'CI' });
    expect(mockInstance.post).toHaveBeenCalledWith('/admin/scim-tokens', { name: 'CI' });
    expect(created.token).toBe('test-plaintext-token-value');
  });
});

describe('revoke', () => {
  it('returns the token with revoked_at now set', async () => {
    mockInstance.delete.mockResolvedValue({
      data: { uuid: 't1', name: 'CI', revoked_at: '2026-01-01T00:00:00Z' },
    });
    const revoked = await ScimTokensApi.revoke('t1');
    expect(mockInstance.delete).toHaveBeenCalledWith('/admin/scim-tokens/t1');
    expect(revoked.revoked_at).toBe('2026-01-01T00:00:00Z');
  });
});
