/**
 * The index overview must say WHICH index could not be read, and must not paint an unreadable
 * index as "Index not created" or as an empty one.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/svelte';

vi.mock('$stores/locale', async () => {
  const { readable } = await import('svelte/store');
  const en = (await import('$lib/i18n/locales/en.json')).default as Record<string, string>;
  const fmt = (key: string, vars?: Record<string, unknown>) =>
    (en[key] ?? key).replace(/\{\{(\w+)\}\}/g, (_m, k: string) => String(vars?.[k] ?? ''));
  return { t: readable(fmt), locale: readable('en') };
});

const http = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock('$lib/axios', () => ({ default: http }));
vi.mock('./OrphanedDataPanel.svelte', async () => ({
  default: (await import('../ui/Spinner.svelte')).default,
}));

import DataIntegritySettings from './DataIntegritySettings.svelte';

beforeEach(() => {
  vi.clearAllMocks();
});

function status(index_overview: unknown) {
  http.get.mockResolvedValue({ data: { running: false, last_run: null, index_overview } });
}

describe('DataIntegritySettings index card titles', () => {
  it('titles a card from its kind code in the UI language, not the server label', async () => {
    status({
      indices: [
        { name: 'my_summaries', kind: 'summaries', label: 'SERVER LABEL', exists: true, total: 1 },
      ],
    });
    render(DataIntegritySettings);

    expect(await screen.findByText('Summaries')).toBeInTheDocument();
    expect(screen.queryByText('SERVER LABEL')).toBeNull();
  });

  it('keeps the server label when the kind is missing or unknown', async () => {
    status({ indices: [{ name: 'idx', label: 'Older Backend Label', exists: true, total: 1 }] });
    render(DataIntegritySettings);

    expect(await screen.findByText('Older Backend Label')).toBeInTheDocument();
  });
});

describe('DataIntegritySettings index overview errors', () => {
  it('names the index whose counts could not be read', async () => {
    status({
      indices: [
        { name: 'speakers_v4', label: 'Speaker Embeddings (V4)', exists: true, error: 'x' },
        { name: 'summaries', label: 'Summaries', exists: true, total: 7 },
      ],
    });
    render(DataIntegritySettings);

    const msg = await screen.findByTestId('index-error-speakers_v4');
    expect(msg.textContent).toContain('speakers_v4');
    expect(screen.queryByTestId('index-error-summaries')).toBeNull();
    expect(screen.queryByText('Index not created')).toBeNull();
  });

  it('shows an unreadable index state instead of "Index not created"', async () => {
    status({
      indices: [
        { name: 'speakers_v3', label: 'Speaker Embeddings (V3)', exists: false, error: 'x' },
      ],
    });
    render(DataIntegritySettings);

    await screen.findByTestId('index-error-speakers_v3');
    expect(screen.queryByText('Index not created')).toBeNull();
  });

  it('explains a whole-overview failure', async () => {
    status({ error: 'OpenSearch not available', indices: [] });
    render(DataIntegritySettings);

    expect(await screen.findByTestId('index-overview-error')).toBeTruthy();
  });
});
