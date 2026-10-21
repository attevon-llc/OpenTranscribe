/**
 * AuthMailNotice (issue #1240): says, where the admin is looking, that
 * authentication email cannot be delivered.
 *
 * Three properties are worth pinning because each is how a notice like this goes
 * quietly wrong:
 *
 * - It must never raise an alarm it cannot back. `null` readiness (403 for a plain
 *   admin, 404 on an edition without the capability, a network error) is "unknown";
 *   treating it as "not deliverable" would show a false alarm to exactly the people who
 *   cannot fix it.
 * - Each context says its own thing. The copy lookup is a map, and a ternary would render
 *   the wrong context's text for a new value with every test still green.
 * - The tab's body names the real cause (never designated / deleted / disabled), not one
 *   sentence for all three.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/svelte';

vi.mock('$lib/axios', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
  isRequestCancelled: () => false,
}));

vi.mock('$stores/locale', () => ({
  t: { subscribe: (run: (value: (key: string) => string) => void) => (run((k) => k), () => {}) },
}));

import axiosInstance from '$lib/axios';
import type { AuthMailDesignation } from '$lib/api/authConfig';
import AuthMailNotice from './AuthMailNotice.svelte';

const get = vi.mocked(axiosInstance.get);

function readiness(overrides: Partial<AuthMailDesignation> = {}): AuthMailDesignation {
  return {
    config_uuid: null,
    config_name: null,
    provider: null,
    is_enabled: null,
    resolves: false,
    status: 'not_designated',
    env_smtp_configured: false,
    deliverable: false,
    verification_required: false,
    ...overrides,
  };
}

const textOf = (container: HTMLElement) => container.textContent ?? '';

beforeEach(() => vi.clearAllMocks());

describe('AuthMailNotice visibility', () => {
  it('shows when auth mail definitely cannot be delivered', () => {
    render(AuthMailNotice, { props: { context: 'tab', readiness: readiness() } });

    expect(screen.getByTestId('auth-mail-notice')).toBeInTheDocument();
  });

  it('renders nothing while auth mail is deliverable', () => {
    render(AuthMailNotice, {
      props: { context: 'tab', readiness: readiness({ deliverable: true }) },
    });

    expect(screen.queryByTestId('auth-mail-notice')).not.toBeInTheDocument();
  });

  it('renders nothing when the readiness could not be read', () => {
    // null is "unknown", not "broken": a plain admin gets 403 and an edition without
    // auth.config_ui gets 404 -- neither is evidence that mail is off.
    render(AuthMailNotice, { props: { context: 'invite', readiness: null } });

    expect(screen.queryByTestId('auth-mail-notice')).not.toBeInTheDocument();
  });

  it('renders nothing for an answer that does not say deliverable:false', () => {
    // A malformed or older answer must not read as an alarm.
    render(AuthMailNotice, { props: { context: 'tab', readiness: {} as never } });

    expect(screen.queryByTestId('auth-mail-notice')).not.toBeInTheDocument();
  });
});

describe('AuthMailNotice self-fetch (invite form)', () => {
  it('reads the designation itself when none is passed, and shows the alarm', async () => {
    get.mockResolvedValue({ data: readiness() } as never);
    render(AuthMailNotice, { props: { context: 'invite' } });

    expect(await screen.findByTestId('auth-mail-notice')).toBeInTheDocument();
    expect(get).toHaveBeenCalledWith('/admin/auth-config/email/designation');
  });

  it.each([403, 404, 500])('stays silent when the read fails with %i', async (status) => {
    get.mockRejectedValue({ response: { status } } as never);
    render(AuthMailNotice, { props: { context: 'invite' } });

    await waitFor(() => expect(get).toHaveBeenCalled());
    expect(screen.queryByTestId('auth-mail-notice')).not.toBeInTheDocument();
  });

  it('does not fetch when the caller supplied the readiness', () => {
    render(AuthMailNotice, { props: { context: 'tab', readiness: readiness() } });

    expect(get).not.toHaveBeenCalled();
  });
});

describe('AuthMailNotice copy', () => {
  it('says something different on the invite form than on the tab', () => {
    const invite = render(AuthMailNotice, { props: { context: 'invite', readiness: readiness() } });
    const inviteText = textOf(invite.container);
    const tab = render(AuthMailNotice, { props: { context: 'tab', readiness: readiness() } });
    const tabText = textOf(tab.container);

    expect(inviteText).toContain('authMailNotice.inviteBody');
    expect(inviteText).not.toContain('authMailNotice.tabNotDesignated');
    expect(tabText).toContain('authMailNotice.tabNotDesignated');
    expect(tabText).not.toContain('authMailNotice.inviteBody');
  });

  it.each([
    ['not_designated', 'authMailNotice.tabNotDesignated'],
    ['missing', 'authMailNotice.tabMissing'],
    ['disabled', 'authMailNotice.tabDisabled'],
  ] as const)('names the cause on the tab when the status is %s', (status, key) => {
    const { container } = render(AuthMailNotice, {
      props: { context: 'tab', readiness: readiness({ status, config_name: 'Old relay' }) },
    });

    const text = textOf(container);
    expect(text).toContain(key);
    for (const other of [
      'authMailNotice.tabNotDesignated',
      'authMailNotice.tabMissing',
      'authMailNotice.tabDisabled',
    ].filter((k) => k !== key)) {
      expect(text).not.toContain(other);
    }
  });

  it('lists what is affected on the tab, but not on the invite form', () => {
    const tab = render(AuthMailNotice, { props: { context: 'tab', readiness: readiness() } });
    expect(textOf(tab.container)).toContain('authMailNotice.affects');

    const invite = render(AuthMailNotice, { props: { context: 'invite', readiness: readiness() } });
    expect(textOf(invite.container)).not.toContain('authMailNotice.affects');
  });

  it('links to the deliverability page, opened safely', () => {
    render(AuthMailNotice, { props: { context: 'tab', readiness: readiness() } });

    const link = screen.getByRole('link') as HTMLAnchorElement;
    expect(link.getAttribute('href')).toBe(
      'https://docs.opentranscribe.app/docs/authentication/email-delivery'
    );
    expect(link.getAttribute('rel')).toContain('noopener');
  });
});

describe('AuthMailNotice severity', () => {
  it('escalates when email verification is required, because sign-in itself is blocked', () => {
    render(AuthMailNotice, {
      props: { context: 'tab', readiness: readiness({ verification_required: true }) },
    });

    expect(screen.getByTestId('auth-mail-notice').classList.contains('escalated')).toBe(true);
    expect(screen.getByTestId('auth-mail-notice-escalation')).toBeInTheDocument();
  });

  it('does not escalate when verification is not required', () => {
    render(AuthMailNotice, { props: { context: 'tab', readiness: readiness() } });

    expect(screen.getByTestId('auth-mail-notice').classList.contains('escalated')).toBe(false);
    expect(screen.queryByTestId('auth-mail-notice-escalation')).not.toBeInTheDocument();
  });
});
