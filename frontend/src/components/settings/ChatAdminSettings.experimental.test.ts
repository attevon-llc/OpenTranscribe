/**
 * The admin chat-settings panel's "Experimental (measurement-gated)"
 * subsection (issue #52+). No experimental `chat.*` flag exists yet (see
 * `backend/app/core/chat_flag_registry.py`), so today this renders
 * explanatory copy rather than controls — but the disabled/no-provider hint
 * it will gate a real control with must already work, or the first flag
 * added there has to build the gating mechanism from scratch instead of
 * just adding a registry entry.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render } from '@testing-library/svelte';
import { writable } from 'svelte/store';
import type { LLMStatusState } from '$stores/llmStatus';
import type { ChatAdminSettings as ChatAdminSettingsType } from '$lib/types/chat';

vi.mock('$lib/api/chatApi', () => ({
  getChatAdminSettings: vi.fn(),
  updateChatAdminSettings: vi.fn(),
}));

const llmState = writable<LLMStatusState>({
  status: null,
  available: false,
  checking: false,
  lastChecked: null,
});

vi.mock('$stores/llmStatus', () => ({
  llmStatusStore: {
    subscribe: (run: (value: LLMStatusState) => void) => llmState.subscribe(run),
    initialize: vi.fn().mockResolvedValue(undefined),
  },
}));

vi.mock('$stores/settingsModalStore', () => ({
  settingsModalStore: { setDirty: vi.fn() },
}));

vi.mock('$stores/toast', () => ({
  toastStore: { success: vi.fn(), error: vi.fn() },
}));

import { getChatAdminSettings } from '$lib/api/chatApi';
import ChatAdminSettings from './ChatAdminSettings.svelte';

const SETTINGS: ChatAdminSettingsType = {
  candidate_pool: 48,
  final_chunks: 12,
  max_chunks_per_file: 4,
  rerank_enabled: true,
  rerank_max_pairs: 50,
  query_rewrite_enabled: true,
  cache_ttl_seconds: 300,
  semantic_cache_enabled: false,
  semantic_cache_threshold: 0.97,
  history_max_turns: 10,
  messages_per_hour: 120,
  max_concurrent_streams: 2,
  retention_days: 0,
};

describe('ChatAdminSettings — Experimental subsection', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(getChatAdminSettings).mockResolvedValue(SETTINGS);
    llmState.set({ status: null, available: false, checking: false, lastChecked: null });
  });

  it('renders the Experimental section', async () => {
    const { findByTestId } = render(ChatAdminSettings);
    expect(await findByTestId('chat-admin-experimental')).toBeTruthy();
  });

  it('shows the no-provider hint when $llmStatus reports unavailable', async () => {
    llmState.set({ status: null, available: false, checking: false, lastChecked: null });
    const { findByTestId } = render(ChatAdminSettings);
    expect(await findByTestId('chat-admin-experimental-hint')).toBeTruthy();
  });

  it('hides the no-provider hint once $llmStatus reports an available provider', async () => {
    // The control: without it, a hint that always renders would pass the
    // test above trivially.
    llmState.set({ status: null, available: true, checking: false, lastChecked: null });
    const { findByTestId, queryByTestId } = render(ChatAdminSettings);
    await findByTestId('chat-admin-experimental');
    expect(queryByTestId('chat-admin-experimental-hint')).toBeNull();
  });

  it('reacts live to a status change after mount', async () => {
    const { findByTestId, queryByTestId } = render(ChatAdminSettings);
    await findByTestId('chat-admin-experimental-hint');

    llmState.set({ status: null, available: true, checking: false, lastChecked: null });

    // Svelte's reactive statement re-runs synchronously on the next tick.
    await new Promise((r) => setTimeout(r, 0));
    expect(queryByTestId('chat-admin-experimental-hint')).toBeNull();
  });
});
