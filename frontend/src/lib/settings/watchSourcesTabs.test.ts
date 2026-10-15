import { describe, it, expect } from 'vitest';
import { watchSourcesTabs, resolveWatchSourcesTab } from './watchSourcesTabs';

describe('watchSourcesTabs', () => {
  it('offers only Sources below super_admin', () => {
    expect(watchSourcesTabs(false)).toEqual(['sources']);
  });

  it('adds the credential-holding and deployment-wide tabs for super_admin', () => {
    expect(watchSourcesTabs(true)).toEqual(['sources', 'email', 'global']);
  });

  it('falls back to the first tab when the requested one is no longer offered', () => {
    expect(resolveWatchSourcesTab('global', watchSourcesTabs(false))).toBe('sources');
    expect(resolveWatchSourcesTab('email', watchSourcesTabs(true))).toBe('email');
  });
});
