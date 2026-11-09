/**
 * The URL panel offers a username and password only for a URL whose host has a
 * `user_password` protected-media config, and sends them only when they were filled in.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { tick } from 'svelte';
import { render, fireEvent } from '@testing-library/svelte';

const mockGetAuthConfigForUrl = vi.hoisted(() => vi.fn());
vi.mock('$lib/services/configService', () => ({
  loadProtectedMediaAuthConfig: vi.fn().mockResolvedValue(undefined),
  getAuthConfigForUrl: mockGetAuthConfigForUrl,
}));
vi.mock('$lib/api/legal', () => ({
  TERMS_KEY: 'terms',
  listLegalDocuments: vi.fn().mockResolvedValue([]),
}));
vi.mock('$lib/api/downloadSettings', () => ({
  getDownloadSettings: vi.fn().mockResolvedValue({}),
  getDownloadSystemDefaults: vi.fn().mockResolvedValue({}),
}));
vi.mock('$stores/toast', async () => (await import('$testMocks/serviceMocks')).toastStoreMock());
vi.mock('$stores/locale', async () => (await import('$testMocks/localeMocks')).keyLocale());

import MediaUrlPanel from './MediaUrlPanel.svelte';

const PROTECTED = 'https://media.example.com/view?m=abc';

beforeEach(() => {
  vi.clearAllMocks();
  mockGetAuthConfigForUrl.mockImplementation((url: URL) =>
    url.hostname === 'media.example.com'
      ? { hosts: ['media.example.com'], auth_type: 'user_password', fields: [] }
      : url.hostname === 'other.example.com'
        ? { hosts: ['other.example.com'], auth_type: 'api_key', fields: [] }
        : null
  );
});

function credentialInputs(container: HTMLElement) {
  return {
    username: container.querySelector('#media-username') as HTMLInputElement | null,
    password: container.querySelector('#media-password') as HTMLInputElement | null,
  };
}

describe('protected media credentials', () => {
  it('asks for a username and password for a user_password host', () => {
    const { container } = render(MediaUrlPanel, { props: { mediaUrl: PROTECTED } });

    const { username, password } = credentialInputs(container);
    expect(username?.type).toBe('text');
    expect(password?.type).toBe('password');
    expect(container.textContent).toContain('uploader.protectedMediaHint');
    const asked = mockGetAuthConfigForUrl.mock.calls.at(-1)?.[0] as URL;
    expect(asked).toBeInstanceOf(URL);
    expect(asked.href).toBe(PROTECTED);
  });

  it.each([
    ['another auth type', 'https://other.example.com/view?m=abc'],
    ['an unconfigured host', 'https://plain.example.com/video'],
    ['text that is not a URL', 'not a url'],
    ['an empty field', ''],
  ])('asks for nothing for %s', (_label, mediaUrl) => {
    const { container } = render(MediaUrlPanel, { props: { mediaUrl } });

    expect(credentialInputs(container)).toEqual({ username: null, password: null });
  });

  it('sends only the credentials that were filled in', async () => {
    const { container, component } = render(MediaUrlPanel, { props: { mediaUrl: PROTECTED } });
    const { username, password } = credentialInputs(container);

    expect(component.getUrlPayloadExtras()).toEqual({});

    await fireEvent.input(username as HTMLInputElement, { target: { value: 'alice' } });
    expect(component.getUrlPayloadExtras()).toEqual({ media_username: 'alice' });

    await fireEvent.input(password as HTMLInputElement, { target: { value: 's3cret' } });
    expect(component.getUrlPayloadExtras()).toEqual({
      media_username: 'alice',
      media_password: 's3cret',
    });

    component.resetState();
    await tick();
    expect(component.getUrlPayloadExtras()).toEqual({});
  });
});
