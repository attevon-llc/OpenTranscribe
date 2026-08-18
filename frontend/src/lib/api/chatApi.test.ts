import { describe, it, expect, vi, beforeEach } from 'vitest';

/**
 * `chatApi` is ~22 mostly-thin CRUD wrappers around `axiosInstance`. This
 * file focuses on `exportConversation`, the one function with real parsing
 * logic (a content-disposition regex with a filename fallback), plus a
 * representative slice of the CRUD surface for request-shape coverage.
 */
const mockInstance = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  patch: vi.fn(),
  delete: vi.fn(),
  put: vi.fn(),
}));

vi.mock('$lib/axios', async () => {
  const actual = await vi.importActual<typeof import('$lib/axios')>('$lib/axios');
  return { ...actual, default: mockInstance };
});

import {
  createConversation,
  createProject,
  deleteConversation,
  exportConversation,
  listConversations,
  listMessages,
} from './chatApi';

const UUID = '11111111-1111-1111-1111-111111111111';

beforeEach(() => {
  vi.clearAllMocks();
  mockInstance.get.mockResolvedValue({ data: {} });
  mockInstance.post.mockResolvedValue({ data: {} });
  mockInstance.patch.mockResolvedValue({ data: {} });
  mockInstance.delete.mockResolvedValue({ data: {} });
  mockInstance.put.mockResolvedValue({ data: {} });
});

describe('exportConversation', () => {
  it('parses a quoted content-disposition filename', async () => {
    const blob = new Blob(['# hi']);
    mockInstance.get.mockResolvedValue({
      data: blob,
      headers: { 'content-disposition': 'attachment; filename="my-chat.md"' },
    });
    const result = await exportConversation(UUID);
    expect(mockInstance.get).toHaveBeenCalledWith(`/chat/conversations/${UUID}/export`, {
      params: { format: 'markdown' },
      responseType: 'blob',
    });
    expect(result).toEqual({ blob, filename: 'my-chat.md' });
  });

  it('parses an unquoted content-disposition filename', async () => {
    const blob = new Blob(['{}']);
    mockInstance.get.mockResolvedValue({
      data: blob,
      headers: { 'content-disposition': 'attachment; filename=my-chat.json' },
    });
    const result = await exportConversation(UUID, 'json');
    expect(result).toEqual({ blob, filename: 'my-chat.json' });
  });

  it('falls back to a generated filename when the header is missing', async () => {
    const blob = new Blob(['# hi']);
    mockInstance.get.mockResolvedValue({ data: blob, headers: {} });
    const result = await exportConversation(UUID);
    expect(result).toEqual({ blob, filename: 'conversation.md' });
  });

  it('falls back to a json-extension filename when the header is missing and format is json', async () => {
    const blob = new Blob(['{}']);
    mockInstance.get.mockResolvedValue({ data: blob, headers: {} });
    const result = await exportConversation(UUID, 'json');
    expect(result).toEqual({ blob, filename: 'conversation.json' });
  });

  it('falls back when the header value is present but malformed', async () => {
    const blob = new Blob(['# hi']);
    mockInstance.get.mockResolvedValue({
      data: blob,
      headers: { 'content-disposition': 'attachment' }, // no filename at all
    });
    const result = await exportConversation(UUID);
    expect(result).toEqual({ blob, filename: 'conversation.md' });
  });
});

describe('representative CRUD calls', () => {
  it('lists conversations with the given query params', async () => {
    mockInstance.get.mockResolvedValue({
      data: { conversations: [], total: 0, limit: 20, offset: 0 },
    });
    const result = await listConversations({ limit: 20, offset: 0, archived: false });
    expect(mockInstance.get).toHaveBeenCalledWith('/chat/conversations', {
      params: { limit: 20, offset: 0, archived: false },
    });
    expect(result.total).toBe(0);
  });

  it('creates a conversation by posting the payload', async () => {
    const payload = { title: 'New chat' };
    mockInstance.post.mockResolvedValue({ data: { uuid: UUID, title: 'New chat' } });
    const result = await createConversation(payload);
    expect(mockInstance.post).toHaveBeenCalledWith('/chat/conversations', payload);
    expect(result.uuid).toBe(UUID);
  });

  it('deletes a conversation by uuid, url-encoded, and resolves', async () => {
    await expect(deleteConversation('a/b')).resolves.toBeUndefined();
    expect(mockInstance.delete).toHaveBeenCalledWith('/chat/conversations/a%2Fb');
  });

  it('lists messages for a conversation with pagination params', async () => {
    mockInstance.get.mockResolvedValue({
      data: { messages: [{ uuid: UUID, role: 'user' }], total: 1, limit: 50, offset: 0 },
    });
    const result = await listMessages(UUID, { limit: 50, offset: 0 });
    expect(mockInstance.get).toHaveBeenCalledWith(`/chat/conversations/${UUID}/messages`, {
      params: { limit: 50, offset: 0 },
    });
    expect(result.total).toBe(1);
  });

  it('creates a project by posting the payload', async () => {
    const payload = { name: 'Project X' };
    mockInstance.post.mockResolvedValue({ data: { uuid: UUID, name: 'Project X' } });
    const result = await createProject(payload);
    expect(mockInstance.post).toHaveBeenCalledWith('/chat/projects', payload);
    expect(result.name).toBe('Project X');
  });
});
