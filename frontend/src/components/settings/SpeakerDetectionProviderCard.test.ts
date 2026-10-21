import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render } from '@testing-library/svelte';

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string, vars?: unknown) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));
vi.mock('$stores/toast', () => ({
  toastStore: { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() },
}));
const key = vi.hoisted(() => ({
  getPyannoteCredential: vi.fn(),
  savePyannoteCredential: vi.fn(),
  deletePyannoteCredential: vi.fn(),
  testPyannoteCredential: vi.fn(),
}));
vi.mock('$lib/api/pyannoteCredential', () => key);

import SpeakerDetectionProviderCard from './SpeakerDetectionProviderCard.svelte';

const STATUS = {
  configured: false,
  locked: false,
  test_status: null,
  test_message: null,
  last_tested: null,
  updated_at: null,
};

beforeEach(() => {
  vi.clearAllMocks();
  key.getPyannoteCredential.mockResolvedValue({ ...STATUS });
});

const line = (c: HTMLElement) =>
  c.querySelector('[data-testid="detection-credential-line"]') as HTMLElement | null;
const form = (c: HTMLElement) => c.querySelector('[data-testid="pyannote-credential-form"]');
const text = (el: Element | null) => el?.textContent ?? '';

describe('SpeakerDetectionProviderCard', () => {
  it('titles the card as a provider choice and keeps the select inside it', () => {
    const { container } = render(SpeakerDetectionProviderCard, {
      props: { diarizationSource: 'provider' },
    });
    const card = container.querySelector('.settings-section') as HTMLElement;
    expect(text(card.querySelector('.section-title'))).toBe(
      'settings.speakerIdentification.detectionHeading'
    );
    expect(card.querySelector('#diarization-source')).not.toBeNull();
  });

  it('Automatic points at the transcription provider key instead of asking for one', async () => {
    const { container } = render(SpeakerDetectionProviderCard, {
      props: { diarizationSource: 'provider' },
    });

    expect(text(line(container))).toContain('settings.speakerIdentification.credential.provider');
    expect(form(container)).toBeNull();
    expect(key.getPyannoteCredential).not.toHaveBeenCalled();
    expect(line(container)?.querySelector('button')).not.toBeNull();
  });

  it('Always on this server says no key is needed and shows no form', () => {
    const { container } = render(SpeakerDetectionProviderCard, {
      props: { diarizationSource: 'local' },
    });
    expect(text(line(container))).toContain('settings.speakerIdentification.credential.local');
    expect(line(container)?.querySelector('button')).toBeNull();
    expect(form(container)).toBeNull();
  });

  it('pyannote.ai reveals the credential form with its status badge under the select', async () => {
    const { container } = render(SpeakerDetectionProviderCard, {
      props: { diarizationSource: 'pyannote' },
    });
    const credentials = await vi.waitFor(() => {
      const el = form(container);
      expect(el).not.toBeNull();
      expect(el!.querySelector('[data-testid="pyannote-badge"]')).not.toBeNull();
      return el as HTMLElement;
    });
    expect(text(credentials.querySelector('.credential-title'))).toBe(
      'settings.speakerIdentification.pyannoteKey.heading'
    );
    expect(
      (container.querySelector('#diarization-source') as HTMLElement).compareDocumentPosition(
        credentials
      ) & Node.DOCUMENT_POSITION_FOLLOWING
    ).toBeTruthy();
  });

  it('pyannote.ai without bring-your-own-key support shows the requirement but no form', () => {
    const { container } = render(SpeakerDetectionProviderCard, {
      props: { diarizationSource: 'pyannote', keyEnabled: false },
    });
    expect(text(line(container))).toContain('settings.speakerIdentification.source.pyannoteHint');
    expect(form(container)).toBeNull();
  });

  it('Off shows its warning and nothing about credentials', () => {
    const { container } = render(SpeakerDetectionProviderCard, {
      props: { diarizationSource: 'off' },
    });
    expect(line(container)).toBeNull();
    expect(form(container)).toBeNull();
    expect(text(container)).toContain('settings.speakerIdentification.source.offWarning');
  });
});
