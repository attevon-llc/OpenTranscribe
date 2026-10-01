/**
 * The Flower entry is admin-only and also requires the deployment to expose
 * Flower (`admin.flower`); the reverse-proxy auth probe denies it otherwise.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, fireEvent } from '@testing-library/svelte';

vi.mock('$lib/edition', () => ({ isCloudEdition: false }));
vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

import UserDropdown from './UserDropdown.svelte';
import { capabilities, resetCapabilities } from '$stores/capabilities';

async function openAsAdmin() {
  const result = render(UserDropdown, { user: { email: 'a@example.com', role: 'admin' } });
  await fireEvent.click(result.container.querySelector('.user-button')!);
  return result;
}

afterEach(() => resetCapabilities());

describe('UserDropdown Flower entry', () => {
  it('is offered to admins by default', async () => {
    const { container } = await openAsAdmin();
    expect(container.querySelector('[aria-label="nav.flowerDashboard"]')?.textContent?.trim()).toBe(
      'nav.flowerDashboard'
    );
  });

  it('is hidden when the deployment turns admin.flower off', async () => {
    capabilities.set({
      edition: 'community',
      loaded: true,
      capabilities: { 'admin.flower': false },
      audience: {},
      maxUploadBytes: undefined,
      apiMediatedUploadEnabled: true,
    });
    const { container } = await openAsAdmin();
    expect(container.querySelector('[aria-label="nav.flowerDashboard"]')).toBeNull();
  });
});
