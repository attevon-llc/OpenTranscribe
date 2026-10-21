import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, fireEvent } from '@testing-library/svelte';

vi.mock('$stores/locale', () => {
  const t = (key: string, vars?: Record<string, unknown>) =>
    vars ? `${key}${JSON.stringify(vars)}` : key;
  return { t: { subscribe: (run: (v: typeof t) => void) => (run(t), () => {}) } };
});
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

import Host from './SpeakerDetectionProviderCardTestHost.svelte';
import { fullLocalView, liteCloudView, twoVendorView } from '$lib/settings/pipelineViewFixtures';
import type { DiarizerChoice } from '$lib/settings/asrCapabilities';

const CHOICES: DiarizerChoice[] = [
  {
    uuid: 'dg',
    name: 'Deepgram work',
    provider: 'deepgram',
    providerLabel: 'Deepgram',
    modelName: 'nova-3',
    owner: null,
    returnsSpeakers: true,
    timing: 'word',
  },
  {
    uuid: 'oa',
    name: 'OpenAI diarize',
    provider: 'openai',
    providerLabel: 'OpenAI',
    modelName: 'gpt-4o-transcribe-diarize',
    owner: null,
    returnsSpeakers: true,
    timing: 'segment',
  },
];

beforeEach(() => {
  vi.clearAllMocks();
  key.getPyannoteCredential.mockResolvedValue({
    configured: false,
    locked: false,
    test_status: null,
    test_message: null,
    last_tested: null,
    updated_at: null,
  });
});

const select = (c: HTMLElement) => c.querySelector('#diarization-source') as HTMLSelectElement;
const optionValues = (c: HTMLElement) => Array.from(select(c).options).map((o) => o.value);
const notices = (c: HTMLElement) =>
  Array.from(c.querySelectorAll<HTMLElement>('[data-testid="selection-notice"]'));
const codes = (c: HTMLElement) => notices(c).map((n) => n.dataset.code);
const where = (c: HTMLElement) => c.querySelector('[data-testid="detection-where"]');
const text = (el: Element | null) => el?.textContent?.replace(/\s+/g, ' ').trim() ?? '';

/** A full-image view whose transcription model returns text only, with the given timing. */
function textOnlyView(timing: 'word' | 'segment' | 'none', mode: 'full' | 'lite' = 'full') {
  const view = mode === 'full' ? fullLocalView() : liteCloudView();
  view.steps[0] = { ...view.steps[0], returns_speakers: false, timing };
  return view;
}

describe('speaker sources on a full deployment', () => {
  it('offers this server and pyannote.ai, and another provider only with the capability', () => {
    const { container, unmount } = render(Host, {
      props: { pipeline: fullLocalView(), choices: CHOICES, keyEnabled: true },
    });
    expect(optionValues(container)).toEqual(['provider', 'local', 'pyannote', 'asr_config', 'off']);
    unmount();
    const withoutCap = render(Host, {
      props: { pipeline: fullLocalView(), choices: CHOICES, keyEnabled: false },
    });
    expect(optionValues(withoutCap.container)).not.toContain('asr_config');
  });
});

describe('lite deployment', () => {
  it('does not render the local option at all', () => {
    const { container } = render(Host, { props: { pipeline: liteCloudView() } });
    expect(optionValues(container)).toEqual(['provider', 'pyannote', 'asr_config', 'off']);
  });

  it('shows a stored local selection as unavailable, with the reason and a way out', async () => {
    const onNavigate = vi.fn();
    const { container } = render(Host, {
      props: { pipeline: liteCloudView(), diarizationSource: 'local', onNavigate },
    });
    const local = Array.from(select(container).options).find((o) => o.value === 'local')!;
    expect(local.disabled).toBe(true);
    expect(codes(container)).toEqual(['local_diarization_unavailable']);
    expect(notices(container)[0].dataset.severity).toBe('error');
    expect(text(where(container))).toBe('');
  });
});

