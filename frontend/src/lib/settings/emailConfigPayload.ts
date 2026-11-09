/**
 * The request body for saving an email config from the modal's form: the common fields plus
 * the fields of the chosen provider, trimmed, with an unchanged secret left out entirely so
 * the server keeps the stored one.
 */
import type { EmailConfigCreate, EmailProvider } from '$lib/api/watchSourcesApi';

/** A create body without its provider; also valid, as is, for an update. */
export type EmailConfigBody = Omit<EmailConfigCreate, 'provider'>;
type ProviderFields = Partial<EmailConfigBody>;

/** The modal's form model: every field is held, whichever provider is selected. */
export interface EmailConfigForm {
  name: string;
  provider: EmailProvider;
  is_enabled: boolean;
  from_address: string;
  default_recipients?: string;
  smtp_host: string;
  smtp_port: number | string;
  smtp_use_tls: boolean;
  smtp_username?: string;
  smtp_password?: string;
  m365_tenant_id: string;
  m365_client_id: string;
  m365_client_secret?: string;
  exchange_server: string;
  exchange_domain?: string;
  exchange_username?: string;
  exchange_password?: string;
}

const trimmedOrNull = (value: string | undefined): string | null => value?.trim() || null;

/** `{ [key]: trimmed }`, or nothing when the secret field was left blank. */
function secretIfTyped<K extends string>(key: K, value: string | undefined) {
  const trimmed = value?.trim();
  return trimmed ? ({ [key]: trimmed } as Record<K, string>) : {};
}

function smtpFields(form: EmailConfigForm): ProviderFields {
  return {
    smtp_host: form.smtp_host.trim(),
    smtp_port: Number(form.smtp_port),
    smtp_use_tls: form.smtp_use_tls,
    smtp_username: trimmedOrNull(form.smtp_username),
    ...secretIfTyped('smtp_password', form.smtp_password),
  };
}

function m365Fields(form: EmailConfigForm): ProviderFields {
  return {
    m365_tenant_id: form.m365_tenant_id.trim(),
    m365_client_id: form.m365_client_id.trim(),
    ...secretIfTyped('m365_client_secret', form.m365_client_secret),
  };
}

function exchangeFields(form: EmailConfigForm): ProviderFields {
  return {
    exchange_server: form.exchange_server.trim(),
    exchange_domain: trimmedOrNull(form.exchange_domain),
    exchange_username: trimmedOrNull(form.exchange_username),
    smtp_port: Number(form.smtp_port),
    ...secretIfTyped('exchange_password', form.exchange_password),
  };
}

const PROVIDER_FIELDS: Record<EmailProvider, (form: EmailConfigForm) => ProviderFields> = {
  smtp: smtpFields,
  m365: m365Fields,
  exchange: exchangeFields,
};

export function buildEmailConfigPayload(form: EmailConfigForm): EmailConfigBody {
  return {
    name: form.name.trim(),
    is_enabled: form.is_enabled,
    from_address: form.from_address.trim(),
    default_recipients: trimmedOrNull(form.default_recipients),
    ...PROVIDER_FIELDS[form.provider]?.(form),
  };
}
