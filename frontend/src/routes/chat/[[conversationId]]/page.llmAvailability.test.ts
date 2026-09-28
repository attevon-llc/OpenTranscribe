/**
 * Chat decides "is an LLM available" from the deployment-wide status
 * (`GET /llm/status` via `llmStatusStore`), never from the per-user
 * `/llm-settings/*` router — which is not mounted at all when the
 * `llm.user_settings` capability is disabled and the provider comes from env
 * (issue #1046). With that router 404ing and `/llm/status` reporting available,
 * the composer must be usable, the empty state must not ask the user to connect
 * a provider, and the token panel must still learn the model's context window.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/svelte';

const mockAxios = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  patch: vi.fn(),
  delete: vi.fn(),
}));
vi.mock('$lib/axios', () => ({ default: mockAxios, isRequestCancelled: () => false }));

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

vi.mock('$app/navigation', () => ({ goto: vi.fn(), beforeNavigate: vi.fn() }));

function noopComponent() {
  return () => {};
}
vi.mock('$components/chat/ChatSidebar.svelte', () => ({ default: noopComponent() }));
vi.mock('$components/chat/ProjectModal.svelte', () => ({ default: noopComponent() }));
vi.mock('$components/chat/ChatThread.svelte', () => ({ default: noopComponent() }));
vi.mock('$components/chat/ChatContextBar.svelte', () => ({ default: noopComponent() }));
vi.mock('$components/chat/ChatControlsPanel.svelte', () => ({ default: noopComponent() }));
vi.mock('$components/chat/ChatTracePanel.svelte', () => ({ default: noopComponent() }));
vi.mock('$components/chat/FilePickerModal.svelte', () => ({ default: noopComponent() }));
vi.mock('$components/chat/LargeSelectionWarningModal.svelte', () => ({
  default: noopComponent(),
}));
vi.mock('$components/RetrievalQualityNotice.svelte', () => ({ default: noopComponent() }));

// Records the live props object so the test can read what the page passed.
const tokenPanelProps = vi.hoisted(() => ({ current: null as null | { contextWindow: number } }));
vi.mock('$components/chat/TokenUsagePanel.svelte', () => ({
  default: (_anchor: unknown, props: { contextWindow: number }) => {
    tokenPanelProps.current = props;
  },
}));

import Page from './+page.svelte';
import { llmStatusStore } from '$stores/llmStatus';
import { llmService } from '$lib/services/llmService';
import { capabilities } from '$stores/capabilities';

const SYSTEM_STATUS = {
  available: true,
  user_id: '1',
  provider: 'bedrock',
  model: 'us.anthropic.claude-haiku-4-5-20251001-v1:0',
  context_window: 200000,
  message: 'LLM service is available and configured',
};

function notFound(url: string) {
  return Promise.reject(
    Object.assign(new Error(`404 ${url}`), { response: { status: 404, data: {} } })
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  llmService.clearCache();
  llmStatusStore.reset();
  tokenPanelProps.current = null;
  capabilities.update((s) => ({
    ...s,
    capabilities: { ...s.capabilities, 'llm.user_settings': false },
  }));

  mockAxios.get.mockImplementation((url: string) => {
    if (url === '/llm/status') return Promise.resolve({ data: SYSTEM_STATUS });
    if (url.startsWith('/llm-settings')) return notFound(url);
    if (url === '/chat/conversations') {
      return Promise.resolve({ data: { conversations: [], total: 0 } });
    }
    if (url === '/chat/projects') return Promise.resolve({ data: { projects: [] } });
    return Promise.resolve({ data: {} });
  });
  mockAxios.post.mockResolvedValue({ data: {} });
});

afterEach(() => {
  llmStatusStore.reset();
});

async function renderChat() {
  // The root layout initializes the shared status for every signed-in page.
  await llmStatusStore.initialize();
  return render(Page, { props: { data: { conversationId: null, fileUuids: [] } as never } });
}

describe('chat LLM availability with per-user LLM settings unmounted', () => {
  it('enables the composer and shows suggestions, not the connect-a-provider CTA', async () => {
    await renderChat();

    expect(screen.getByTestId('chat-composer-input')).not.toBeDisabled();
    expect(screen.queryByTestId('chat-open-llm-settings')).toBeNull();
    expect(screen.queryByText('chat.setup.noLlmTitle')).toBeNull();
    expect(screen.getAllByTestId('chat-suggestion').length).toBeGreaterThan(0);
  });

  it('takes the context window from /llm/status and never calls /llm-settings/status', async () => {
    await renderChat();

    await waitFor(() => expect(tokenPanelProps.current?.contextWindow).toBe(200000));
    const urls = mockAxios.get.mock.calls.map(([url]) => url);
    expect(urls).not.toContain('/llm-settings/status');
  });
});
