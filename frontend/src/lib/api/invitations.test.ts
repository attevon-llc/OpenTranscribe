/**
 * Non-enumeration invariants for the public invitation / verification routes.
 *
 * The backend deliberately answers every bad-token state with ONE identical
 * message, and answers the verification resend identically for a registered
 * address, an unknown one and an already-verified one. Both properties are only
 * worth anything if the client refuses to reconstruct the distinction, so they
 * are pinned here rather than left to review.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('$lib/axios', () => {
  const axiosInstance = { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() };
  return { axiosInstance, default: axiosInstance };
});

import { axiosInstance } from '$lib/axios';
import {
  lookupInvitation,
  acceptInvitation,
  createInvitation,
  listInvitations,
  revokeInvitation,
  verifyEmail,
  resendEmailVerification,
  invitationErrorMessage,
  INVITE_EXPIRY_DEFAULT_HOURS,
} from './invitations';

const post = vi.mocked(axiosInstance.post);
const get = vi.mocked(axiosInstance.get);
const del = vi.mocked(axiosInstance.delete);

/** The single message the backend returns for every invitation-token failure. */
const GENERIC_INVALID = 'This invitation link is invalid, expired, or has already been used.';

const badToken = () => ({ response: { status: 400, data: { detail: GENERIC_INVALID } } });

describe('invitation token transport', () => {
  beforeEach(() => vi.clearAllMocks());

  it('sends the lookup token in the BODY, never a query param or header', async () => {
    post.mockResolvedValue({ data: { email: 'a@b.test', requires_password: true } } as never);

    await lookupInvitation('raw-invite-token');

    const [url, body, config] = post.mock.calls[0];
    expect(url).toBe('/auth/invitations/lookup');
    expect(body).toEqual({ token: 'raw-invite-token' });
    // A token in a query string or header lands in access logs, browser history
    // and referrer headers — it is a bearer credential for a future account.
    expect(url).not.toContain('raw-invite-token');
    expect(JSON.stringify(config ?? {})).not.toContain('raw-invite-token');
  });

  it('sends the accept token in the body and omits password when not supplied', async () => {
    post.mockResolvedValue({ data: { can_login_with_password: false } } as never);

    await acceptInvitation({ token: 'raw-invite-token', full_name: 'Ada' });

    const [url, body] = post.mock.calls[0];
    expect(url).toBe('/auth/invitations/accept');
    expect(body).toEqual({ token: 'raw-invite-token', full_name: 'Ada' });
    expect(body).not.toHaveProperty('password');
  });

  it('sends the verification token in the body', async () => {
    post.mockResolvedValue({ data: { message: 'ok' } } as never);

    await verifyEmail('raw-verify-token');

    expect(post.mock.calls[0][0]).toBe('/auth/verify-email');
    expect(post.mock.calls[0][1]).toEqual({ token: 'raw-verify-token' });
  });
});

describe('invitation errors are rendered, not classified', () => {
  beforeEach(() => vi.clearAllMocks());

  it('returns the same string for every bad-token state', async () => {
    // Unknown, expired, revoked and already-used all arrive as this one 400.
    const outcomes = await Promise.all(
      ['unknown', 'expired', 'revoked', 'used'].map(async () => {
        post.mockRejectedValueOnce(badToken());
        try {
          await lookupInvitation('whatever');
          throw new Error('expected a rejection');
        } catch (err) {
          return invitationErrorMessage(err, 'fallback');
        }
      })
    );

    expect(new Set(outcomes).size).toBe(1);
    expect(outcomes[0]).toBe(GENERIC_INVALID);
  });

  it('renders the backend detail verbatim rather than substituting a local message', async () => {
    post.mockRejectedValueOnce({
      response: { status: 400, data: { detail: 'Password does not meet policy requirements: …' } },
    });

    try {
      await acceptInvitation({ token: 't', password: 'weak' });
    } catch (err) {
      expect(invitationErrorMessage(err, 'fallback')).toBe(
        'Password does not meet policy requirements: …'
      );
    }
  });

  it('falls back only when the server sent no usable detail', () => {
    expect(invitationErrorMessage({ response: { status: 400, data: {} } }, 'fallback')).toBe(
      'fallback'
    );
    expect(invitationErrorMessage(new Error('Network Error'), 'fallback')).toBe('fallback');
    expect(invitationErrorMessage({ response: { data: { detail: '  ' } } }, 'fallback')).toBe(
      'fallback'
    );
  });

  it('flattens a validation-array detail without inventing a classification', () => {
    expect(
      invitationErrorMessage(
        { response: { status: 422, data: { detail: [{ msg: 'not an email' }] } } },
        'fallback'
      )
    ).toBe('not an email');
  });
});

describe('verification resend never reveals whether an address exists', () => {
  /** The backend's constant reply — identical for every address. */
  const CONSTANT = 'If that address needs verification, a new link has been sent.';

  beforeEach(() => vi.clearAllMocks());

  it('returns one identical message for registered, unknown and verified addresses', async () => {
    post.mockResolvedValue({ data: { message: CONSTANT } } as never);

    const results = await Promise.all([
      resendEmailVerification('registered@example.com'),
      resendEmailVerification('nobody-here@example.com'),
      resendEmailVerification('already-verified@example.com'),
    ]);

    // Nothing in the resolved value distinguishes the three cases: not the
    // message, not an extra field, not a status. If it did, this endpoint would
    // be an account-existence oracle reachable with no session.
    const serialised = results.map((r) => JSON.stringify(r));
    expect(new Set(serialised).size).toBe(1);
    expect(results[0]).toEqual({ message: CONSTANT });
    for (const result of results) {
      expect(Object.keys(result)).toEqual(['message']);
    }
  });

  it('sends the address in the body of the resend request', async () => {
    post.mockResolvedValue({ data: { message: CONSTANT } } as never);

    await resendEmailVerification('someone@example.com');

    expect(post).toHaveBeenCalledWith('/auth/verify-email/resend', {
      email: 'someone@example.com',
    });
  });
});

describe('admin invitation endpoints', () => {
  beforeEach(() => vi.clearAllMocks());

  it('creates an invitation with role, auth_type and expiry', async () => {
    post.mockResolvedValue({ data: { uuid: 'inv-1', status: 'pending' } } as never);

    await createInvitation({
      email: 'new@example.com',
      full_name: 'New Person',
      role: 'user',
      auth_type: 'oidc',
      expires_in_hours: INVITE_EXPIRY_DEFAULT_HOURS,
    });

    expect(post).toHaveBeenCalledWith('/auth/invitations', {
      email: 'new@example.com',
      full_name: 'New Person',
      role: 'user',
      auth_type: 'oidc',
      expires_in_hours: 72,
    });
  });

  it('lists pending invitations and passes the server-computed status through', async () => {
    get.mockResolvedValue({
      data: [{ uuid: 'inv-1', email: 'a@b.test', status: 'pending' }],
    } as never);

    const rows = await listInvitations();

    expect(get).toHaveBeenCalledWith('/auth/invitations', {
      params: { include_inactive: false },
    });
    // The status is server-computed; the client must not re-derive it.
    expect(rows[0].status).toBe('pending');
  });

  it('degrades to an empty list if the endpoint returns a non-array', async () => {
    get.mockResolvedValue({ data: null } as never);
    expect(await listInvitations()).toEqual([]);
  });

  it('revokes by UUID', async () => {
    del.mockResolvedValue({} as never);
    await revokeInvitation('inv-uuid');
    expect(del).toHaveBeenCalledWith('/auth/invitations/inv-uuid');
  });
});
