import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/svelte';

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

vi.mock('$stores/toast', () => ({
  toastStore: { success: vi.fn(), error: vi.fn() },
}));

const linkExternalIdentity = vi.fn();
vi.mock('$lib/api/admin', () => ({
  AdminApi: { linkExternalIdentity: (...args: unknown[]) => linkExternalIdentity(...args) },
}));

import LinkIdentityModal from './LinkIdentityModal.svelte';
import { toastStore } from '$stores/toast';

const targetUser = { uuid: 'user-uuid', full_name: 'Jane Doe', email: 'jane@example.com' };

beforeEach(() => {
  linkExternalIdentity.mockReset();
  (toastStore.success as ReturnType<typeof vi.fn>).mockReset();
  (toastStore.error as ReturnType<typeof vi.fn>).mockReset();
});

function renderModal(props: Record<string, unknown> = {}) {
  return render(LinkIdentityModal, {
    props: { isOpen: true, targetUser, ...props },
  } as never);
}

describe('LinkIdentityModal (P1.3)', () => {
  it('defaults to the OIDC provider and an empty identifier', () => {
    renderModal();

    expect(screen.getByLabelText('userManagement.linkIdentity.provider')).toHaveValue('oidc');
    expect(screen.getByLabelText('userManagement.linkIdentity.identifierOidc')).toHaveValue('');
  });

  it('disables the submit button until an identifier is typed', async () => {
    renderModal();

    const submit = screen.getByRole('button', { name: 'userManagement.linkIdentity.linkButton' });
    expect(submit).toBeDisabled();

    await fireEvent.input(screen.getByLabelText('userManagement.linkIdentity.identifierOidc'), {
      target: { value: 'authentik|abc123' },
    });
    expect(submit).not.toBeDisabled();
  });

  it('submits the selected provider and typed identifier', async () => {
    linkExternalIdentity.mockResolvedValue({
      success: true,
      provider: 'ldap',
      identifier: 'jdoe',
    });
    renderModal();

    await fireEvent.change(screen.getByLabelText('userManagement.linkIdentity.provider'), {
      target: { value: 'ldap' },
    });
    await fireEvent.input(screen.getByLabelText('userManagement.linkIdentity.identifierLdap'), {
      target: { value: 'jdoe' },
    });
    await fireEvent.click(
      screen.getByRole('button', { name: 'userManagement.linkIdentity.linkButton' })
    );

    await waitFor(() => {
      expect(linkExternalIdentity).toHaveBeenCalledWith('user-uuid', 'ldap', 'jdoe');
    });
    expect(toastStore.success).toHaveBeenCalled();
  });

  it('surfaces a failure via toast without closing', async () => {
    linkExternalIdentity.mockRejectedValue({
      response: { data: { detail: 'That ldap identifier is already linked to another account' } },
    });
    renderModal();

    await fireEvent.input(screen.getByLabelText('userManagement.linkIdentity.identifierOidc'), {
      target: { value: 'authentik|taken' },
    });
    await fireEvent.click(
      screen.getByRole('button', { name: 'userManagement.linkIdentity.linkButton' })
    );

    await waitFor(() => {
      expect(toastStore.error).toHaveBeenCalled();
    });
  });

  it('re-seeds provider and identifier back to defaults each time it opens', async () => {
    const { rerender } = renderModal();

    await fireEvent.change(screen.getByLabelText('userManagement.linkIdentity.provider'), {
      target: { value: 'pki' },
    });
    await fireEvent.input(screen.getByLabelText('userManagement.linkIdentity.identifierPki'), {
      target: { value: 'CN=Someone' },
    });

    await rerender({ isOpen: false, targetUser });
    await rerender({ isOpen: true, targetUser });

    expect(screen.getByLabelText('userManagement.linkIdentity.provider')).toHaveValue('oidc');
    expect(screen.getByLabelText('userManagement.linkIdentity.identifierOidc')).toHaveValue('');
  });
});
