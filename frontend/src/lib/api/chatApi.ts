/**
 * Typed API client for RAG chat (issue #52).
 *
 * Mirrors the backend Pydantic schemas. Everything except the streaming send
 * goes through the shared axiosInstance (cookie auth, CSRF, 401 refresh); the
 * streaming send lives in `chatStream.ts` because it needs raw fetch to read a
 * ReadableStream.
 */

import axiosInstance from '$lib/axios';
import type {
  ChatAdminSettings,
  ChatProject,
  ChatProjectDetail,
  ChatProjectList,
  ChatScope,
  ChatUserSettings,
  Conversation,
  ConversationList,
  ConversationSettings,
  ContextEstimate,
  MessageList,
} from '$lib/types/chat';

export interface ListConversationsParams {
  limit?: number;
  offset?: number;
  q?: string;
  archived?: boolean;
}

export async function listConversations(
  params: ListConversationsParams = {}
): Promise<ConversationList> {
  const { data } = await axiosInstance.get<ConversationList>('/chat/conversations', { params });
  return data;
}

export interface CreateConversationPayload {
  title?: string;
  scope?: ChatScope;
  llm_config_uuid?: string | null;
  settings?: ConversationSettings;
  /** Joining a project makes the chat inherit its scope and prompt layer. */
  project_uuid?: string | null;
}

export async function createConversation(
  payload: CreateConversationPayload = {}
): Promise<Conversation> {
  const { data } = await axiosInstance.post<Conversation>('/chat/conversations', payload);
  return data;
}

export async function getConversation(uuid: string): Promise<Conversation> {
  const { data } = await axiosInstance.get<Conversation>(
    `/chat/conversations/${encodeURIComponent(uuid)}`
  );
  return data;
}

export interface UpdateConversationPayload {
  title?: string;
  is_archived?: boolean;
  scope?: ChatScope;
  llm_config_uuid?: string | null;
  settings?: ConversationSettings;
}

export async function updateConversation(
  uuid: string,
  patch: UpdateConversationPayload
): Promise<Conversation> {
  const { data } = await axiosInstance.patch<Conversation>(
    `/chat/conversations/${encodeURIComponent(uuid)}`,
    patch
  );
  return data;
}

export async function deleteConversation(uuid: string): Promise<void> {
  await axiosInstance.delete(`/chat/conversations/${encodeURIComponent(uuid)}`);
}

export async function listMessages(
  uuid: string,
  params: { limit?: number; offset?: number } = {}
): Promise<MessageList> {
  const { data } = await axiosInstance.get<MessageList>(
    `/chat/conversations/${encodeURIComponent(uuid)}/messages`,
    { params }
  );
  return data;
}

/**
 * Ask the server to stop an in-flight generation.
 *
 * Belt-and-braces beside aborting the fetch: on a flaky connection the abort may
 * never reach the server, so this sets a flag the generator polls.
 */
export async function cancelMessage(messageUuid: string): Promise<void> {
  await axiosInstance.post(`/chat/messages/${encodeURIComponent(messageUuid)}/cancel`);
}

/**
 * Download a conversation as Markdown or JSON.
 *
 * Fetched as a blob and saved client-side rather than navigating to the URL:
 * the endpoint is cookie-authenticated and a plain link would lose the axios
 * auth/refresh handling.
 */
export async function exportConversation(
  uuid: string,
  format: 'markdown' | 'json' = 'markdown'
): Promise<{ blob: Blob; filename: string }> {
  const response = await axiosInstance.get(
    `/chat/conversations/${encodeURIComponent(uuid)}/export`,
    { params: { format }, responseType: 'blob' }
  );

  const disposition = String(response.headers?.['content-disposition'] ?? '');
  const match = disposition.match(/filename="?([^"]+)"?/);
  const filename = match?.[1] ?? `conversation.${format === 'json' ? 'json' : 'md'}`;
  return { blob: response.data as Blob, filename };
}

export async function estimateContext(scope: ChatScope): Promise<ContextEstimate> {
  const { data } = await axiosInstance.post<ContextEstimate>('/chat/context/estimate', scope);
  return data;
}

// --- Settings ---------------------------------------------------------------

export async function getChatUserSettings(): Promise<ChatUserSettings> {
  const { data } = await axiosInstance.get<ChatUserSettings>('/user-settings/chat');
  return data;
}

export async function updateChatUserSettings(
  patch: Partial<ChatUserSettings>
): Promise<ChatUserSettings> {
  const { data } = await axiosInstance.put<ChatUserSettings>('/user-settings/chat', patch);
  return data;
}

export async function getChatAdminSettings(): Promise<ChatAdminSettings> {
  const { data } = await axiosInstance.get<ChatAdminSettings>('/admin/chat-settings');
  return data;
}

export async function updateChatAdminSettings(
  patch: Partial<ChatAdminSettings>
): Promise<ChatAdminSettings> {
  const { data } = await axiosInstance.put<ChatAdminSettings>('/admin/chat-settings', patch);
  return data;
}

// --- Projects (issue #360) -------------------------------------------------

export interface ProjectPayload {
  name?: string;
  description?: string | null;
  system_prompt?: string | null;
  scope?: ChatScope;
  llm_config_uuid?: string | null;
  is_archived?: boolean;
}

export async function listProjects(includeArchived = false): Promise<ChatProjectList> {
  const { data } = await axiosInstance.get<ChatProjectList>('/chat/projects', {
    params: { include_archived: includeArchived },
  });
  return data;
}

export async function getProject(uuid: string): Promise<ChatProjectDetail> {
  const { data } = await axiosInstance.get<ChatProjectDetail>(`/chat/projects/${uuid}`);
  return data;
}

export async function createProject(payload: ProjectPayload): Promise<ChatProjectDetail> {
  const { data } = await axiosInstance.post<ChatProjectDetail>('/chat/projects', payload);
  return data;
}

export async function updateProject(
  uuid: string,
  payload: ProjectPayload
): Promise<ChatProjectDetail> {
  const { data } = await axiosInstance.patch<ChatProjectDetail>(`/chat/projects/${uuid}`, payload);
  return data;
}

/** Deletes the project only — its conversations survive, ungrouped. */
export async function deleteProject(uuid: string): Promise<void> {
  await axiosInstance.delete(`/chat/projects/${uuid}`);
}
