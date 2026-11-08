import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/svelte';

vi.mock('$stores/locale', async () => {
  const { readable } = await import('svelte/store');
  const en = (await import('$lib/i18n/locales/en.json')).default as Record<string, string>;
  const t = (key: string, vars?: Record<string, unknown>) =>
    Object.entries(vars ?? {}).reduce(
      (text, [name, value]) => text.replace(`{{${name}}}`, String(value)),
      en[key] ?? key
    );
  return { t: readable(t), locale: readable('en') };
});

const h = vi.hoisted(() => ({
  loginAsDemo: vi.fn(),
  error: vi.fn(),
  getDemoInfo: vi.fn(),
  getServerVersion: vi.fn(),
}));
vi.mock('$stores/auth', () => ({ loginAsDemo: h.loginAsDemo }));
vi.mock('$stores/toast', () => ({ toastStore: { error: h.error } }));
vi.mock('$lib/api/version', () => ({ getServerVersion: h.getServerVersion }));
vi.mock('$lib/demo/demoApi', () => ({
  getDemoInfo: h.getDemoInfo,
  getDemoAttributions: vi.fn(),
}));

import DemoSignIn from './DemoSignIn.svelte';
import DemoBanner from './DemoBanner.svelte';
import DemoAttribution from './DemoAttribution.svelte';
import { demoAttributions, demoInfo, demoMode } from '$stores/demo';

beforeEach(() => {
  vi.clearAllMocks();
  demoMode.set(false);
  demoInfo.set(null);
  demoAttributions.set({});
  h.getServerVersion.mockResolvedValue({ version: 'v0.7.0', git_sha: '' });
  h.getDemoInfo.mockResolvedValue({
    enabled: true,
    next_reset_at: '2026-11-09T04:00:00Z',
    upgraded_at: '2026-11-08T10:15:00Z',
  });
});

describe('DemoSignIn', () => {
  it('signs in without asking for credentials and tells the page', async () => {
    h.loginAsDemo.mockResolvedValue({ success: true });
    const signedIn = vi.fn();
    render(DemoSignIn, { onSignedIn: signedIn });

    await fireEvent.click(screen.getByRole('button', { name: 'Try the demo' }));

    await waitFor(() => expect(signedIn).toHaveBeenCalledTimes(1));
    expect(h.loginAsDemo).toHaveBeenCalledTimes(1);
    expect(h.error).not.toHaveBeenCalled();
  });

  it('shows the failure and does not claim a sign-in', async () => {
    h.loginAsDemo.mockResolvedValue({ success: false, message: 'The demo is down' });
    const signedIn = vi.fn();
    render(DemoSignIn, { onSignedIn: signedIn });

    await fireEvent.click(screen.getByRole('button', { name: 'Try the demo' }));

    await waitFor(() => expect(h.error).toHaveBeenCalledWith('The demo is down'));
    expect(signedIn).not.toHaveBeenCalled();
  });

  it('shows the version and when the demo was upgraded', async () => {
    render(DemoSignIn);

    const info = await screen.findByTestId('demo-build-info');
    expect(info.textContent).toMatch(/^Version v0\.7\.0, upgraded .*2026/);
  });

  it('shows only the version when no upgrade time was recorded', async () => {
    h.getDemoInfo.mockResolvedValue({ enabled: true, next_reset_at: null, upgraded_at: null });
    render(DemoSignIn);

    const info = await screen.findByTestId('demo-build-info');
    expect(info.textContent).toBe('Version v0.7.0');
  });

  it('is disabled while another sign-in is in flight', () => {
    render(DemoSignIn, { disabled: true });

    expect((screen.getByRole('button') as HTMLButtonElement).disabled).toBe(true);
  });
});

describe('DemoBanner', () => {
  it('renders nothing outside a demo, and asks the server nothing', () => {
    render(DemoBanner);

    expect(screen.queryByTestId('demo-banner')).toBeNull();
    expect(h.getDemoInfo).not.toHaveBeenCalled();
  });

  it('says it is read-only and when the data resets', async () => {
    demoMode.set(true);
    render(DemoBanner);

    const banner = await screen.findByTestId('demo-banner');
    await waitFor(() => expect(banner.textContent).toContain('resets on'));
    expect(banner.textContent).toContain('read-only demo');
    expect(banner.textContent).toContain('2026');
  });
});

describe('DemoAttribution', () => {
  const item = {
    file_uuid: 'f-1',
    license: 'CC-BY-4.0',
    license_url: 'https://creativecommons.org/licenses/by/4.0/',
    attribution: 'AMI Corpus',
    source_url: 'https://groups.inf.ed.ac.uk/ami/corpus/',
  };

  it('credits the work and links its licence and source', () => {
    demoAttributions.set({ 'f-1': item });
    render(DemoAttribution, { fileUuid: 'f-1' });

    const block = screen.getByTestId('demo-attribution');
    expect(block.textContent).toContain('Credit: AMI Corpus.');
    const licence = screen.getByRole('link', { name: 'CC-BY-4.0' });
    expect(licence.getAttribute('href')).toBe(item.license_url);
    expect(screen.getByRole('link', { name: 'Original source' }).getAttribute('href')).toBe(
      item.source_url
    );
  });

  it('renders nothing for a file with no recorded licence', () => {
    demoAttributions.set({ 'f-1': item });
    render(DemoAttribution, { fileUuid: 'someone-elses' });

    expect(screen.queryByTestId('demo-attribution')).toBeNull();
  });

  it('never links a non-https address from the catalogue', () => {
    demoAttributions.set({
      'f-1': { ...item, license_url: 'javascript:alert(1)', source_url: 'http://example.com/' },
    });
    render(DemoAttribution, { fileUuid: 'f-1' });

    expect(screen.queryAllByRole('link')).toEqual([]);
    expect(screen.getByTestId('demo-attribution').textContent).toContain('CC-BY-4.0');
  });
});
