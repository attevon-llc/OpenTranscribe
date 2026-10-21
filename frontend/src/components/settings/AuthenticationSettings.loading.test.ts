/**
 * Authentication settings: click-to-content behaviour (#1243 item 6).
 *
 * The panel used to import all twelve tab panels statically (the biggest module graph of any
 * section), mount the Local form before its configuration arrived (painting coded defaults,
 * fetching the password policy, then unmounting and fetching it again), and replace the whole tab
 * with the words "Loading configuration..." after every save.
 */
import { describe, it, expect, vi, beforeAll, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/svelte';
import { readFileSync } from 'fs';
import { resolve } from 'path';

vi.mock('$lib/api/authConfig', () => ({
  AuthConfigApi: { getAllConfigs: vi.fn(), updateCategory: vi.fn(), testConnection: vi.fn() },
}));

vi.mock('$lib/passwordPolicy', () => ({
  fetchPasswordPolicy: vi.fn().mockRejectedValue(new Error('offline')),
}));

vi.mock('$stores/toast', () => ({ toastStore: { success: vi.fn(), error: vi.fn() } }));

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

import { AuthConfigApi } from '$lib/api/authConfig';
import { fetchPasswordPolicy } from '$lib/passwordPolicy';
import AuthenticationSettings from './AuthenticationSettings.svelte';
import { clearSectionData, setSectionData, SectionDataKey } from '$lib/settings/sectionData';
import { authTabLoaders } from '$lib/settings/sectionLoaders';

const getAll = vi.mocked(AuthConfigApi.getAllConfigs);

function deferred<T>() {
  let resolveFn!: (v: T) => void;
  const promise = new Promise<T>((res) => (resolveFn = res));
  return { promise, resolve: resolveFn };
}

const SAVE = { name: 'settings.localAuth.saveConfiguration' };

describe('AuthenticationSettings loading', () => {
  // The Local panel is a lazy chunk and vitest transforms a cold import on demand: do it once up
  // front so the first test's 5 s budget measures the component (as SettingsModal.test.ts does).
  beforeAll(async () => {
    await authTabLoaders.local();
  }, 120_000);

  beforeEach(() => {
    vi.clearAllMocks();
    // The remembered configuration is module state: one test's answer must not paint the next.
    clearSectionData();
  });

  it('does not mount the Local form (and its defaults) before the configuration lands', async () => {
    const pending = deferred<Record<string, never[]>>();
    getAll.mockReturnValue(pending.promise as never);
    render(AuthenticationSettings);

    // The panel chunk lands first, then it waits for the configuration behind a skeleton.
    expect(
      await screen.findByRole(
        'status',
        { name: 'settings.authentication.loadingConfig' },
        { timeout: 5000 }
      )
    ).toBeInTheDocument();
    expect(screen.queryByRole('button', SAVE)).toBeNull();

    pending.resolve({ local: [] });
    expect(await screen.findByRole('button', SAVE, { timeout: 5000 })).toBeInTheDocument();
  });

  it('paints a remembered configuration at once, behind no skeleton, and refreshes it', async () => {
    setSectionData(SectionDataKey.authConfig, { local: [] });
    const pending = deferred<Record<string, never[]>>();
    getAll.mockReturnValue(pending.promise as never);
    render(AuthenticationSettings);

    expect(await screen.findByRole('button', SAVE, { timeout: 5000 })).toBeInTheDocument();
    expect(
      screen.queryByRole('status', { name: 'settings.authentication.loadingConfig' })
    ).toBeNull();
    // The refresh is still in flight behind the painted form.
    expect(getAll).toHaveBeenCalledTimes(1);
    pending.resolve({ local: [] });
  });

  it('reads the password policy once, not once per mount', async () => {
    getAll.mockResolvedValue({ local: [] } as never);
    render(AuthenticationSettings);
    await screen.findByRole('button', SAVE, { timeout: 5000 });
    await new Promise((r) => setTimeout(r, 50));
    expect(vi.mocked(fetchPasswordPolicy)).toHaveBeenCalledTimes(1);
  });

  it('keeps the form on screen while it re-reads the configuration after a save', async () => {
    getAll.mockResolvedValueOnce({ local: [] } as never);
    render(AuthenticationSettings);
    const save = await screen.findByRole('button', SAVE, { timeout: 5000 });

    const again = deferred<Record<string, never[]>>();
    getAll.mockReturnValueOnce(again.promise as never);
    await fireEvent.click(save);
    await waitFor(() => expect(AuthConfigApi.updateCategory).toHaveBeenCalled());
    await new Promise((r) => setTimeout(r, 30));
    // The button reads "saving" for a moment after the click, so match either label.
    expect(
      screen.getByRole('button', { name: /settings\.localAuth\.saveConfiguration|common\.saving/ })
    ).toBeInTheDocument();
    again.resolve({ local: [] });
  });
});

describe('AuthenticationSettings module graph', () => {
  it('imports no tab panel statically (each loads when its tab opens)', () => {
    const src = readFileSync(resolve(__dirname, 'AuthenticationSettings.svelte'), 'utf8');
    const staticPanels = [...src.matchAll(/^\s*import\s+\w+\s+from\s+'\.\/(\w+)\.svelte';/gm)].map(
      (m) => m[1]
    );
    // LazySection is the loader itself; every real panel must come through authTabLoaders.
    expect(staticPanels.filter((name) => name !== 'LazySection')).toEqual([]);
  });
});
