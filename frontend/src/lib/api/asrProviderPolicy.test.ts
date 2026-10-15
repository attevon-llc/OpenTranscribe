import { describe, it, expect, vi, beforeEach } from 'vitest';

const mockInstance = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn() }));
vi.mock('../axios', () => ({ default: mockInstance }));

import {
  getProviderPolicy,
  updateProviderPolicy,
  buildPolicyUpdate,
  type PolicyProvider,
} from './asrProviderPolicy';

const provider = (id: string, allowed: boolean): PolicyProvider => ({
  id,
  display_name: id,
  allowed_for_users: allowed,
  shared_credentials: [],
});

beforeEach(() => vi.clearAllMocks());

describe('provider policy client', () => {
  it('reads the policy from /admin/asr-providers', async () => {
    const doc = { providers: [], users_may_add_own_keys: true };
    mockInstance.get.mockResolvedValue({ data: doc });
    await expect(getProviderPolicy()).resolves.toEqual(doc);
    expect(mockInstance.get).toHaveBeenCalledWith('/admin/asr-providers');
  });

  it('writes the exact body to /admin/asr-providers', async () => {
    const doc = { providers: [], users_may_add_own_keys: false };
    mockInstance.put.mockResolvedValue({ data: doc });
    const body = { allowed_for_users: ['deepgram'], users_may_add_own_keys: false };
    await expect(updateProviderPolicy(body)).resolves.toEqual(doc);
    expect(mockInstance.put).toHaveBeenCalledWith('/admin/asr-providers', body);
  });
});

describe('buildPolicyUpdate', () => {
  it('sends null (all allowed) only when every listed provider is allowed', () => {
    const all = [provider('a', true), provider('b', true)];
    expect(buildPolicyUpdate(all, true)).toEqual({
      allowed_for_users: null,
      users_may_add_own_keys: true,
    });
  });

  it('sends the allowed ids otherwise, in listed order', () => {
    const some = [provider('a', false), provider('b', true), provider('c', true)];
    expect(buildPolicyUpdate(some, false)).toEqual({
      allowed_for_users: ['b', 'c'],
      users_may_add_own_keys: false,
    });
  });

  it('sends an empty list when none is allowed (not null, which means all)', () => {
    expect(buildPolicyUpdate([provider('a', false)], true).allowed_for_users).toEqual([]);
  });
});
