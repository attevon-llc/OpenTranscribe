import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/svelte';

const mockAxios = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
const toast = vi.hoisted(() => ({
  success: vi.fn(),
  error: vi.fn(),
  info: vi.fn(),
  warning: vi.fn(),
}));
vi.mock('$lib/axios', () => ({ default: mockAxios, isRequestCancelled: () => false }));
vi.mock('$stores/toast', () => ({ toastStore: toast }));
vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (
      run: (value: (key: string, params?: Record<string, unknown>) => string) => void
    ) => (run((k, params) => (params ? `${k}:${JSON.stringify(params)}` : k)), () => {}),
  },
}));

import EmbeddingMigrationSettings from './EmbeddingMigrationSettings.svelte';

const STATUS = {
  current_mode: 'v3',
  migration_needed: true,
  v3_document_count: 10,
  v4_document_count: 0,
  completed_file_count: 4,
};

function serve(policySkipped: Record<string, unknown> | undefined) {
  mockAxios.get.mockImplementation(async (url: string) => ({
    data: url.endsWith('/status')
      ? { ...STATUS, ...(policySkipped ? { policy_skipped: policySkipped } : {}) }
      : { running: false },
  }));
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe('EmbeddingMigrationSettings and the voiceprint switches (issue #1280)', () => {
  it('warns before starting when some recordings will be skipped', async () => {
    serve({ files_total: 4, files_skipped: 3, scopes_total: 2, scopes_skipped: 1 });
    render(EmbeddingMigrationSettings, { props: { locked: false } });

    const notice = await screen.findByTestId('embedding-migration-policy-skip');

    expect(notice.textContent).toContain(
      'settings.embeddingMigration.policySkipNotice:{"files":3,"scopes":1}'
    );
  });

  it('shows no warning when nothing is skipped', async () => {
    serve({ files_total: 4, files_skipped: 0, scopes_total: 1, scopes_skipped: 0 });
    render(EmbeddingMigrationSettings, { props: { locked: false } });

    await screen.findByRole('button', { name: 'settings.embeddingMigration.startMigration' });

    expect(screen.queryByTestId('embedding-migration-policy-skip')).toBeNull();
  });

  it('explains a 409 voiceprints_disabled instead of calling it a failed start', async () => {
    serve(undefined);
    mockAxios.post.mockRejectedValue({
      response: {
        status: 409,
        data: { detail: { code: 'voiceprints_disabled', locked_by: 'organization' } },
      },
    });
    render(EmbeddingMigrationSettings, { props: { locked: false } });

    await fireEvent.click(
      await screen.findByRole('button', { name: 'settings.embeddingMigration.startMigration' })
    );

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith('settings.embeddingMigration.voiceprintsDisabled')
    );
  });

  it('still reports any other start failure as a failed start', async () => {
    serve(undefined);
    mockAxios.post.mockRejectedValue({ response: { status: 500, data: {} } });
    render(EmbeddingMigrationSettings, { props: { locked: false } });

    await fireEvent.click(
      await screen.findByRole('button', { name: 'settings.embeddingMigration.startMigration' })
    );

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith('settings.embeddingMigration.migrationStartFailed')
    );
  });
});
