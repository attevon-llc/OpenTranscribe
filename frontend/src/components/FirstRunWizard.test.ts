import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/svelte';

const mockAxios = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), put: vi.fn() }));
vi.mock('$lib/axios', () => ({ default: mockAxios, isRequestCancelled: () => false }));

vi.mock('$stores/toast', () => ({
  toastStore: { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() },
}));

vi.mock('$stores/auth', async () => {
  const { readable } = await import('svelte/store');
  return { user: readable({ role: 'super_admin' }) };
});

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

import FirstRunWizard from './FirstRunWizard.svelte';
import { settingsModalStore } from '$stores/settingsModalStore';

const baseStatus = {
  completed_at: null,
  credential_kind: 'operator_set',
  password_min_age_hours: 24,
  password_changed_by_you: false,
};

function statusReturns(overrides: Record<string, unknown> = {}) {
  mockAxios.get.mockResolvedValue({ data: { ...baseStatus, ...overrides } });
}

const completeCalls = () =>
  mockAxios.post.mock.calls.filter(([url]) => url === '/admin/first-run-wizard/complete');

const stepOf = () => screen.getByTestId('wizard-step-indicator').getAttribute('data-step');

beforeEach(() => {
  vi.clearAllMocks();
  settingsModalStore.reset();
  mockAxios.post.mockResolvedValue({ data: {} });
  mockAxios.put.mockResolvedValue({ data: {} });
});

describe('FirstRunWizard step 1 credential state', () => {
  it('shows the strong warning only for the default dev credential', async () => {
    statusReturns({ credential_kind: 'default_dev' });
    render(FirstRunWizard);

    expect(await screen.findByTestId('cred-default-dev')).toBeTruthy();
    expect(screen.queryByTestId('cred-operator-set')).toBeNull();
  });

  it('shows a calm note for an operator-set password and no warning', async () => {
    statusReturns();
    render(FirstRunWizard);

    expect(await screen.findByTestId('cred-operator-set')).toBeTruthy();
    expect(screen.queryByTestId('cred-default-dev')).toBeNull();
  });

  it('says the change is already required for a generated password', async () => {
    statusReturns({ credential_kind: 'generated' });
    render(FirstRunWizard);

    expect(await screen.findByTestId('cred-generated')).toBeTruthy();
    expect(screen.queryByTestId('cred-default-dev')).toBeNull();
  });

  it('is not shown at all once the wizard has been completed', async () => {
    statusReturns({ completed_at: '2026-10-01T00:00:00Z' });
    render(FirstRunWizard);

    await waitFor(() => expect(mockAxios.get).toHaveBeenCalled());
    expect(screen.queryByTestId('wizard-step-indicator')).toBeNull();
  });
});

describe('FirstRunWizard keep-current-password choice', () => {
  it('advances to step 2 without recording completion and marks the password step done', async () => {
    statusReturns();
    render(FirstRunWizard);

    await fireEvent.click(await screen.findByText('firstRunWizard.keepPasswordButton'));

    expect(stepOf()).toBe('2');
    expect(completeCalls()).toHaveLength(0);
    const first = screen.getByText('firstRunWizard.stepPassword').closest('li');
    expect(first?.getAttribute('data-done')).toBe('true');
  });
});

describe('FirstRunWizard resume after Settings', () => {
  async function openProfile() {
    await fireEvent.click(await screen.findByText('firstRunWizard.changePasswordButton'));
    await waitFor(() => {
      let open = false;
      settingsModalStore.subscribe((s) => (open = s.isOpen))();
      expect(open).toBe(true);
    });
    expect(screen.queryByTestId('wizard-step-indicator')).toBeNull();
  }

  it('reopens at step 2 when the password was changed meanwhile', async () => {
    statusReturns();
    render(FirstRunWizard);
    await openProfile();
    statusReturns({ password_changed_by_you: true });
    settingsModalStore.close();

    await waitFor(() => expect(stepOf()).toBe('2'));
    expect(completeCalls()).toHaveLength(0);
  });

  it('reopens at step 1 with an unchanged note when nothing changed', async () => {
    statusReturns();
    render(FirstRunWizard);
    await openProfile();
    settingsModalStore.close();

    expect(await screen.findByTestId('password-unchanged')).toBeTruthy();
    expect(stepOf()).toBe('1');
  });
});

describe('FirstRunWizard navigation and completion', () => {
  async function toStep3() {
    statusReturns();
    render(FirstRunWizard);
    await fireEvent.click(await screen.findByText('firstRunWizard.keepPasswordButton'));
    await fireEvent.click(screen.getByText('common.next'));
  }

  it('Back returns to the previous step', async () => {
    await toStep3();
    expect(stepOf()).toBe('3');

    await fireEvent.click(screen.getByText('common.back'));
    expect(stepOf()).toBe('2');
    await fireEvent.click(screen.getByText('common.back'));
    expect(stepOf()).toBe('1');
  });

  it('records completion only when Finish is pressed on the last step', async () => {
    await toStep3();
    await fireEvent.click(screen.getByText('firstRunWizard.applyButton'));

    await waitFor(() => expect(stepOf()).toBe('4'));
    expect(screen.getByTestId('wizard-summary')).toBeTruthy();
    expect(completeCalls()).toHaveLength(0);

    await fireEvent.click(screen.getByText('firstRunWizard.finishButton'));
    await waitFor(() => expect(completeCalls()).toHaveLength(1));
  });

  it('explicit Skip records completion and states what is skipped', async () => {
    statusReturns();
    render(FirstRunWizard);
    await screen.findByText('firstRunWizard.skipHint');

    await fireEvent.click(screen.getByText('firstRunWizard.skipButton'));
    await waitFor(() => expect(completeCalls()).toHaveLength(1));
  });
});

