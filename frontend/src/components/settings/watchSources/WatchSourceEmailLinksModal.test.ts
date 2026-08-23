/**
 * `WatchSourceEmailLinksModal` is #490.
 *
 * The load-bearing behaviour is not the linking itself — it is telling the user when a
 * link they just made will deliver **nothing**. Three states look configured and send
 * no mail, and two of them are invisible from the link alone.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/svelte';

const api = vi.hoisted(() => ({
  getEmailLinks: vi.fn(),
  getAvailableEmailConfigs: vi.fn(),
  linkEmailConfig: vi.fn(),
  unlinkEmailConfig: vi.fn(),
}));

vi.mock('$lib/api/watchSourcesApi', async () => {
  const actual = await vi.importActual<typeof import('$lib/api/watchSourcesApi')>(
    '$lib/api/watchSourcesApi'
  );
  return { ...actual, ...api };
});

const mockToast = vi.hoisted(() => ({
  success: vi.fn(),
  error: vi.fn(),
  warning: vi.fn(),
  info: vi.fn(),
}));
vi.mock('$stores/toast', () => ({ toastStore: mockToast }));

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string, vars?: Record<string, unknown>) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

import WatchSourceEmailLinksModal from './WatchSourceEmailLinksModal.svelte';

const SOURCE = { uuid: 'w1', name: 'NAS recordings' } as never;

const HEALTHY_OPTION = {
  uuid: 'e1',
  name: 'Ops mailer',
  provider: 'smtp',
  is_enabled: true,
  has_default_recipients: true,
};

/**
 * A linked config is **absent** from the picker — the backend excludes it — so the
 * link row is the only place its `config_*` facts come from. An earlier draft of
 * these tests returned the config in both lists, which is a state the API cannot
 * produce, and it hid a real bug: the two config-side warnings could never fire.
 */
function link(overrides: Record<string, unknown> = {}) {
  return {
    email_config_uuid: 'e1',
    email_config_name: 'Ops mailer',
    email_config_provider: 'smtp',
    config_is_enabled: true,
    config_has_default_recipients: true,
    additional_recipients: null,
    notify_on_success: true,
    notify_on_error: true,
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  api.getEmailLinks.mockResolvedValue([]);
  api.getAvailableEmailConfigs.mockResolvedValue([HEALTHY_OPTION]);
  api.linkEmailConfig.mockResolvedValue(undefined);
  api.unlinkEmailConfig.mockResolvedValue(undefined);
});

function open() {
  return render(WatchSourceEmailLinksModal, { props: { show: true, source: SOURCE } } as never);
}

describe('loading', () => {
  it('reads the links and the source-scoped picker together', async () => {
    open();
    await waitFor(() => expect(api.getEmailLinks).toHaveBeenCalledWith('w1'));
    expect(api.getAvailableEmailConfigs).toHaveBeenCalledWith('w1');
  });

  it('renders each link with the options that link carries', async () => {
    api.getEmailLinks.mockResolvedValue([
      link({ notify_on_success: false, additional_recipients: 'oncall@example.com' }),
    ]);
    api.getAvailableEmailConfigs.mockResolvedValue([]);
    open();

    expect(await screen.findByText('Ops mailer')).toBeInTheDocument();
    expect(
      screen.getByLabelText('settings.emailNotifications.links.notifyOnSuccess')
    ).not.toBeChecked();
    expect(screen.getByLabelText('settings.emailNotifications.links.notifyOnError')).toBeChecked();
    expect(screen.getByDisplayValue('oncall@example.com')).toBeInTheDocument();
  });

  it('says an admin must create a config when none exist at all', async () => {
    // An owner cannot create one, so a bare empty dropdown reads as a broken page
    // rather than as "somebody else has to do this first".
    api.getEmailLinks.mockResolvedValue([]);
    api.getAvailableEmailConfigs.mockResolvedValue([]);
    open();

    expect(
      await screen.findByText('settings.emailNotifications.links.noConfigsExist')
    ).toBeInTheDocument();
  });
});

