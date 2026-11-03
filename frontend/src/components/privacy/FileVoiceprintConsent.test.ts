/**
 * The owner's per-file voice identification control (issue #1356): owner only, an opt-out asks
 * first and then follows the purge, and opting back in records the statement and says to reprocess.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/svelte';

vi.mock('$stores/locale', async () => {
  const { readable } = await import('svelte/store');
  const en = (await import('$lib/i18n/locales/en.json')).default as Record<string, string>;
  return { t: readable((key: string) => en[key] ?? key), locale: readable('en') };
});

const api = vi.hoisted(() => ({
  setFileVoiceprintConsent: vi.fn(),
  getPurgeStatus: vi.fn(),
}));
vi.mock('$lib/api/privacy', async (importOriginal) => {
  const actual = await importOriginal<typeof import('$lib/api/privacy')>();
  return { ...actual, PrivacyApi: api };
});
const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn() }));
vi.mock('$stores/toast', () => ({ toastStore: toast }));

import FileVoiceprintConsent from './FileVoiceprintConsent.svelte';
import enMessages from '$lib/i18n/locales/en.json';

beforeEach(() => {
  vi.clearAllMocks();
});

async function optOut() {
  await fireEvent.click(screen.getByTestId('file-voiceprint-opt-out'));
  await fireEvent.click(screen.getByText('Turn off and delete voiceprints'));
}

describe('FileVoiceprintConsent', () => {
  it('renders nothing for a non-owner', () => {
    render(FileVoiceprintConsent, { props: { fileUuid: 'f-1', isOwner: false } });
    expect(screen.queryByTestId('file-voiceprint-consent')).toBeNull();
  });

  it('does not call the API until the opt-out is confirmed', async () => {
    api.setFileVoiceprintConsent.mockResolvedValue({
      state: 'declined',
      previous_state: 'granted',
      changed: true,
      purge_task_id: null,
      reprocess_to_create_voiceprints: false,
    });
    render(FileVoiceprintConsent, { props: { fileUuid: 'f-1', state: 'granted', isOwner: true } });

    await fireEvent.click(screen.getByTestId('file-voiceprint-opt-out'));
    expect(api.setFileVoiceprintConsent).not.toHaveBeenCalled();

    await fireEvent.click(screen.getByText('Turn off and delete voiceprints'));
    await waitFor(() =>
      expect(api.setFileVoiceprintConsent).toHaveBeenCalledWith('f-1', 'declined')
    );
    await waitFor(() =>
      expect(screen.getByTestId('file-voiceprint-consent-state').textContent).toMatch(
        /Turned off for this recording/
      )
    );
  });

  it('follows the purge to its end and reports it', async () => {
    api.setFileVoiceprintConsent.mockResolvedValue({
      state: 'declined',
      previous_state: 'unspecified',
      changed: true,
      purge_task_id: 't-1',
      reprocess_to_create_voiceprints: false,
    });
    api.getPurgeStatus.mockResolvedValue({
      task_id: 't-1',
      status: 'completed',
      residual_ok: true,
    });
    render(FileVoiceprintConsent, { props: { fileUuid: 'f-1', isOwner: true } });

    await optOut();

    await waitFor(() => expect(api.getPurgeStatus).toHaveBeenCalledWith('t-1'));
    await waitFor(() => expect(toast.success).toHaveBeenCalled());
  });

  it('says the purge did not finish when a residual remains', async () => {
    api.setFileVoiceprintConsent.mockResolvedValue({
      state: 'declined',
      previous_state: 'granted',
      changed: true,
      purge_task_id: 't-2',
      reprocess_to_create_voiceprints: false,
    });
    api.getPurgeStatus.mockResolvedValue({ task_id: 't-2', status: 'failed', residual_ok: false });
    render(FileVoiceprintConsent, { props: { fileUuid: 'f-1', state: 'granted', isOwner: true } });

    await optOut();

    await waitFor(() => expect(toast.error).toHaveBeenCalled());
    expect(toast.success).not.toHaveBeenCalled();
  });

  it('opting back in records the statement and tells the owner to reprocess', async () => {
    api.setFileVoiceprintConsent.mockResolvedValue({
      state: 'granted',
      previous_state: 'declined',
      changed: true,
      purge_task_id: null,
      reprocess_to_create_voiceprints: true,
    });
    render(FileVoiceprintConsent, { props: { fileUuid: 'f-1', state: 'declined', isOwner: true } });

    await fireEvent.click(screen.getByText('Record that the speakers agreed'));

    await waitFor(() =>
      expect(api.setFileVoiceprintConsent).toHaveBeenCalledWith('f-1', 'granted')
    );
    await waitFor(() => expect(toast.success).toHaveBeenCalled());
    expect(api.getPurgeStatus).not.toHaveBeenCalled();
  });

  it('keeps the old state and reports an error when the save fails', async () => {
    api.setFileVoiceprintConsent.mockRejectedValue(new Error('500'));
    render(FileVoiceprintConsent, { props: { fileUuid: 'f-1', state: 'granted', isOwner: true } });

    await optOut();

    await waitFor(() => expect(toast.error).toHaveBeenCalled());
    expect(screen.getByTestId('file-voiceprint-consent-state').textContent).toMatch(/agreed/);
  });

  const reject = (status: number, detail: unknown) =>
    api.setFileVoiceprintConsent.mockRejectedValue({ response: { status, data: { detail } } });
  const en = (key: string) => (enMessages as Record<string, string>)[key];

  it('says a held recording cannot change, without suggesting a retry', async () => {
    reject(409, { error: 'FILE_UNDER_LEGAL_HOLD', message: 'held', held_by: ['organization'] });
    render(FileVoiceprintConsent, { props: { fileUuid: 'f-1', state: 'granted', isOwner: true } });

    await optOut();

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(en('file.voiceprintConsent.heldRefusal'))
    );
    expect(screen.getByTestId('file-voiceprint-consent-state').textContent).toMatch(/agreed/);
  });

  it('says a quarantined or missing recording is no longer available', async () => {
    reject(404, 'File not found');
    render(FileVoiceprintConsent, { props: { fileUuid: 'f-1', state: 'granted', isOwner: true } });

    await optOut();

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(en('file.voiceprintConsent.unavailable'))
    );
  });

  it('shows the opt-out as saved when only the purge could not be queued (503)', async () => {
    reject(503, 'Could not queue the deletion');
    render(FileVoiceprintConsent, { props: { fileUuid: 'f-1', state: 'granted', isOwner: true } });

    await optOut();

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(en('file.voiceprintConsent.purgePending'))
    );
    expect(screen.getByTestId('file-voiceprint-consent-state').textContent).toMatch(
      /Turned off for this recording/
    );
  });

  it('surfaces the support-grant refusal sentence', async () => {
    reject(403, {
      code: 'support_grant_action_not_permitted',
      message: 'Not permitted during support access.',
    });
    render(FileVoiceprintConsent, { props: { fileUuid: 'f-1', state: 'granted', isOwner: true } });

    await optOut();

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        'This action is not available during a support session.'
      )
    );
  });

  it('says voiceprints were kept when the purge aborts for protected recordings', async () => {
    api.setFileVoiceprintConsent.mockResolvedValue({
      state: 'declined',
      previous_state: 'granted',
      changed: true,
      purge_task_id: 't-3',
      reprocess_to_create_voiceprints: false,
    });
    api.getPurgeStatus.mockResolvedValue({
      task_id: 't-3',
      status: 'failed',
      reason: 'files_protected',
      residual_ok: null,
    });
    render(FileVoiceprintConsent, { props: { fileUuid: 'f-1', state: 'granted', isOwner: true } });

    await optOut();

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(enMessages['file.voiceprintConsent.purgeProtected'])
    );
    expect(toast.error).not.toHaveBeenCalledWith(enMessages['file.voiceprintConsent.purgeFailed']);
  });
});
