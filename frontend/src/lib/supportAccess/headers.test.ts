import { describe, it, expect, beforeEach } from 'vitest';
import {
  SUPPORT_GRANT_HEADER,
  setActiveSupportGrant,
  isSupportAccessExempt,
  getSupportAccessHeaders,
} from './headers';

const GRANT = '11111111-2222-3333-4444-555555555555';

describe('support-access header injection', () => {
  beforeEach(() => setActiveSupportGrant(null));

  it('sends nothing while no session is active', () => {
    expect(getSupportAccessHeaders('/files/abc')).toEqual({});
  });

  it('sends the grant header on an ordinary tenant route while active', () => {
    setActiveSupportGrant(GRANT);
    expect(getSupportAccessHeaders('/files/abc')).toEqual({ [SUPPORT_GRANT_HEADER]: GRANT });
    expect(getSupportAccessHeaders('/api/files/abc/thumbnail')).toEqual({
      [SUPPORT_GRANT_HEADER]: GRANT,
    });
  });

  it('stops sending after the session is cleared', () => {
    setActiveSupportGrant(GRANT);
    setActiveSupportGrant(null);
    expect(getSupportAccessHeaders('/files/abc')).toEqual({});
  });

  it.each([
    '/auth/me',
    '/api/auth/token/refresh',
    '/system/capabilities',
    '/api/system/capabilities',
    '/support-access/grants',
    '/api/support-access/grants/x/revoke',
    '/org-admin/members',
    '/api/org-admin/support-access',
    '/users/me/support-access',
    '/api/users/me/support-access/x/approve',
    '/chat/sessions',
    '/api/chat/stream',
    '/admin/users/search',
    '/api/admin/users',
  ])('never sends the header to the exempt route %s', (url) => {
    setActiveSupportGrant(GRANT);
    expect(isSupportAccessExempt(url)).toBe(true);
    expect(getSupportAccessHeaders(url)).toEqual({});
  });

  it('does not exempt look-alike prefixes', () => {
    setActiveSupportGrant(GRANT);
    expect(getSupportAccessHeaders('/users/me/profile')).toEqual({
      [SUPPORT_GRANT_HEADER]: GRANT,
    });
    expect(getSupportAccessHeaders('/files/chat-notes')).toEqual({
      [SUPPORT_GRANT_HEADER]: GRANT,
    });
    expect(getSupportAccessHeaders('/authors')).toEqual({ [SUPPORT_GRANT_HEADER]: GRANT });
  });

  it.each([
    'https://minio.example/bucket/x?X-Amz-Signature=abc',
    'http://localhost:5178/bucket/x',
    '//cdn.example/x',
  ])('never sends the header to the absolute URL %s', (url) => {
    setActiveSupportGrant(GRANT);
    expect(getSupportAccessHeaders(url)).toEqual({});
  });

  it('treats a missing url as exempt (fail closed)', () => {
    setActiveSupportGrant(GRANT);
    expect(getSupportAccessHeaders(undefined)).toEqual({});
  });
});
