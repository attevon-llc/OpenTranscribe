/**
 * The form fills from `editingConfig` once per config and per open, and a re-render with the
 * same config must not overwrite what the admin has typed since.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, fireEvent } from '@testing-library/svelte';

vi.mock('$stores/locale', async () => (await import('$testMocks/localeMocks')).keyLocale());
vi.mock('$stores/toast', async () => (await import('$testMocks/serviceMocks')).toastStoreMock());

import EmailConfigModal from './EmailConfigModal.svelte';

const CONFIG = (uuid: string, name: string) =>
  ({
    uuid,
    name,
    provider: 'smtp',
    is_enabled: true,
    from_address: 'a@example.com',
    default_recipients: '',
    smtp_host: 'smtp.example.com',
    smtp_port: 587,
    smtp_use_tls: true,
    smtp_username: '',
  }) as never;

describe('EmailConfigModal form population', () => {
  it('fills from the config, keeps user edits across same-config renders, refills for another config', async () => {
    const { rerender } = render(EmailConfigModal, {
      props: { show: true, editingConfig: CONFIG('c1', 'Ops') },
    } as never);
    expect(document.querySelector<HTMLInputElement>('#em-name')!.value).toBe('Ops');

    await fireEvent.input(document.querySelector('#em-name')!, { target: { value: 'Edited' } });
    await rerender({ show: true, editingConfig: CONFIG('c1', 'Ops') } as never);
    expect(document.querySelector<HTMLInputElement>('#em-name')!.value).toBe('Edited');

    await rerender({ show: true, editingConfig: CONFIG('c2', 'Billing') } as never);
    expect(document.querySelector<HTMLInputElement>('#em-name')!.value).toBe('Billing');

    await rerender({ show: true, editingConfig: null } as never);
    expect(document.querySelector<HTMLInputElement>('#em-name')!.value).toBe('');
  });
});