describe('Automatic with a text-only transcription model (#1219)', () => {
  it('tells the user speakers will be detected on this server, with segment timing noted', () => {
    const { container } = render(Host, { props: { pipeline: textOnlyView('segment') } });
    expect(codes(container)).toEqual(['automatic_uses_server', 'segment_timing_only']);
    expect(text(where(container))).toContain('settings.pipeline.where.server');
  });

  it('is an error with no timing, linking to the provider tab that fixes it', async () => {
    const onNavigate = vi.fn();
    const { container } = render(Host, {
      props: { pipeline: textOnlyView('none'), onNavigate },
    });
    expect(codes(container)).toEqual(['asr_no_timing_for_speakers']);
    expect(notices(container)[0].dataset.severity).toBe('error');
    const link = container.querySelector('[data-testid="selection-notice-link"]') as HTMLElement;
    expect(link.dataset.section).toBe('asr-provider');
    await fireEvent.click(link);
    expect(onNavigate).toHaveBeenCalledWith('asr-provider');
  });

  it('on lite steers to another speaker provider instead', () => {
    const { container } = render(Host, { props: { pipeline: textOnlyView('word', 'lite') } });
    expect(codes(container)).toEqual(['automatic_needs_speaker_provider']);
  });

  it('shows no notice when the model returns speakers with word timing', () => {
    const { container } = render(Host, { props: { pipeline: fullLocalView() } });
    expect(notices(container)).toHaveLength(0);
  });

  it('re-evaluates the notice as the selection changes, without saving', async () => {
    const { container } = render(Host, { props: { pipeline: textOnlyView('none') } });
    expect(codes(container)).toEqual(['asr_no_timing_for_speakers']);
    await fireEvent.change(select(container), { target: { value: 'off' } });
    expect(notices(container)).toHaveLength(0);
  });
});

describe('where the chosen source runs', () => {
  it('names pyannote.ai as the cloud vendor and this server for local', async () => {
    const { container } = render(Host, { props: { pipeline: fullLocalView() } });
    await fireEvent.change(select(container), { target: { value: 'pyannote' } });
    expect(text(where(container))).toBe('settings.pipeline.where.cloud{"vendor":"pyannote.ai"}');
    await fireEvent.change(select(container), { target: { value: 'local' } });
    expect(text(where(container))).toBe('settings.pipeline.where.server');
  });
});

describe('another provider as the speaker detector', () => {
  it('lists the offered configs, defaults to the first, and warns about billing and audio', async () => {
    const { container, getByTestId } = render(Host, {
      props: { pipeline: twoVendorView(), choices: CHOICES, diarizationSource: 'asr_config' },
    });
    const picker = container.querySelector('#diarization-asr-config') as HTMLSelectElement;
    expect(Array.from(picker.options).map((o) => o.value)).toEqual(['dg', 'oa']);
    expect(picker.value).toBe('dg');
    expect(getByTestId('host-uuid').textContent).toBe('dg');
    // A different vendor than the one that transcribes: the full transcription it bills for is
    // said before saving, not discovered on the invoice.
    expect(codes(container)).toContain('diarizer_double_billing');
    expect(text(container)).toContain('settings.speakerIdentification.source.asrConfigDesc');
    expect(text(where(container))).toBe('settings.pipeline.where.cloud{"vendor":"Deepgram"}');
  });

  it('follows the picked config', async () => {
    const { container, getByTestId } = render(Host, {
      props: {
        pipeline: fullLocalView(),
        choices: CHOICES,
        diarizationSource: 'asr_config',
        diarizationAsrConfigUuid: 'dg',
      },
    });
    await fireEvent.change(
      container.querySelector('#diarization-asr-config') as HTMLSelectElement,
      {
        target: { value: 'oa' },
      }
    );
    expect(getByTestId('host-uuid').textContent).toBe('oa');
    expect(text(where(container))).toContain('OpenAI');
  });

  it('says no saved provider returns speakers, with a link to add one', async () => {
    const onNavigate = vi.fn();
    const { container } = render(Host, {
      props: {
        pipeline: fullLocalView(),
        choices: [],
        diarizationSource: 'asr_config',
        onNavigate,
      },
    });
    expect(container.querySelector('#diarization-asr-config')).toBeNull();
    expect(text(container)).toContain('settings.speakerIdentification.source.asrConfigNone');
    await fireEvent.click(
      container.querySelector('[data-testid="asr-config-add-link"]') as HTMLElement
    );
    expect(onNavigate).toHaveBeenCalledWith('asr-provider');
  });

  it('does not render the picker for any other source', () => {
    const { container } = render(Host, { props: { pipeline: fullLocalView(), choices: CHOICES } });
    expect(container.querySelector('#diarization-asr-config')).toBeNull();
  });
});

describe('source locked by the administrator', () => {
  it('is read-only and says the deployment chose it', () => {
    const { container } = render(Host, {
      props: { pipeline: fullLocalView({ diarization_source_locked: true }), locked: true },
    });
    expect(select(container).disabled).toBe(true);
    expect(text(container)).toContain('settings.pipeline.issue.source_locked');
    expect(container.querySelector('[data-testid="pyannote-credential-form"]')).toBeNull();
  });

  it('is editable when not locked', () => {
    const { container } = render(Host, { props: { pipeline: fullLocalView() } });
    expect(select(container).disabled).toBe(false);
  });
});

describe('before the pipeline view has loaded', () => {
  it('still renders the four sources and no notice', () => {
    const { container } = render(Host, { props: { pipeline: null } });
    expect(optionValues(container)).toEqual(['provider', 'local', 'pyannote', 'asr_config', 'off']);
    expect(notices(container)).toHaveLength(0);
  });
});

