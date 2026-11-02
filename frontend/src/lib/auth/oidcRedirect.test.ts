/**
 * Pins the OIDC redirect guard: only http(s) authorization URLs are followed, so a
 * misconfigured or compromised backend cannot send the browser to a javascript: or
 * data: URL.
 */
import { describe, it, expect } from 'vitest';
import { checkAuthorizationUrl } from './oidcRedirect';

describe('checkAuthorizationUrl', () => {
  it('follows https and http URLs, normalised', () => {
    expect(checkAuthorizationUrl('https://idp.example/auth?x=1')).toEqual({
      ok: true,
      url: 'https://idp.example/auth?x=1',
    });
    expect(checkAuthorizationUrl('http://keycloak:8180')).toEqual({
      ok: true,
      url: 'http://keycloak:8180/',
    });
  });

  it('rejects script and data URLs by protocol', () => {
    expect(checkAuthorizationUrl('javascript:alert(1)')).toEqual({
      ok: false,
      reason: 'protocol',
      protocol: 'javascript:',
    });
    expect(checkAuthorizationUrl('data:text/html,<b>x</b>')).toMatchObject({
      ok: false,
      reason: 'protocol',
      protocol: 'data:',
    });
    expect(checkAuthorizationUrl('ftp://idp.example/')).toMatchObject({ ok: false });
  });

  it('rejects a value that is not a URL at all', () => {
    expect(checkAuthorizationUrl('/relative/path')).toEqual({ ok: false, reason: 'invalid' });
    expect(checkAuthorizationUrl('')).toEqual({ ok: false, reason: 'invalid' });
  });
});
