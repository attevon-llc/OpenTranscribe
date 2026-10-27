/**
 * The effective-privacy read shared by every speaker surface (issue #1280). A page of speaker
 * rows must cost one request per file, a failed read must resolve to "no notice" instead of an
 * error and must not be remembered, and `resetEffectivePrivacy` must really forget (it is what
 * `clearUserState` calls so the next user never sees the previous user's organization policy).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

const api = vi.hoisted(() => ({ getEffective: vi.fn() }));
vi.mock('$lib/api/privacy', () => ({ PrivacyApi: api }));

import { loadEffectivePrivacy, resetEffectivePrivacy } from './privacyPolicy';

const policy = (voiceEnabled: boolean) => ({
  organization_uuid: null,
  voiceprint_matching: { enabled: voiceEnabled, locked_by: voiceEnabled ? null : 'user' },
  gender_inference: { enabled: true, locked_by: null },
});

beforeEach(() => {
  // mockReset, not clearAllMocks: queued once-values must not leak into the next test.
  api.getEffective.mockReset();
  resetEffectivePrivacy();
});

afterEach(() => {
  vi.useRealTimers();
});

describe('loadEffectivePrivacy', () => {
  it('reads the policy from the API and returns it', async () => {
    api.getEffective.mockResolvedValue(policy(false));
    const answer = await loadEffectivePrivacy('file-1');
    expect(answer).toEqual(policy(false));
    expect(api.getEffective).toHaveBeenCalledExactlyOnceWith('file-1');
  });

  it('asks about the caller (no file) when no file is given', async () => {
    api.getEffective.mockResolvedValue(policy(true));
    await loadEffectivePrivacy();
    expect(api.getEffective).toHaveBeenCalledExactlyOnceWith(undefined);
  });

  it('serves a second read of the same file from the cache, including one still in flight', async () => {
    let resolve!: (value: ReturnType<typeof policy>) => void;
    api.getEffective.mockReturnValue(new Promise((r) => (resolve = r)));
    const first = loadEffectivePrivacy('file-1');
    const second = loadEffectivePrivacy('file-1');
    resolve(policy(true));
    expect(await first).toEqual(await second);
    expect(await loadEffectivePrivacy('file-1')).toEqual(policy(true));
    expect(api.getEffective).toHaveBeenCalledTimes(1);
  });

  it('keeps one answer per file and one for the caller', async () => {
    api.getEffective.mockImplementation(async (uuid?: string) => policy(uuid !== 'file-off'));
    const off = await loadEffectivePrivacy('file-off');
    const on = await loadEffectivePrivacy('file-on');
    const mine = await loadEffectivePrivacy();
    expect(off?.voiceprint_matching.enabled).toBe(false);
    expect(on?.voiceprint_matching.enabled).toBe(true);
    expect(mine?.voiceprint_matching.enabled).toBe(true);
    expect(api.getEffective).toHaveBeenCalledTimes(3);
  });

  it('reads again once the answer is older than 15 seconds, so a saved change shows up', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-10-27T10:00:00Z'));
    api.getEffective.mockResolvedValueOnce(policy(true)).mockResolvedValueOnce(policy(false));
    expect((await loadEffectivePrivacy('file-1'))?.voiceprint_matching.enabled).toBe(true);

    vi.setSystemTime(new Date('2026-10-27T10:00:14Z'));
    expect((await loadEffectivePrivacy('file-1'))?.voiceprint_matching.enabled).toBe(true);
    expect(api.getEffective).toHaveBeenCalledTimes(1);

    vi.setSystemTime(new Date('2026-10-27T10:00:16Z'));
    expect((await loadEffectivePrivacy('file-1'))?.voiceprint_matching.enabled).toBe(false);
    expect(api.getEffective).toHaveBeenCalledTimes(2);
  });

  describe('when the read fails', () => {
    let errorSpy: ReturnType<typeof vi.spyOn>;
    beforeEach(() => {
      errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    });
    afterEach(() => errorSpy.mockRestore());

    it('resolves to null instead of throwing, and logs the cause', async () => {
      const boom = new Error('501 not implemented');
      api.getEffective.mockRejectedValue(boom);
      await expect(loadEffectivePrivacy('file-1')).resolves.toBeNull();
      expect(errorSpy).toHaveBeenCalledWith(expect.stringContaining('privacy policy'), boom);
    });

    it('does not remember the failure: the next mount retries and can succeed', async () => {
      api.getEffective.mockRejectedValueOnce(new Error('503')).mockResolvedValueOnce(policy(false));
      expect(await loadEffectivePrivacy('file-1')).toBeNull();
      expect((await loadEffectivePrivacy('file-1'))?.voiceprint_matching.enabled).toBe(false);
      expect(api.getEffective).toHaveBeenCalledTimes(2);
    });
  });
});

describe('resetEffectivePrivacy', () => {
  it('forgets every cached answer, so the next read goes to the server', async () => {
    api.getEffective.mockResolvedValueOnce(policy(true)).mockResolvedValueOnce(policy(false));
    expect((await loadEffectivePrivacy('file-1'))?.voiceprint_matching.enabled).toBe(true);

    resetEffectivePrivacy();

    expect((await loadEffectivePrivacy('file-1'))?.voiceprint_matching.enabled).toBe(false);
    expect(api.getEffective).toHaveBeenCalledTimes(2);
  });

  it('forgets the caller scope as well as the file scopes', async () => {
    api.getEffective.mockResolvedValue(policy(true));
    await loadEffectivePrivacy();
    await loadEffectivePrivacy('file-1');
    resetEffectivePrivacy();
    await loadEffectivePrivacy();
    await loadEffectivePrivacy('file-1');
    expect(api.getEffective).toHaveBeenCalledTimes(4);
  });
});
