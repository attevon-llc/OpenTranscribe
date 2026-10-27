/**
 * The privacy client (issue #1280): every call must hit the path the backend mounts and send
 * the literal confirmation word on a purge. A wrong verb or path is a silent 404/405, and a
 * purge body without `confirm: 'PURGE'` is refused (422), so these pin the wire.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('$lib/axios', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}));

import axiosInstance from '$lib/axios';
import { OrgAdminApi } from './orgAdmin';
import { PrivacyApi, PURGE_CONFIRM_WORD, isTerminalPurgeState } from './privacy';

const get = vi.mocked(axiosInstance.get);
const put = vi.mocked(axiosInstance.put);
const post = vi.mocked(axiosInstance.post);
const patch = vi.mocked(axiosInstance.patch);

beforeEach(() => {
  vi.clearAllMocks();
  for (const fn of [get, put, post, patch]) fn.mockResolvedValue({ data: { ok: true } } as never);
});

describe('PrivacyApi', () => {
  it('asks for the effective policy of a file, or of the caller when no file is given', async () => {
    await PrivacyApi.getEffective('f-1');
    expect(get).toHaveBeenLastCalledWith('/privacy/effective', { params: { file_uuid: 'f-1' } });
    await PrivacyApi.getEffective();
    expect(get).toHaveBeenLastCalledWith('/privacy/effective', { params: undefined });
  });

  it('reads and writes the caller own switch', async () => {
    await PrivacyApi.getMine();
    expect(get).toHaveBeenLastCalledWith('/privacy/me');
    await PrivacyApi.setMine(false);
    expect(put).toHaveBeenLastCalledWith('/privacy/me', { voiceprint_matching_enabled: false });
  });

  it('purges the caller own data with the literal confirmation word', async () => {
    await PrivacyApi.purgeMine(['voiceprints', 'inferred_gender']);
    expect(post).toHaveBeenLastCalledWith('/privacy/me/purge', {
      targets: ['voiceprints', 'inferred_gender'],
      confirm: 'PURGE',
    });
    expect(PURGE_CONFIRM_WORD).toBe('PURGE');
  });

  it('encodes the task id of a purge status read', async () => {
    await PrivacyApi.getPurgeStatus('a/b c');
    expect(get).toHaveBeenLastCalledWith('/privacy/purge/a%2Fb%20c');
  });

  it('sends only the instance switches that changed', async () => {
    await PrivacyApi.getInstance();
    expect(get).toHaveBeenLastCalledWith('/admin/privacy-policy');
    await PrivacyApi.updateInstance({ gender_inference_enabled: false });
    expect(put).toHaveBeenLastCalledWith('/admin/privacy-policy', {
      gender_inference_enabled: false,
    });
  });

  it('sends the scope with an instance purge', async () => {
    await PrivacyApi.purgeInstance('personal', ['voiceprints']);
    expect(post).toHaveBeenLastCalledWith('/admin/privacy-policy/purge', {
      scope: 'personal',
      targets: ['voiceprints'],
      confirm: 'PURGE',
    });
  });

  it('knows which purge states are final', () => {
    expect(['completed', 'failed', 'skipped'].every((s) => isTerminalPurgeState(s as never))).toBe(
      true
    );
    expect(['pending', 'in_progress'].some((s) => isTerminalPurgeState(s as never))).toBe(false);
  });
});

describe('OrgAdminApi privacy policy', () => {
  it('reads and patches the organization policy with no organization parameter', async () => {
    await OrgAdminApi.getPolicy();
    expect(get).toHaveBeenLastCalledWith('/org-admin/policy');
    await OrgAdminApi.updatePolicy({ voiceprint_matching_enabled: false });
    expect(patch).toHaveBeenLastCalledWith('/org-admin/policy', {
      voiceprint_matching_enabled: false,
    });
  });

  it('purges the organization data with the literal confirmation word', async () => {
    await OrgAdminApi.purgePolicyData(['inferred_gender']);
    expect(post).toHaveBeenLastCalledWith('/org-admin/policy/purge', {
      targets: ['inferred_gender'],
      confirm: 'PURGE',
    });
  });
});
