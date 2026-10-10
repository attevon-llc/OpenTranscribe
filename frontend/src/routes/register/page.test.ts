/**
 * A rejected registration used to surface the same message twice at once: the
 * inline `role="alert"` paragraph AND a toast carrying identical text, so an
 * e2e `get_by_text("Email already registered")` resolved to two elements. The
 * inline alert is the persistent, untruncated one, so it is the single channel.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, fireEvent, waitFor } from '@testing-library/svelte';

const mockRegister = vi.hoisted(() => vi.fn());
const mockGetAuthMethods = vi.hoisted(() => vi.fn());

vi.mock('$stores/auth', () => ({
  register: mockRegister,
  login: vi.fn(),
  getAuthMethods: mockGetAuthMethods,
}));

const mockToast = vi.hoisted(() => ({ error: vi.fn(), success: vi.fn(), info: vi.fn() }));
vi.mock('$stores/toast', () => ({ toastStore: mockToast }));

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

vi.mock('$lib/passwordPolicy', async (importOriginal) => ({
  ...(await importOriginal<typeof import('$lib/passwordPolicy')>()),
  loadPasswordPolicy: vi
    .fn()
    .mockResolvedValue(
      (await importOriginal<typeof import('$lib/passwordPolicy')>()).FALLBACK_PASSWORD_POLICY
    ),
}));

import Page from './+page.svelte';

beforeEach(() => {
  vi.clearAllMocks();
  mockGetAuthMethods.mockResolvedValue({ allow_registration: true });
});

describe('register page: rejected registration', () => {
  it('shows the server message once, inline, and does not also toast it', async () => {
    mockRegister.mockResolvedValue({ success: false, message: 'Email already registered' });
    const { container, findByText } = render(Page);

    const fill = async (selector: string, value: string) => {
      const el = (await waitFor(() => {
        const found = container.querySelector(selector);
        if (!found) throw new Error(`${selector} not rendered`);
        return found;
      })) as HTMLInputElement;
      await fireEvent.input(el, { target: { value } });
    };
    await fill('#username', 'someone');
    await fill('#email', 'admin@example.com');
    await fill('#password', 'ValidPassword123!');
    await fill('#confirmPassword', 'ValidPassword123!');

    await fireEvent.submit(container.querySelector('form') as HTMLFormElement);

    const alert = await findByText('Email already registered');
    expect(alert.getAttribute('role')).toBe('alert');
    expect(mockToast.error).not.toHaveBeenCalledWith('Email already registered');
  });
});