describe('warnings for links that deliver nothing', () => {
  it('warns when both notify options are off', async () => {
    api.getEmailLinks.mockResolvedValue([
      link({ notify_on_success: false, notify_on_error: false }),
    ]);
    api.getAvailableEmailConfigs.mockResolvedValue([]);
    open();

    expect(
      await screen.findByText('settings.emailNotifications.links.warnNoEvents')
    ).toBeInTheDocument();
  });

  it('warns when the linked config is disabled', async () => {
    // Invisible from the link's OWN options: the flags are on and recipients are set;
    // the only thing stopping delivery is a boolean on a config the owner may not
    // even be able to see, and which the picker no longer lists.
    api.getEmailLinks.mockResolvedValue([link({ config_is_enabled: false })]);
    api.getAvailableEmailConfigs.mockResolvedValue([]);
    open();

    expect(
      await screen.findByText('settings.emailNotifications.links.warnConfigDisabled')
    ).toBeInTheDocument();
  });

  it('warns when neither the config nor the link supplies a recipient', async () => {
    // Both halves look complete in isolation; only their combination is empty.
    api.getEmailLinks.mockResolvedValue([
      link({ config_has_default_recipients: false, additional_recipients: null }),
    ]);
    api.getAvailableEmailConfigs.mockResolvedValue([]);
    open();

    expect(
      await screen.findByText('settings.emailNotifications.links.warnNoRecipients')
    ).toBeInTheDocument();
  });

  it('shows no warning on a healthy link', async () => {
    // The negative control: warnings rendered unconditionally would satisfy all three
    // tests above while telling the user nothing.
    api.getEmailLinks.mockResolvedValue([link()]);
    api.getAvailableEmailConfigs.mockResolvedValue([]);
    open();

    expect(await screen.findByText('Ops mailer')).toBeInTheDocument();
    expect(
      screen.queryByText('settings.emailNotifications.links.warnNoEvents')
    ).not.toBeInTheDocument();
    expect(
      screen.queryByText('settings.emailNotifications.links.warnConfigDisabled')
    ).not.toBeInTheDocument();
    expect(
      screen.queryByText('settings.emailNotifications.links.warnNoRecipients')
    ).not.toBeInTheDocument();
  });
});

describe('saving', () => {
  it('upserts the whole link, since the backend POST is the edit path too', async () => {
    api.getEmailLinks.mockResolvedValue([link({ notify_on_success: false })]);
    api.getAvailableEmailConfigs.mockResolvedValue([]);
    open();
    expect(await screen.findByText('Ops mailer')).toBeInTheDocument();

    await fireEvent.click(screen.getByText('common.save'));

    await waitFor(() =>
      expect(api.linkEmailConfig).toHaveBeenCalledWith('w1', {
        email_config_uuid: 'e1',
        additional_recipients: null,
        notify_on_success: false,
        notify_on_error: true,
      })
    );
  });

  it('refuses a malformed recipient before the round trip', async () => {
    // Mirrors the backend validator. Without it the address is accepted, dropped at
    // send time, and the link goes on claiming it notifies someone it never reaches.
    api.getEmailLinks.mockResolvedValue([link({ additional_recipients: 'not-an-address' })]);
    api.getAvailableEmailConfigs.mockResolvedValue([]);
    open();
    expect(await screen.findByText('Ops mailer')).toBeInTheDocument();

    await fireEvent.click(screen.getByText('common.save'));

    expect(mockToast.error).toHaveBeenCalledWith(
      'settings.emailNotifications.links.invalidRecipients'
    );
    expect(api.linkEmailConfig).not.toHaveBeenCalled();
  });

  it('attaches the picked config and reloads', async () => {
    open();
    await waitFor(() => expect(api.getAvailableEmailConfigs).toHaveBeenCalled());

    await fireEvent.change(screen.getByLabelText('settings.emailNotifications.links.addLabel'), {
      target: { value: 'e1' },
    });
    await fireEvent.click(screen.getByText('settings.emailNotifications.links.add'));

    await waitFor(() =>
      expect(api.linkEmailConfig).toHaveBeenCalledWith('w1', { email_config_uuid: 'e1' })
    );
    expect(api.getEmailLinks).toHaveBeenCalledTimes(2);
  });

  it('unlinks a config without touching the config itself', async () => {
    api.getEmailLinks.mockResolvedValue([link()]);
    api.getAvailableEmailConfigs.mockResolvedValue([]);
    open();
    expect(await screen.findByText('Ops mailer')).toBeInTheDocument();

    await fireEvent.click(screen.getByText('settings.emailNotifications.links.unlink'));

    await waitFor(() => expect(api.unlinkEmailConfig).toHaveBeenCalledWith('w1', 'e1'));
  });
});
