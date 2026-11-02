/**
 * The user-management panel's invitations: the invite form, the pending list and
 * revocation, over `$lib/api/invitations`.
 *
 * Each listed invitation carries a server-computed `status`; it is rendered, never
 * re-derived from `expires_at` / `used_at` (fat backend, thin frontend). Who may invite
 * which role is the server's call (`POST /auth/invitations` 403s an elevated role from
 * a plain admin); the form only offers what `rowPermissions.canAssignElevatedRoles`
 * allows.
 *
 *     const invites = createInvitationsController(confirmDialog);
 *     const { form, invitations, loading, submitting, revokingUuid } = invites;
 *     <input bind:value={$form.email} />
 */
import { get, writable, type Writable } from 'svelte/store';
import {
  INVITE_EXPIRY_DEFAULT_HOURS,
  createInvitation,
  listInvitations,
  revokeInvitation,
  type AuthType,
  type Invitation,
} from '$lib/api/invitations';
import type { ConfirmDialog } from '$lib/confirmDialog';
import { getErrorMessage, getErrorStatus } from '$lib/utils/apiError';
import { t } from '$stores/locale';
import { toastStore } from '$stores/toast';
import { ROLE_SUPER_ADMIN } from './rowPermissions';

export interface InviteForm {
  email: string;
  full_name: string;
  role: string;
  auth_type: AuthType;
  expires_in_hours: number;
}

export function emptyInviteForm(): InviteForm {
  return {
    email: '',
    full_name: '',
    role: 'user',
    auth_type: 'local',
    expires_in_hours: INVITE_EXPIRY_DEFAULT_HOURS,
  };
}

export interface InvitationsController {
  form: Writable<InviteForm>;
  invitations: Writable<Invitation[]>;
  loading: Writable<boolean>;
  submitting: Writable<boolean>;
  /** UUID of the invitation being revoked, or null. */
  revokingUuid: Writable<string | null>;
  refresh(): Promise<void>;
  resetForm(): void;
  /** Validate, confirm an elevated role, send. `onSent` runs once the invite is created. */
  send(onSent: () => void): Promise<void>;
  revoke(invitation: Invitation): void;
}

export function createInvitationsController(confirmDialog: ConfirmDialog): InvitationsController {
  const form = writable<InviteForm>(emptyInviteForm());
  const invitations = writable<Invitation[]>([]);
  const loading = writable(false);
  const submitting = writable(false);
  const revokingUuid = writable<string | null>(null);
  const tr = (key: string, vars?: Record<string, unknown>) => get(t)(key, vars);

  async function refresh() {
    loading.set(true);
    try {
      invitations.set(await listInvitations(false));
    } catch (err) {
      // A plain admin still sees the rest of the panel; the endpoint is admin-
      // gated exactly like the user list, so a failure here is not fatal.
      console.warn('Could not load invitations', err);
      invitations.set([]);
    } finally {
      loading.set(false);
    }
  }

  function resetForm() {
    form.set(emptyInviteForm());
  }

  async function executeSend(onSent: () => void) {
    submitting.set(true);
    const current = get(form);
    const invitedAddress = current.email.trim();
    try {
      await createInvitation({
        email: invitedAddress,
        full_name: current.full_name.trim() || undefined,
        role: current.role,
        auth_type: current.auth_type,
        expires_in_hours: current.expires_in_hours,
      });

      resetForm();
      onSent();

      toastStore.success(tr('userManagement.inviteSent', { email: invitedAddress }));
      await refresh();
    } catch (err) {
      console.error('Error creating invitation:', err);
      // 503 is the backend's "no mail transport" refusal. Its sentence is English and names no
      // fix, so say what happened to the invitation (it IS saved, as pending) and where to
      // fix it. The invitation list is refreshed so that pending row is actually visible.
      if (getErrorStatus(err) === 503) {
        toastStore.error(tr('userManagement.inviteMailUnavailable'));
        await refresh();
      } else {
        toastStore.error(getErrorMessage(err, tr('userManagement.inviteFailed')));
      }
    } finally {
      submitting.set(false);
    }
  }

  /**
   * Invite someone to create their own account.
   *
   * Preferred over creating the account directly: the admin never sees or chooses
   * the invitee's password, the invitee gets an email, and `auth_type` is carried
   * through so an IdP-fronted deployment can pre-provision matching accounts.
   */
  async function send(onSent: () => void) {
    const current = get(form);
    if (!current.email.trim()) {
      toastStore.error(tr('userManagement.fillAllFields'));
      return;
    }

    if (current.role === ROLE_SUPER_ADMIN) {
      confirmDialog.request(
        tr('userManagement.confirmSuperAdminTitle'),
        tr('userManagement.confirmSuperAdminInvite', { name: current.full_name || current.email }),
        () => executeSend(onSent),
        tr('userManagement.confirmSuperAdminButton')
      );
      return;
    }

    await executeSend(onSent);
  }

  async function executeRevoke(invitation: Invitation) {
    revokingUuid.set(invitation.uuid);
    try {
      await revokeInvitation(invitation.uuid);
      toastStore.success(tr('userManagement.inviteRevoked', { email: invitation.email }));
      await refresh();
    } catch (err) {
      console.error('Error revoking invitation:', err);
      toastStore.error(getErrorMessage(err, tr('userManagement.revokeInviteFailed')));
    } finally {
      revokingUuid.set(null);
    }
  }

  function revoke(invitation: Invitation) {
    confirmDialog.request(
      tr('userManagement.revokeInvitation'),
      tr('userManagement.revokeInvitationConfirm', { email: invitation.email }),
      () => executeRevoke(invitation),
      tr('userManagement.revokeInvitation')
    );
  }

  return { form, invitations, loading, submitting, revokingUuid, refresh, resetForm, send, revoke };
}
