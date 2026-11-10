/**
 * The instance privacy card lives on the Privacy & Redaction "policy" tab (issue #1280). That tab
 * is super_admin only (an admin sees it locked), so for anyone else the card must not mount and
 * must not call the super_admin endpoint.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/svelte';

vi.mock('$stores/locale', async () =>
  (await import('$testMocks/localeMocks')).keyLocale({ code: 'en' })
);

const api = vi.hoisted(() => ({ getInstance: vi.fn() }));
vi.mock('$lib/api/privacy', async (importOriginal) => {
  const actual = await importOriginal<typeof import('$lib/api/privacy')>();
  return { ...actual, PrivacyApi: api };
});
vi.mock('$stores/toast', async () => (await import('$testMocks/serviceMocks')).toastStoreMock());
vi.mock('$components/settings/ContentRedactionSettings.svelte', async () => ({
  default: (await import('$testMocks/EmptyComponent.svelte')).default,
}));
vi.mock('$components/settings/RedactionPolicySettings.svelte', async () => ({
  default: (await import('$testMocks/EmptyComponent.svelte')).default,
}));

vi.mock('$lib/api/telemetry', () => ({
  getTelemetryStatus: vi.fn().mockRejectedValue(new Error('not under test')),
  setTelemetryEnabled: vi.fn(),
}));
vi.mock('$lib/api/selfErasure', async (importOriginal) => ({
  ...(await importOriginal<typeof import('$lib/api/selfErasure')>()),
  getSelfErasureSettings: vi.fn().mockRejectedValue(new Error('not under test')),
}));

import PrivacyRedactionSettings from './PrivacyRedactionSettings.svelte';

beforeEach(() => {
  vi.clearAllMocks();
  api.getInstance.mockResolvedValue({
    voiceprint_matching_enabled: true,
    gender_inference_enabled: true,
    voiceprint_upload_consent_required: false,
  });
});

describe('PrivacyRedactionSettings instance privacy card', () => {
  it('mounts for a super administrator on the policy tab and reads the instance layer', async () => {
    render(PrivacyRedactionSettings, { initialTab: 'policy', isAdmin: true, isSuperAdmin: true });
    expect(await screen.findByTestId('instance-privacy-card')).toBeInTheDocument();
    await waitFor(() => expect(api.getInstance).toHaveBeenCalledTimes(1));
  });

  it('does not mount for an admin, who sees the tab locked, and makes no request', async () => {
    render(PrivacyRedactionSettings, { initialTab: 'policy', isAdmin: true, isSuperAdmin: false });
    await screen.findByTestId('privacy-redaction-panel');
    expect(screen.queryByTestId('instance-privacy-card')).toBeNull();
    expect(api.getInstance).not.toHaveBeenCalled();
  });

  it('does not mount for a plain user, who has no policy tab at all', async () => {
    render(PrivacyRedactionSettings, { initialTab: 'policy', isAdmin: false, isSuperAdmin: false });
    await screen.findByTestId('privacy-redaction-panel');
    expect(screen.queryByTestId('instance-privacy-card')).toBeNull();
    expect(api.getInstance).not.toHaveBeenCalled();
  });
});
