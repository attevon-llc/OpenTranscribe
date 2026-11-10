import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/svelte';

vi.mock('$stores/locale', async () => (await import('$testMocks/localeMocks')).keyLocale());

import LDAPSettings from './LDAPSettings.svelte';

describe('LDAP panel: defaults for unset fields', () => {
  it('fills port, attributes and timeout when the saved config omits them or holds null', () => {
    render(LDAPSettings, {
      props: { config: { ldap_server: 'ldap.example.com', ldap_port: null } },
    } as never);

    expect((screen.getByLabelText('settings.ldap.port') as HTMLInputElement).value).toBe('389');
    expect((screen.getByLabelText('settings.ldap.server') as HTMLInputElement).value).toBe(
      'ldap.example.com'
    );
  });

  it('keeps stored values over the defaults', () => {
    render(LDAPSettings, { props: { config: { ldap_port: 3269 } } } as never);

    expect((screen.getByLabelText('settings.ldap.port') as HTMLInputElement).value).toBe('3269');
  });
});
