import { describe, it, expect } from 'vitest';
import { durationLabel, grantTargetLabel, userLabel, isUuid, targetNameOf } from './format';
import type { SupportGrant } from '$lib/api/supportAccess';

// Identity-ish translator that exposes the key and the interpolation values.
const tr = (key: string, opts?: Record<string, unknown>) =>
  opts ? `${key}|${JSON.stringify(opts)}` : key;

function grant(over: Partial<SupportGrant>): SupportGrant {
  return {
    uuid: 'g1',
    status: 'active',
    target_kind: 'organization',
    grant_mode: 'approved',
    access_level: 'read',
    organization: { uuid: 'o1', name: 'Acme', slug: 'acme' },
    subject_user: null,
    grantee: { uuid: 'u1', full_name: 'Sam Support', email: 'sam@example.com' },
    reason: 'r',
    ticket_ref: null,
    requested_duration_minutes: 60,
    requested_at: '2026-10-09T12:00:00Z',
    pending_expires_at: null,
    decided_by: null,
    decided_at: null,
    starts_at: null,
    expires_at: null,
    revoked_by: null,
    revoked_at: null,
    ...over,
  };
}

describe('durationLabel', () => {
  it.each([
    [15, 'supportAccess.duration.minutes|{"count":15}'],
    [45, 'supportAccess.duration.minutes|{"count":45}'],
    [60, 'supportAccess.duration.hours|{"count":1}'],
    [120, 'supportAccess.duration.hours|{"count":2}'],
    [480, 'supportAccess.duration.hours|{"count":8}'],
    [90, 'supportAccess.duration.minutes|{"count":90}'],
  ])('%i minutes', (minutes, expected) => {
    expect(durationLabel(minutes, tr)).toBe(expected);
  });
});

describe('grantTargetLabel / targetNameOf', () => {
  it('names the organization', () => {
    expect(grantTargetLabel(grant({}), tr)).toBe('Acme');
    expect(targetNameOf(grant({}), tr)).toBe('Acme');
  });

  it('names a deleted organization explicitly', () => {
    expect(grantTargetLabel(grant({ organization: null }), tr)).toBe(
      'supportAccess.deletedOrganization'
    );
  });

  it('labels a personal workspace with the subject name, falling back to the email', () => {
    const named = grant({
      target_kind: 'personal',
      organization: null,
      subject_user: { uuid: 'u2', full_name: 'Jane Doe', email: 'jane@example.com' },
    });
    expect(grantTargetLabel(named, tr)).toBe('supportAccess.personalWorkspace|{"name":"Jane Doe"}');
    expect(targetNameOf(named, tr)).toBe('Jane Doe');
    const unnamed = grant({
      target_kind: 'personal',
      organization: null,
      subject_user: { uuid: 'u2', full_name: null, email: 'jane@example.com' },
    });
    expect(targetNameOf(unnamed, tr)).toBe('jane@example.com');
  });

  it('names a deleted subject user explicitly', () => {
    const gone = grant({ target_kind: 'personal', organization: null, subject_user: null });
    expect(targetNameOf(gone, tr)).toBe('supportAccess.deletedUser');
  });
});

describe('userLabel', () => {
  it('prefers the full name, then the email, and flags a deleted account', () => {
    expect(userLabel({ uuid: 'u', full_name: 'A B', email: 'a@b.c' }, tr)).toBe('A B');
    expect(userLabel({ uuid: 'u', full_name: null, email: 'a@b.c' }, tr)).toBe('a@b.c');
    expect(userLabel(null, tr)).toBe('supportAccess.deletedUser');
  });
});

describe('isUuid', () => {
  it('accepts a canonical uuid and rejects look-alikes', () => {
    expect(isUuid('11111111-2222-3333-4444-555555555555')).toBe(true);
    expect(isUuid(' 11111111-2222-3333-4444-555555555555 ')).toBe(true);
    expect(isUuid('11111111-2222-3333-4444-55555555555')).toBe(false);
    expect(isUuid('../etc/passwd')).toBe(false);
    expect(isUuid('')).toBe(false);
  });
});
