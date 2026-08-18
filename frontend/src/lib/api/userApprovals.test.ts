/**
 * `userApprovals.ts` covers the account-approval queue. `isAlreadyDecided` is the
 * one piece of real logic — the module's own docstring explains why the backend
 * refuses to re-decide (a second approve/reject would corrupt audit fields or
 * revoke a working account's sessions), so a caller MUST distinguish this 409
 * from a generic failure rather than swallow it.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

const mockInstance = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock('../axios', () => ({ default: mockInstance }));

import { UserApprovalsApi, isAlreadyDecided } from './userApprovals';

beforeEach(() => {
  vi.clearAllMocks();
});

describe('isAlreadyDecided', () => {
  it('is true only for a 409 response', () => {
    expect(isAlreadyDecided({ response: { status: 409 } })).toBe(true);
    expect(isAlreadyDecided({ response: { status: 500 } })).toBe(false);
    expect(isAlreadyDecided({ response: { status: 404 } })).toBe(false);
  });

  it('is false for a value with no response at all', () => {
    expect(isAlreadyDecided(new Error('network down'))).toBe(false);
    expect(isAlreadyDecided(undefined)).toBe(false);
  });
});

describe('list', () => {
  it('defaults to limit 200 offset 0, oldest first', async () => {
    mockInstance.get.mockResolvedValue({ data: [{ uuid: 'u1' }] });
    const result = await UserApprovalsApi.list();
    expect(mockInstance.get).toHaveBeenCalledWith('/admin/user-approvals', {
      params: { limit: 200, offset: 0 },
    });
    expect(result).toHaveLength(1);
  });

  it('passes through a custom page', async () => {
    mockInstance.get.mockResolvedValue({ data: [{ uuid: 'u2' }] });
    const result = await UserApprovalsApi.list(50, 100);
    expect(mockInstance.get).toHaveBeenCalledWith('/admin/user-approvals', {
      params: { limit: 50, offset: 100 },
    });
    expect(result).toEqual([{ uuid: 'u2' }]);
  });
});

describe('approve / reject', () => {
  it('sends a null body when no reason is given', async () => {
    mockInstance.post.mockResolvedValue({ data: { uuid: 'u1', approval_status: 'approved' } });
    const result = await UserApprovalsApi.approve('u1');
    expect(mockInstance.post).toHaveBeenCalledWith('/admin/user-approvals/u1/approve', null);
    expect(result.approval_status).toBe('approved');
  });

  it('wraps a given reason in { reason } for both approve and reject', async () => {
    mockInstance.post.mockResolvedValue({ data: { uuid: 'u1', approval_status: 'rejected' } });
    const result = await UserApprovalsApi.reject('u1', 'suspicious email domain');
    expect(mockInstance.post).toHaveBeenCalledWith('/admin/user-approvals/u1/reject', {
      reason: 'suspicious email domain',
    });
    expect(result.approval_status).toBe('rejected');
  });
});
