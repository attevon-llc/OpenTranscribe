import { describe, expect, it } from 'vitest';
import { buildEmailConfigPayload, type EmailConfigForm } from './emailConfigPayload';

const form = (over: Partial<EmailConfigForm> = {}): EmailConfigForm => ({
  name: '  Ops mail ',
  provider: 'smtp',
  is_enabled: true,
  from_address: ' ops@example.com ',
  default_recipients: ' ',
  smtp_host: ' smtp.example.com ',
  smtp_port: '2525',
  smtp_use_tls: false,
  smtp_username: ' user ',
  smtp_password: '',
  m365_tenant_id: ' tenant ',
  m365_client_id: ' client ',
  m365_client_secret: '',
  exchange_server: ' ex.example.com ',
  exchange_domain: '',
  exchange_username: ' svc ',
  exchange_password: '',
  ...over,
});

describe('buildEmailConfigPayload', () => {
  it('sends only the SMTP fields for smtp, trimmed, with the port as a number', () => {
    expect(buildEmailConfigPayload(form())).toEqual({
      name: 'Ops mail',
      is_enabled: true,
      from_address: 'ops@example.com',
      default_recipients: null,
      smtp_host: 'smtp.example.com',
      smtp_port: 2525,
      smtp_use_tls: false,
      smtp_username: 'user',
    });
  });

  it('includes a typed secret, trimmed, and leaves a blank one out so the stored one is kept', () => {
    expect(buildEmailConfigPayload(form({ smtp_password: ' pw ' }))).toHaveProperty(
      'smtp_password',
      'pw'
    );
    expect(buildEmailConfigPayload(form({ smtp_password: '   ' }))).not.toHaveProperty(
      'smtp_password'
    );
  });

  it('sends the tenant and client for m365, and the secret only when typed', () => {
    const p = buildEmailConfigPayload(form({ provider: 'm365', m365_client_secret: ' s ' }));
    expect(p).toMatchObject({ m365_tenant_id: 'tenant', m365_client_id: 'client' });
    expect(p).toHaveProperty('m365_client_secret', 's');
    expect(p).not.toHaveProperty('smtp_host');
    expect(buildEmailConfigPayload(form({ provider: 'm365' }))).not.toHaveProperty(
      'm365_client_secret'
    );
  });

  it('sends the server, nullable domain/user and the SMTP port for exchange', () => {
    const p = buildEmailConfigPayload(form({ provider: 'exchange', exchange_password: ' x ' }));
    expect(p).toMatchObject({
      exchange_server: 'ex.example.com',
      exchange_domain: null,
      exchange_username: 'svc',
      smtp_port: 2525,
      exchange_password: 'x',
    });
    expect(p).not.toHaveProperty('smtp_host');
  });
});
