/**
 * Organization Context used to be the one settings section that ended with a large tinted
 * "How it works" card. Every other section keeps its explanation in the description under the
 * title and/or an InfoTip beside it, so this one now does the same.
 */
import { describe, it, expect, vi, beforeAll, beforeEach } from 'vitest';
import { render, fireEvent, waitFor, screen } from '@testing-library/svelte';

const mockAxios = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock('$lib/axios', () => ({ default: mockAxios, isRequestCancelled: () => false }));

vi.mock('$stores/auth', async () => {
  const { writable } = await import('svelte/store');
  return { user: writable<{ role: string } | null>(null), readAccountLifecycle: () => null };
});

vi.mock('$stores/toast', () => ({ toastStore: { success: vi.fn(), error: vi.fn() } }));

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
  locale: {
    subscribe: (run: (value: string) => void) => {
      run('en');
      return () => {};
    },
  },
}));

vi.mock('$lib/api/organizationContext', () => ({
  getOrganizationContext: vi.fn().mockResolvedValue({
    context_text: 'Acme',
    include_in_default_prompts: true,
    include_in_custom_prompts: false,
    is_shared: false,
    using_shared_from: null,
  }),
  getSharedOrganizationContexts: vi.fn().mockResolvedValue({ shared_contexts: [] }),
  updateOrganizationContext: vi.fn(),
  resetOrganizationContext: vi.fn(),
  useSharedOrganizationContext: vi.fn(),
}));

vi.mock('$lib/settings/sectionLoaders', async (importOriginal) => {
  const actual = await importOriginal<typeof import('$lib/settings/sectionLoaders')>();
  return { ...actual, startIdlePrefetch: vi.fn(() => vi.fn()) };
});

import SettingsModal from './SettingsModal.svelte';
import { user as mockUser } from '$stores/auth';
import { settingsModalStore } from '$stores/settingsModalStore';
import { capabilities } from '$stores/capabilities';
import { resetAppStores } from '../test-mocks/app-stores';
import { sectionLoaders } from '$lib/settings/sectionLoaders';

beforeAll(async () => {
  await Promise.all(Object.values(sectionLoaders).map((load) => load()));
}, 180_000);

beforeEach(() => {
  vi.clearAllMocks();
  resetAppStores();
  settingsModalStore.reset();
  capabilities.set({
    edition: 'community',
    loaded: true,
    capabilities: {},
    audience: {},
    maxUploadBytes: undefined,
  });
  mockAxios.get.mockResolvedValue({ data: {} });
  (mockUser as unknown as { set: (v: unknown) => void }).set({ role: 'user' });
});

async function openOrgContext() {
  settingsModalStore.open('organization-context');
  const view = render(SettingsModal);
  await waitFor(() => expect(view.container.querySelector('#org-context-text')).not.toBeNull(), {
    timeout: 10_000,
  });
  return view;
}

describe('Organization Context help text', () => {
  it('has no tinted "How it works" card at the bottom of the form', async () => {
    const { container } = await openOrgContext();
    expect(container.querySelector('.info-box')).toBeNull();
  });

  it('keeps one description line under the title', async () => {
    const { container } = await openOrgContext();
    const title = container.querySelector('.settings-content .section-title');
    expect(title?.textContent).toBe('settings.orgContext.title');
    expect(container.querySelector('.settings-content .section-description')?.textContent).toBe(
      'settings.orgContext.description'
    );
  });

  it('puts the four-point explanation in an InfoTip beside the title', async () => {
    const { container } = await openOrgContext();
    const title = container.querySelector('.settings-content .section-title') as HTMLElement;
    const trigger = screen.getByRole('button', { name: 'settings.orgContext.howItWorks' });
    expect(title.parentElement?.contains(trigger)).toBe(true);

    const panel = document.getElementById(trigger.getAttribute('aria-describedby') as string);
    expect(Array.from(panel?.querySelectorAll('li') ?? []).map((li) => li.textContent)).toEqual([
      'settings.orgContext.howItWorks1',
      'settings.orgContext.howItWorks2',
      'settings.orgContext.howItWorks3',
      'settings.orgContext.howItWorks4',
    ]);

    await fireEvent.click(trigger);
    expect(trigger).toHaveAttribute('aria-expanded', 'true');
  });
});
