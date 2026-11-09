/**
 * `users/PasswordResetModal.svelte`: the admin's "set a new password" dialog, split
 * out of `UserManagementTable.svelte`. The table's own suite drives it end to end;
 * this pins the client-side checks and the request in isolation. `$lib/axios` is
 * mocked at the transport boundary only.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, fireEvent, waitFor } from '@testing-library/svelte';

const mockAxios = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
}));
vi.mock('$lib/axios', () => ({ default: mockAxios, isRequestCancelled: () => false }));

vi.mock('$stores/toast', async () => (await import('$testMocks/serviceMocks')).toastStoreMock());

vi.mock('$stores/locale', async () =>
  (await import('$testMocks/localeMocks')).keyLocale({ code: 'en' })
);

import PasswordResetModal from './PasswordResetModal.svelte';
import { toastStore } from '$stores/toast';

const target = { uuid: 'u-1', email: 'alice@example.com', full_name: 'Alice Example' };

function renderModal(onClose = vi.fn()) {
  const view = render(PasswordResetModal, { props: { target, passwordMinLength: 12, onClose } });
  return { ...view, onClose };
}

async function fill(
  getByLabelText: (text: string) => HTMLElement,
  password: string,
  confirm: string
) {
  await fireEvent.input(getByLabelText('userManagement.newPassword'), {
    target: { value: password },
  });
  await fireEvent.input(getByLabelText('userManagement.confirmPassword'), {
    target: { value: confirm },
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  mockAxios.put.mockResolvedValue({ data: {} });
});

describe('PasswordResetModal', () => {
  it('names the account and keeps submit disabled until both fields are filled', async () => {
    const { getByText, getByLabelText } = renderModal();
    expect(getByText('Alice Example')).toBeInTheDocument();

    const submit = getByText('userManagement.resetPasswordButton').closest('button');
    expect(submit).toBeDisabled();
    await fill(getByLabelText, 'abcdefghijkl', '');
    expect(submit).toBeDisabled();
    await fill(getByLabelText, 'abcdefghijkl', 'abcdefghijkl');
    expect(submit).not.toBeDisabled();
  });

  it('refuses a mismatch and a password under the server minimum without a request', async () => {
    const { getByText, getByLabelText } = renderModal();

    await fill(getByLabelText, 'abcdefghijkl', 'abcdefghijkX');
    await fireEvent.click(getByText('userManagement.resetPasswordButton'));
    expect(toastStore.error).toHaveBeenCalledWith('userManagement.passwordsDoNotMatch');

    await fill(getByLabelText, 'abcdefghijk', 'abcdefghijk');
    await fireEvent.click(getByText('userManagement.resetPasswordButton'));
    expect(toastStore.error).toHaveBeenCalledWith('userManagement.passwordMinLength');

    expect(mockAxios.put).not.toHaveBeenCalled();
  });

  it('puts the new password on the target and closes on success', async () => {
    const { getByText, getByLabelText, onClose } = renderModal();

    await fill(getByLabelText, 'abcdefghijkl', 'abcdefghijkl');
    await fireEvent.click(getByText('userManagement.resetPasswordButton'));

    await waitFor(() =>
      expect(mockAxios.put).toHaveBeenCalledWith('/users/u-1', { password: 'abcdefghijkl' })
    );
    expect(toastStore.success).toHaveBeenCalledWith('userManagement.passwordResetSuccess');
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('stays open and shows the server message when the reset is refused', async () => {
    mockAxios.put.mockRejectedValue({
      response: { status: 400, data: { detail: 'Password has been used recently.' } },
    });
    const { getByText, getByLabelText, onClose } = renderModal();

    await fill(getByLabelText, 'abcdefghijkl', 'abcdefghijkl');
    await fireEvent.click(getByText('userManagement.resetPasswordButton'));

    await waitFor(() =>
      expect(toastStore.error).toHaveBeenCalledWith('Password has been used recently.')
    );
    expect(onClose).not.toHaveBeenCalled();
    expect(getByText('userManagement.resetPasswordButton').closest('button')).not.toBeDisabled();
  });

  it('cancel closes without a request', async () => {
    const { getByText, onClose } = renderModal();
    await fireEvent.click(getByText('common.cancel'));
    expect(onClose).toHaveBeenCalled();
    expect(mockAxios.put).not.toHaveBeenCalled();
  });
});