describe('FirstRunWizard terms step', () => {
  const ACK_MD = [
    '# Operator acknowledgment',
    '',
    'You are responsible.',
    '',
    'Checkbox text:',
    '> I accept responsibility as operator.',
  ].join('\n');
  const ackDoc = {
    key: 'opentranscribe-operator-acknowledgment',
    title: 'Operator acknowledgment',
    version: '1.0',
    status: 'draft',
    sha256: 'abc123def456',
    markdown: ACK_MD,
  };

  function legalStatus(overrides: Record<string, unknown> = {}, doc = ackDoc) {
    mockAxios.get.mockImplementation((url: string) => {
      if (url === '/legal/documents/opentranscribe-operator-acknowledgment') {
        return Promise.resolve({ data: doc });
      }
      return Promise.resolve({
        data: { ...baseStatus, legal_required: true, legal_acknowledged: false, ...overrides },
      });
    });
  }

  const indicatorText = () => screen.getByTestId('wizard-step-indicator').textContent;

  it('shows the terms step first, as step 1 of 5', async () => {
    legalStatus();
    render(FirstRunWizard);

    expect(await screen.findByTestId('wizard-terms-step')).toBeTruthy();
    expect(stepOf()).toBe('1');
    expect(indicatorText()).toContain('firstRunWizard.stepTerms');
    expect(screen.getAllByRole('listitem').length).toBeGreaterThanOrEqual(5);
    expect(screen.queryByText('firstRunWizard.skipButton')).toBeNull();
  });

  it('puts the acceptance checkbox in the footer, outside the scrollable panel', async () => {
    legalStatus();
    render(FirstRunWizard);

    const box = await screen.findByTestId('terms-checkbox');
    const footer = screen.getByTestId('terms-footer');
    expect(footer.contains(box)).toBe(true);
    expect(footer.contains(screen.getByTestId('terms-accept-button'))).toBe(true);
    expect(screen.getByTestId('terms-panel').contains(box)).toBe(false);
  });

  it('is skipped when the operator already acknowledged: 4 steps starting at password', async () => {
    legalStatus({ legal_acknowledged: true });
    render(FirstRunWizard);

    expect(await screen.findByText('firstRunWizard.keepPasswordButton')).toBeTruthy();
    expect(screen.queryByTestId('wizard-terms-step')).toBeNull();
    expect(indicatorText()).not.toContain('firstRunWizard.stepTerms');
    expect(screen.getByTestId('wizard-step-indicator').querySelectorAll('li')).toHaveLength(4);
  });

  it('is skipped when legal is not required (cloud edition)', async () => {
    legalStatus({ legal_required: false });
    render(FirstRunWizard);

    expect(await screen.findByText('firstRunWizard.keepPasswordButton')).toBeTruthy();
    expect(screen.queryByTestId('wizard-terms-step')).toBeNull();
    expect(screen.getByTestId('wizard-step-indicator').querySelectorAll('li')).toHaveLength(4);
  });

  it('keeps accept disabled until the extracted checkbox sentence is ticked, then POSTs the acknowledgment', async () => {
    legalStatus();
    render(FirstRunWizard);

    const accept = (await screen.findByTestId('terms-accept-button')) as HTMLButtonElement;
    const label = (await screen.findByTestId('terms-checkbox')).closest('label');
    expect(label?.textContent?.trim()).toBe('I accept responsibility as operator.');
    expect(accept.disabled).toBe(true);

    await fireEvent.click(screen.getByTestId('terms-checkbox'));
    expect(accept.disabled).toBe(false);

    await fireEvent.click(accept);
    await waitFor(() =>
      expect(mockAxios.post).toHaveBeenCalledWith('/legal/acknowledgments', {
        key: 'opentranscribe-operator-acknowledgment',
        version: '1.0',
        sha256: 'abc123def456',
      })
    );
    await waitFor(() => expect(screen.getByTestId('wizard-step-indicator')).toBeTruthy());
    await waitFor(() => expect(stepOf()).toBe('2'));
    expect(completeCalls()).toHaveLength(0);
  });

  it('stays on the terms step and toasts when the acknowledgment fails', async () => {
    legalStatus();
    mockAxios.post.mockRejectedValue(new Error('boom'));
    const { toastStore } = await import('$stores/toast');
    render(FirstRunWizard);

    await fireEvent.click(await screen.findByTestId('terms-checkbox'));
    await fireEvent.click(screen.getByTestId('terms-accept-button'));

    await waitFor(() => expect(toastStore.error).toHaveBeenCalled());
    expect(stepOf()).toBe('1');
    expect(screen.getByTestId('wizard-terms-step')).toBeTruthy();
  });

  it('shows the draft badge only for a draft document', async () => {
    legalStatus();
    const { unmount } = render(FirstRunWizard);
    expect(await screen.findByTestId('terms-draft-badge')).toBeTruthy();
    unmount();

    legalStatus({}, { ...ackDoc, status: 'final' });
    render(FirstRunWizard);
    await screen.findByTestId('terms-panel');
    expect(screen.queryByTestId('terms-draft-badge')).toBeNull();
  });
});
