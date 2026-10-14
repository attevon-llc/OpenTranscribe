import { describe, it, expect, vi, beforeEach } from 'vitest';

const mockInstance = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock('../axios', () => ({ default: mockInstance }));

import { SupportAccessApi } from './supportAccess';

const UUID = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee';

beforeEach(() => {
  vi.clearAllMocks();
  mockInstance.get.mockResolvedValue({ data: { marker: 'get' } });
  mockInstance.post.mockResolvedValue({ data: { marker: 'post' } });
});

describe('SupportAccessApi paths, verbs and bodies', () => {
  it('listMyGrants GETs /support-access/grants with scope, status and paging', async () => {
    const out = await SupportAccessApi.listMyGrants({
      status: 'pending',
      scope: 'all',
      limit: 25,
      offset: 50,
    });
    expect(mockInstance.get).toHaveBeenCalledWith('/support-access/grants', {
      params: { status: 'pending', scope: 'all', limit: 25, offset: 50 },
    });
    expect(out).toEqual({ marker: 'get' });
    expect(out).toEqual({ marker: 'get' });
  });

  it('listMyGrants omits an unset status filter', async () => {
    const out = await SupportAccessApi.listMyGrants({ scope: 'mine', limit: 25, offset: 0 });
    expect(mockInstance.get).toHaveBeenCalledWith('/support-access/grants', {
      params: { scope: 'mine', limit: 25, offset: 0 },
    });
    expect(out).toEqual({ marker: 'get' });
  });

  it('getGrant GETs the detail route', async () => {
    const out = await SupportAccessApi.getGrant(UUID);
    expect(mockInstance.get).toHaveBeenCalledWith(`/support-access/grants/${UUID}`);
    expect(out).toEqual({ marker: 'get' });
  });

  it('requestGrant POSTs the body as-is', async () => {
    const body = {
      organization_uuid: UUID,
      subject_user_uuid: null,
      access_level: 'read' as const,
      reason: 'Customer reported a stuck transcript',
      duration_minutes: 60,
    };
    const out = await SupportAccessApi.requestGrant(body);
    expect(mockInstance.post).toHaveBeenCalledWith('/support-access/grants', body);
    expect(out).toEqual({ marker: 'post' });
  });

  it('breakGlass POSTs to the break-glass route', async () => {
    const body = {
      organization_uuid: null,
      subject_user_uuid: UUID,
      access_level: 'write' as const,
      reason: 'Production outage for this customer',
      duration_minutes: 30,
      ticket_ref: 'INC-42',
    };
    const out = await SupportAccessApi.breakGlass(body);
    expect(mockInstance.post).toHaveBeenCalledWith('/support-access/grants/break-glass', body);
    expect(out).toEqual({ marker: 'post' });
  });

  it('revokeGrant POSTs the note', async () => {
    const out = await SupportAccessApi.revokeGrant(UUID, { note: 'done' });
    expect(mockInstance.post).toHaveBeenCalledWith(`/support-access/grants/${UUID}/revoke`, {
      note: 'done',
    });
    expect(out).toEqual({ marker: 'post' });
  });

  it('path-encodes uuids so a hostile value cannot escape the segment', async () => {
    const out = await SupportAccessApi.getGrant('a/b?c');
    expect(mockInstance.get).toHaveBeenCalledWith('/support-access/grants/a%2Fb%3Fc');
    expect(out).toEqual({ marker: 'get' });
  });

  it.each([
    ['staff', `/support-access/grants/${UUID}/uses`],
    ['org', `/org-admin/support-access/${UUID}/uses`],
    ['workspace', `/users/me/support-access/${UUID}/uses`],
  ] as const)('listUses(%s) hits %s with paging', async (perspective, path) => {
    const out = await SupportAccessApi.listUses(perspective, UUID, { limit: 25, offset: 25 });
    expect(mockInstance.get).toHaveBeenCalledWith(path, { params: { limit: 25, offset: 25 } });
    expect(out).toEqual({ marker: 'get' });
  });

  it('searchOrganizations GETs the target lookup', async () => {
    const out = await SupportAccessApi.searchOrganizations('acm', 10);
    expect(mockInstance.get).toHaveBeenCalledWith('/support-access/targets/organizations', {
      params: { q: 'acm', limit: 10 },
    });
    expect(out).toEqual({ marker: 'get' });
  });

  it.each([
    ['org', '/org-admin/support-access'],
    ['workspace', '/users/me/support-access'],
  ] as const)('listRequests(%s) GETs %s', async (perspective, path) => {
    const out = await SupportAccessApi.listRequests(perspective, {
      status: 'active',
      limit: 25,
      offset: 0,
    });
    expect(mockInstance.get).toHaveBeenCalledWith(path, {
      params: { status: 'active', limit: 25, offset: 0 },
    });
    expect(out).toEqual({ marker: 'get' });
  });

  it.each([
    ['org', `/org-admin/support-access/${UUID}/approve`],
    ['workspace', `/users/me/support-access/${UUID}/approve`],
  ] as const)('approve(%s) POSTs the shortened duration to %s', async (perspective, path) => {
    const out = await SupportAccessApi.approve(perspective, UUID, { duration_minutes: 30 });
    expect(mockInstance.post).toHaveBeenCalledWith(path, { duration_minutes: 30 });
    expect(out).toEqual({ marker: 'post' });
  });

  it.each([
    ['org', `/org-admin/support-access/${UUID}/deny`],
    ['workspace', `/users/me/support-access/${UUID}/deny`],
  ] as const)('deny(%s) POSTs the note to %s', async (perspective, path) => {
    const out = await SupportAccessApi.deny(perspective, UUID, { note: 'no' });
    expect(mockInstance.post).toHaveBeenCalledWith(path, { note: 'no' });
    expect(out).toEqual({ marker: 'post' });
  });

  it('revoke from the tenant side reuses the shared revoke route', async () => {
    const out = await SupportAccessApi.revokeGrant(UUID, {});
    expect(mockInstance.post).toHaveBeenCalledWith(`/support-access/grants/${UUID}/revoke`, {});
    expect(out).toEqual({ marker: 'post' });
  });
});
