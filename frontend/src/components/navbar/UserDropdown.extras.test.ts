/**
 * The user menu hosts entries contributed by the UserMenuExtras seam component.
 * Pins: nothing renders outside the managed edition, and choosing an extra entry
 * closes the menu and dismisses like a built-in item.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/svelte';

const edition = vi.hoisted(() => ({ isCloudEdition: true }));
vi.mock('$lib/edition', () => edition);
vi.mock('$lib/cloud/components/UserMenuExtras.svelte', async () => ({
  default: (await import('./UserMenuExtrasFixture.svelte')).default,
}));
vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

import UserDropdown from './UserDropdown.svelte';

async function openMenu() {
  const result = render(UserDropdown, { user: { email: 'a@example.com', role: 'user' } });
  await fireEvent.click(result.container.querySelector('.user-button')!);
  return result;
}

describe('UserDropdown extras seam', () => {
  beforeEach(() => {
    edition.isCloudEdition = true;
  });

  it('renders extra entries inside the menu', async () => {
    await openMenu();
    expect(screen.getByTestId('extras-item').closest('.dropdown-menu')).not.toBeNull();
  });

  it('closes the menu when an extra entry is chosen', async () => {
    await openMenu();

    await fireEvent.click(screen.getByTestId('extras-item'));

    expect(screen.queryByTestId('extras-item')).toBeNull();
  });

  it('renders no extra entries outside the managed edition', async () => {
    edition.isCloudEdition = false;
    await openMenu();
    expect(screen.queryByTestId('extras-item')).toBeNull();
  });
});
