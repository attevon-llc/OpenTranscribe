/**
 * `speakerClusters.ts` is mostly a thin typed wrapper around `/speaker-clusters` and
 * `/speaker-profiles`. Tests cover a representative sample of the wire shapes —
 * optional query params, URLSearchParams-encoded PUT bodies, and query-string
 * encoding of a free-text value — plus that each call returns the server's payload.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

const mockInstance = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
}));

vi.mock('../axios', () => ({ default: mockInstance }));

import {
  listClusters,
  updateCluster,
  mergeClusters,
  splitCluster,
  batchVerifySpeakers,
  updateProfile,
  confirmSpeakerGender,
  unassignSpeakers,
} from './speakerClusters';

beforeEach(() => {
  vi.clearAllMocks();
  mockInstance.get.mockResolvedValue({ data: {} });
  mockInstance.post.mockResolvedValue({ data: {} });
  mockInstance.put.mockResolvedValue({ data: {} });
  mockInstance.delete.mockResolvedValue({ data: {} });
});

describe('listClusters', () => {
  it('omits search/has_label params when not given, and returns the page unchanged', async () => {
    mockInstance.get.mockResolvedValue({ data: { items: [], total: 0, page: 1 } });
    const result = await listClusters();
    expect(mockInstance.get).toHaveBeenCalledWith('/speaker-clusters', {
      params: { page: 1, per_page: 20 },
    });
    expect(result).toEqual({ items: [], total: 0, page: 1 });
  });

  it('includes search and has_label when provided, including has_label: false', async () => {
    mockInstance.get.mockResolvedValue({ data: { items: [{ uuid: 'c1' }], total: 1, page: 2 } });
    const result = await listClusters(2, 10, 'alice', false);
    expect(mockInstance.get).toHaveBeenCalledWith('/speaker-clusters', {
      params: { page: 2, per_page: 10, search: 'alice', has_label: false },
    });
    expect(result.items).toEqual([{ uuid: 'c1' }]);
  });
});

describe('updateCluster / mergeClusters / splitCluster', () => {
  it('PUTs the label/description and returns the updated cluster', async () => {
    mockInstance.put.mockResolvedValue({ data: { uuid: 'c1', label: 'Alice' } });
    const result = await updateCluster('c1', { label: 'Alice' });
    expect(mockInstance.put).toHaveBeenCalledWith('/speaker-clusters/c1', { label: 'Alice' });
    expect(result).toEqual({ uuid: 'c1', label: 'Alice' });
  });

  it('merges source into target via the path segments and returns the merged cluster', async () => {
    mockInstance.post.mockResolvedValue({ data: { uuid: 'tgt', member_count: 5 } });
    const result = await mergeClusters('src', 'tgt');
    expect(mockInstance.post).toHaveBeenCalledWith('/speaker-clusters/src/merge/tgt');
    expect(result).toEqual({ uuid: 'tgt', member_count: 5 });
  });

  it('splits a cluster by the given speaker uuids and returns the new cluster', async () => {
    mockInstance.post.mockResolvedValue({ data: { uuid: 'c2', member_count: 2 } });
    const result = await splitCluster('c1', ['s1', 's2']);
    expect(mockInstance.post).toHaveBeenCalledWith('/speaker-clusters/c1/split', {
      speaker_uuids: ['s1', 's2'],
    });
    expect(result).toEqual({ uuid: 'c2', member_count: 2 });
  });
});

describe('batchVerifySpeakers', () => {
  it('sends the action and optional profile/display-name fields, and returns the response', async () => {
    mockInstance.post.mockResolvedValue({ data: { updated: 2 } });
    const result = await batchVerifySpeakers(['s1', 's2'], 'assign', 'profile-1');
    expect(mockInstance.post).toHaveBeenCalledWith('/speaker-clusters/batch-verify', {
      speaker_uuids: ['s1', 's2'],
      action: 'assign',
      profile_uuid: 'profile-1',
      display_name: undefined,
    });
    expect(result).toEqual({ updated: 2 });
  });
});

describe('updateProfile', () => {
  it('encodes name/description as URLSearchParams on the query string, and returns the updated profile', async () => {
    mockInstance.put.mockResolvedValue({
      data: { uuid: 'p1', name: 'Bob', description: 'a note' },
    });
    const result = await updateProfile('p1', { name: 'Bob', description: 'a note' });
    expect(mockInstance.put).toHaveBeenCalledWith(
      '/speaker-profiles/profiles/p1?name=Bob&description=a+note'
    );
    expect(result).toEqual({ uuid: 'p1', name: 'Bob', description: 'a note' });
  });

  it('omits description from the query string when undefined, but keeps an explicit empty string', async () => {
    mockInstance.put.mockResolvedValue({ data: { uuid: 'p1', name: 'Bob' } });
    const result = await updateProfile('p1', { name: 'Bob' });
    expect(mockInstance.put).toHaveBeenCalledWith('/speaker-profiles/profiles/p1?name=Bob');
    expect(result.name).toBe('Bob');

    await updateProfile('p1', { description: '' });
    expect(mockInstance.put).toHaveBeenCalledWith('/speaker-profiles/profiles/p1?description=');
  });
});

describe('confirmSpeakerGender', () => {
  it('URL-encodes a gender value with special characters, and returns the confirmed result', async () => {
    mockInstance.post.mockResolvedValue({
      data: {
        speaker_uuid: 's1',
        predicted_gender: 'non-binary & other',
        gender_confirmed_by_user: true,
      },
    });
    const result = await confirmSpeakerGender('s1', 'non-binary & other');
    expect(mockInstance.post).toHaveBeenCalledWith(
      '/speakers/s1/confirm-gender?gender=non-binary%20%26%20other'
    );
    expect(result.gender_confirmed_by_user).toBe(true);
  });
});

describe('unassignSpeakers', () => {
  it('defaults blacklist to true and returns the unassign count', async () => {
    mockInstance.post.mockResolvedValue({ data: { unassigned_count: 1, message: 'ok' } });
    const result = await unassignSpeakers('c1', ['s1']);
    expect(mockInstance.post).toHaveBeenCalledWith('/speaker-clusters/c1/unassign', {
      speaker_uuids: ['s1'],
      blacklist: true,
    });
    expect(result.unassigned_count).toBe(1);
  });
});