const SAVED = [
  { ...CHOICES[0], transcribing: false },
  { ...CHOICES[1], transcribing: false },
  {
    uuid: 'ng',
    name: 'OpenAI plain',
    provider: 'openai',
    providerLabel: 'OpenAI',
    modelName: 'gpt-4o-transcribe',
    owner: null,
    returnsSpeakers: false,
    timing: 'none' as const,
    transcribing: false,
  },
  {
    uuid: 'now',
    name: 'Gladia now',
    provider: 'gladia',
    providerLabel: 'Gladia',
    modelName: 'solaria-1',
    owner: null,
    returnsSpeakers: true,
    timing: 'word' as const,
    transcribing: true,
  },
];

describe('which saved providers qualify, and what each returns (#1243)', () => {
  const saved = (c: HTMLElement) =>
    Array.from(c.querySelectorAll<HTMLElement>('[data-testid="asr-config-saved-row"]'));

  it('lists every saved provider with its speaker and timing flags and marks who qualifies', () => {
    const { container } = render(Host, {
      props: {
        pipeline: twoVendorView(),
        choices: CHOICES,
        saved: SAVED,
        diarizationSource: 'asr_config',
      },
    });
    const rows = saved(container);
    expect(rows.map((r) => r.dataset.qualifies)).toEqual(['true', 'true', 'false', 'false']);
    const flags = (row: HTMLElement) => ({
      speakers: row.querySelector('[data-testid="badge-speakers"]')?.getAttribute('data-value'),
      timing: row.querySelector('[data-testid="badge-timing"]')?.getAttribute('data-value'),
    });
    expect(flags(rows[0])).toEqual({ speakers: 'included', timing: 'word' });
    expect(flags(rows[1])).toEqual({ speakers: 'included', timing: 'segment' });
    expect(flags(rows[2])).toEqual({ speakers: 'text-only', timing: 'none' });
    // The provider that transcribes is Automatic's job, so it is named but not offered.
    expect(text(rows[3])).toContain('settings.speakerIdentification.source.asrConfigTranscribing');
    const picker = container.querySelector('#diarization-asr-config') as HTMLSelectElement;
    expect(Array.from(picker.options).map((o) => o.value)).toEqual(['dg', 'oa']);
  });

  it('puts the flags in the native option text, which cannot hold pills', () => {
    const { container } = render(Host, {
      props: {
        pipeline: twoVendorView(),
        choices: CHOICES,
        saved: SAVED,
        diarizationSource: 'asr_config',
      },
    });
    const picker = container.querySelector('#diarization-asr-config') as HTMLSelectElement;
    const first = picker.options[0].textContent;
    expect(first).toContain('settings.asrProvider.speakersIncluded');
    expect(first).toContain('settings.asrProvider.timing.word');
  });

  it('shows the chosen provider as badges under the picker', () => {
    const { container } = render(Host, {
      props: {
        pipeline: twoVendorView(),
        choices: CHOICES,
        saved: SAVED,
        diarizationSource: 'asr_config',
      },
    });
    const flags = container.querySelector('[data-testid="asr-config-flags"]') as HTMLElement;
    const timing = flags.querySelector('[data-testid="badge-timing"]');
    expect(timing?.getAttribute('data-value')).toBe('word');
  });

  it('says nothing is billed twice when the chosen provider already transcribes', () => {
    // Transcription is Deepgram nova-3 and the chosen saved config is the same provider and model.
    const view = liteCloudView({ deployment_mode: 'full' });
    const { container } = render(Host, {
      props: {
        pipeline: view,
        choices: [CHOICES[0]],
        saved: SAVED,
        diarizationSource: 'asr_config',
      },
    });
    expect(codes(container)).toContain('diarizer_same_as_transcription');
    expect(codes(container)).not.toContain('diarizer_double_billing');
  });

  it('adds a no-extra-cost line for Automatic with a cloud model that returns its own speakers', () => {
    const cloud = render(Host, {
      props: { pipeline: liteCloudView(), diarizationSource: 'provider' },
    });
    expect(cloud.container.querySelector('[data-testid="detection-billing-note"]')).not.toBeNull();
    cloud.unmount();
    const local = render(Host, {
      props: { pipeline: fullLocalView(), diarizationSource: 'provider' },
    });
    expect(local.container.querySelector('[data-testid="detection-billing-note"]')).toBeNull();
    local.unmount();
    const other = render(Host, {
      props: { pipeline: liteCloudView(), diarizationSource: 'pyannote' },
    });
    expect(other.container.querySelector('[data-testid="detection-billing-note"]')).toBeNull();
  });
});
