import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/svelte';

const mockAxios = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock('$lib/axios', () => ({ default: mockAxios, isRequestCancelled: () => false }));
vi.mock('$stores/toast', () => ({
  toastStore: { success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() },
}));
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

beforeEach(() => {
  vi.clearAllMocks();
  mockAxios.get.mockImplementation(async (url: string) => ({
    data: url.endsWith('/status') ? STATUS : { running: false },
  }));
});

describe('EmbeddingMigrationSettings by role (issue #1208)', () => {
  it('locked (admin): shows the super-admin reason, disables controls, makes no migration call', async () => {
    const { container } = render(EmbeddingMigrationSettings, { props: { locked: true } });

    expect(await screen.findByText('settings.embeddingMigration.superAdminRequired')).toBeVisible();
    const buttons = Array.from(
      container.querySelectorAll('[data-testid="embedding-migration-locked"] button')
    );
    expect(buttons.length).toBeGreaterThan(0);
    for (const button of buttons) expect(button).toBeDisabled();

    // Nothing may reach an endpoint that would answer 403.
    expect(mockAxios.get).not.toHaveBeenCalled();
    expect(mockAxios.post).not.toHaveBeenCalled();
    expect(screen.queryByText('settings.embeddingMigration.adminRequired')).toBeNull();
  });

  it('locked: ignores migration WebSocket events instead of rendering progress', async () => {
    const { container } = render(EmbeddingMigrationSettings, { props: { locked: true } });
    window.dispatchEvent(
      new CustomEvent('migration-progress', {
        detail: {
          processed_files: 1,
          total_files: 2,
          failed_files: [],
          progress: 50,
          running: true,
        },
      })
    );
    await screen.findByText('settings.embeddingMigration.superAdminRequired');
    expect(container.querySelector('.progress-section')).toBeNull();
  });

  it('unlocked (super admin): loads status and offers an enabled start button', async () => {
    render(EmbeddingMigrationSettings, { props: { locked: false } });

    const start = await screen.findByRole('button', {
      name: 'settings.embeddingMigration.startMigration',
    });
    expect(start).toBeEnabled();
    await waitFor(() => expect(mockAxios.get).toHaveBeenCalledWith('/embeddings/migration/status'));
    expect(screen.queryByText('settings.embeddingMigration.superAdminRequired')).toBeNull();
  });
});
