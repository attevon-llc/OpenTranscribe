/**
 * documents.ts — `listDocuments`' query serialization (#362 lane C3).
 *
 * Caught live, not by a type-checker: axios's default array serialization produces
 * `status[]=completed`, and FastAPI's `status: list[str] | None = Query(None)`
 * (backend/app/api/endpoints/documents.py) does not recognize that shape — it needs
 * the param repeated bare. Without a custom `paramsSerializer` the `status` filter
 * silently matched every document regardless of status; `PickerDocumentsTab`'s
 * "completed only" request returned pending/error documents right alongside them.
 * These tests build the REAL query string axios would send (via the real
 * `paramsSerializer`, not a mock of it) so a regression here fails loudly instead of
 * quietly widening a filter into a no-op again.
 */
import { describe, it, expect, vi } from 'vitest';

const getMock = vi.hoisted(() => vi.fn().mockResolvedValue({ data: { documents: [], total: 0 } }));
vi.mock('$lib/axios', () => ({ default: { get: getMock } }));

import { listDocuments } from './documents';

function sentQueryString(): string {
  const [, config] = getMock.mock.calls[getMock.mock.calls.length - 1];
  return config.paramsSerializer(config.params);
}

describe('listDocuments query serialization (#362 lane C3)', () => {
  it('serializes a single-value status array as a bare repeated param', async () => {
    await listDocuments(0, 30, { status: ['completed'] });

    const qs = sentQueryString();
    expect(qs).toContain('status=completed');
    expect(qs).not.toContain('status%5B%5D'); // status[]=... — the bug shape
    expect(qs).not.toContain('status[]');
  });

  it('serializes a multi-value status array as repeated bare params', async () => {
    await listDocuments(0, 30, { status: ['pending', 'processing'] });

    const qs = sentQueryString();
    const parsed = new URLSearchParams(qs);
    expect(parsed.getAll('status')).toEqual(['pending', 'processing']);
  });

  it('omits status entirely when none is given', async () => {
    await listDocuments(0, 30, {});

    const qs = sentQueryString();
    expect(qs).not.toContain('status');
  });

  it('still serializes plain scalar params (skip/limit/search/sort) correctly', async () => {
    await listDocuments(10, 25, { search: 'report', sortBy: 'filename', sortOrder: 'asc' });

    const qs = sentQueryString();
    const parsed = new URLSearchParams(qs);
    expect(parsed.get('skip')).toBe('10');
    expect(parsed.get('limit')).toBe('25');
    expect(parsed.get('search')).toBe('report');
    expect(parsed.get('sort_by')).toBe('filename');
    expect(parsed.get('sort_order')).toBe('asc');
  });
});
