import { describe, it, expect, vi, beforeEach } from 'vitest';

/**
 * The client is transport-only, so these tests assert the wire shape each
 * method produces (path, method, params/body) plus the return-value passthrough.
 *
 * Regression coverage for BC-25: `triggerRedactionReindex` used to hand-build
 * its query string with a template literal (`?only_stale=${onlyStale}`) instead
 * of axios' `{ params }` option, unlike every sibling function in this file.
 */
const mockInstance = vi.hoisted(() => ({
  get: vi.fn(),
  put: vi.fn(),
  post: vi.fn(),
  delete: vi.fn(),
}));

vi.mock('$lib/axios', async () => {
  const actual = await vi.importActual<typeof import('$lib/axios')>('$lib/axios');
  return { ...actual, default: mockInstance };
});

import {
  getRedactionSettings,
  updateRedactionSettings,
  resetRedactionSettings,
  getRedactionDefaults,
  getRedactionPolicy,
  updateRedactionPolicy,
  triggerRedactionReindex,
} from './redactionSettings';

beforeEach(() => {
  vi.clearAllMocks();
  mockInstance.get.mockResolvedValue({ data: {} });
  mockInstance.put.mockResolvedValue({ data: {} });
  mockInstance.post.mockResolvedValue({ data: {} });
  mockInstance.delete.mockResolvedValue({ data: {} });
});

describe('triggerRedactionReindex', () => {
  it('sends only_stale=true via the params option, not a hand-built query string', async () => {
    mockInstance.post.mockResolvedValue({ data: { status: 'queued' } });
    const result = await triggerRedactionReindex(true);
    expect(mockInstance.post).toHaveBeenCalledWith(
      '/admin/redaction-policy/reindex',
      {},
      { params: { only_stale: true } }
    );
    expect(result).toEqual({ status: 'queued' });
  });

  it('sends only_stale=false via the params option when explicitly disabled', async () => {
    mockInstance.post.mockResolvedValue({ data: { status: 'queued' } });
    const result = await triggerRedactionReindex(false);
    expect(mockInstance.post).toHaveBeenCalledWith(
      '/admin/redaction-policy/reindex',
      {},
      { params: { only_stale: false } }
    );
    expect(result).toEqual({ status: 'queued' });
  });

  it('defaults only_stale to true when called with no argument', async () => {
    mockInstance.post.mockResolvedValue({ data: { status: 'queued' } });
    const result = await triggerRedactionReindex();
    expect(mockInstance.post).toHaveBeenCalledWith(
      '/admin/redaction-policy/reindex',
      {},
      { params: { only_stale: true } }
    );
    expect(result).toEqual({ status: 'queued' });
  });
});

describe('per-user preferences', () => {
  it('fetches redaction settings and returns them unchanged', async () => {
    const settings = { enabled: true, style: 'label' };
    mockInstance.get.mockResolvedValue({ data: settings });
    const result = await getRedactionSettings();
    expect(mockInstance.get).toHaveBeenCalledWith('/user-settings/redaction');
    expect(result).toEqual(settings);
  });

  it('puts partial settings updates and returns the server copy', async () => {
    mockInstance.put.mockResolvedValue({ data: { enabled: false } });
    const result = await updateRedactionSettings({ enabled: false });
    expect(mockInstance.put).toHaveBeenCalledWith('/user-settings/redaction', { enabled: false });
    expect(result).toEqual({ enabled: false });
  });

  it('resets settings via DELETE and returns the confirmation message', async () => {
    mockInstance.delete.mockResolvedValue({ data: { message: 'reset' } });
    const result = await resetRedactionSettings();
    expect(mockInstance.delete).toHaveBeenCalledWith('/user-settings/redaction');
    expect(result).toEqual({ message: 'reset' });
  });

  it('fetches system defaults/option lists', async () => {
    const defaults = { available_detectors: ['pii'], locked_categories: [] };
    mockInstance.get.mockResolvedValue({ data: defaults });
    const result = await getRedactionDefaults();
    expect(mockInstance.get).toHaveBeenCalledWith('/user-settings/redaction/defaults');
    expect(result).toEqual(defaults);
  });
});

describe('admin governance', () => {
  it('fetches the admin redaction policy', async () => {
    const policy = { force_pii: true, force_pii_entities: ['EMAIL'] };
    mockInstance.get.mockResolvedValue({ data: policy });
    const result = await getRedactionPolicy();
    expect(mockInstance.get).toHaveBeenCalledWith('/admin/redaction-policy');
    expect(result).toEqual(policy);
  });

  it('posts a partial policy update to the /update path', async () => {
    mockInstance.post.mockResolvedValue({ data: { force_pii: true } });
    const result = await updateRedactionPolicy({ force_pii: true });
    expect(mockInstance.post).toHaveBeenCalledWith('/admin/redaction-policy/update', {
      force_pii: true,
    });
    expect(result).toEqual({ force_pii: true });
  });
});
