import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, fireEvent, waitFor } from '@testing-library/svelte';

// The translator echoes the key and any variables so a test can see WHICH copy was chosen.
vi.mock('$stores/locale', () => {
  const t = (key: string, vars?: Record<string, unknown>) =>
    vars ? `${key}${JSON.stringify(vars)}` : key;
  return {
    t: { subscribe: (run: (v: typeof t) => void) => (run(t), () => {}) },
    locale: { subscribe: (run: (v: string) => void) => (run('en'), () => {}) },
  };
});

const api = vi.hoisted(() => ({ getPipelineView: vi.fn() }));
vi.mock('$lib/api/pipeline', () => api);

import PipelineOverviewTestHost from './PipelineOverviewTestHost.svelte';
import {
  fullLocalView,
  issue,
  liteCloudView,
  step,
  twoVendorView,
} from '$lib/settings/pipelineViewFixtures';

async function renderView(view: unknown, onNavigate = vi.fn()) {
  api.getPipelineView.mockResolvedValue(view);
  const result = render(PipelineOverviewTestHost, { props: { onNavigate } });
  await waitFor(() =>
    expect(result.container.querySelectorAll('[data-testid="pipeline-step"]').length).toBe(4)
  );
  return { ...result, onNavigate };
}

const cards = (c: HTMLElement) =>
  Array.from(c.querySelectorAll<HTMLElement>('[data-testid="pipeline-step"]'));
const card = (c: HTMLElement, name: string) =>
  c.querySelector<HTMLElement>(`[data-testid="pipeline-step"][data-step="${name}"]`)!;
const text = (el: Element | null) => el?.textContent?.replace(/\s+/g, ' ').trim() ?? '';

beforeEach(() => vi.clearAllMocks());

describe('PipelineOverview', () => {
  it('renders the four steps in processing order with their titles', async () => {
    const { container } = await renderView(fullLocalView());
    expect(cards(container).map((el) => el.dataset.step)).toEqual([
      'transcription',
      'diarization',
      'speaker_id',
      'llm',
    ]);
    expect(cards(container).map((el) => text(el.querySelector('.step-title')))).toEqual([
      'settings.pipeline.step.transcription',
      'settings.pipeline.step.diarization',
      'settings.pipeline.step.speakerId',
      'settings.pipeline.step.llm',
    ]);
  });

  it('puts a This server chip on local steps and names the vendor on cloud steps', async () => {
    const { container } = await renderView(twoVendorView());
    const where = (name: string) =>
      text(card(container, name).querySelector('[data-testid="pipeline-where"]'));
    expect(where('transcription')).toBe('settings.pipeline.where.cloud{"vendor":"OpenAI"}');
    expect(where('diarization')).toBe('settings.pipeline.where.cloud{"vendor":"Deepgram"}');
    expect(where('speaker_id')).toBe('settings.pipeline.where.server');
  });

  it('shows the speakers and timing badges on the transcription step (text only, segment timing)', async () => {
    const { container } = await renderView(twoVendorView());
    const badges = card(container, 'transcription');
    expect(badges.querySelector('[data-testid="badge-speakers"]')?.getAttribute('data-value')).toBe(
      'text-only'
    );
    expect(badges.querySelector('[data-testid="badge-timing"]')?.getAttribute('data-value')).toBe(
      'segment'
    );
    expect(text(badges.querySelector('[data-testid="badge-speakers"]'))).toBe(
      'settings.asrProvider.textOnly'
    );
  });

  it('shows Speakers included with word timing for a model that returns both', async () => {
    const { container } = await renderView(fullLocalView());
    const badges = card(container, 'transcription');
    expect(text(badges.querySelector('[data-testid="badge-speakers"]'))).toBe(
      'settings.asrProvider.speakersIncluded'
    );
    expect(badges.querySelector('[data-testid="badge-timing"]')?.getAttribute('data-value')).toBe(
      'word'
    );
  });

  it('shows the extra-billing badge on a speaker detector that is another transcription provider', async () => {
    const { container } = await renderView(twoVendorView());
    expect(
      card(container, 'diarization').querySelector('[data-testid="badge-adapter"]')
    ).not.toBeNull();
    expect(
      card(container, 'transcription').querySelector('[data-testid="badge-adapter"]')
    ).toBeNull();
  });

  it('names both vendors in the one privacy note when audio goes to two of them', async () => {
    const { container } = await renderView(twoVendorView());
    expect(text(container.querySelector('[data-testid="pipeline-privacy"]'))).toBe(
      'settings.pipeline.audioLeaves{"vendor":"OpenAI and Deepgram"}'
    );
  });

  it('shows no privacy note when nothing leaves this server', async () => {
    const { container } = await renderView(fullLocalView());
    expect(container.querySelector('[data-testid="pipeline-privacy"]')).toBeNull();
  });

  it('dims a step that does not run and labels it Off', async () => {
    const { container } = await renderView(liteCloudView());
    const llm = card(container, 'llm');
    expect(llm.classList.contains('off')).toBe(true);
    expect(text(llm.querySelector('[data-testid="pipeline-where"]'))).toBe('settings.pipeline.off');
    expect(card(container, 'transcription').classList.contains('off')).toBe(false);
  });

  it('says whether the speaker range is used', async () => {
    const { container } = await renderView(fullLocalView());
    expect(
      text(card(container, 'diarization').querySelector('[data-testid="pipeline-hints"]'))
    ).toBe('settings.pipeline.hintsIgnored');
  });

  it('renders an issue from its code, never from the server text, and links to the tab that fixes it', async () => {
    const view = fullLocalView({
      issues: [issue('asr_no_timing_for_speakers', 'error', 'diarization')],
    });
    const { container, onNavigate } = await renderView(view);
    const row = card(container, 'diarization').querySelector<HTMLElement>(
      '[data-testid="pipeline-issue"]'
    )!;
    expect(row.dataset.severity).toBe('error');
    expect(text(row)).toContain('settings.pipeline.issue.asr_no_timing_for_speakers');
    expect(text(row)).not.toContain('fixed sentence for');
    const links = Array.from(
      row.querySelectorAll<HTMLElement>('[data-testid="pipeline-fix-link"]')
    );
    expect(links.map((l) => l.dataset.section)).toEqual(['asr-provider', 'speaker-identification']);
    await fireEvent.click(links[0]);
    expect(onNavigate).toHaveBeenCalledWith('asr-provider');
  });

  it('falls back to the fixed server sentence only for a code it has no copy for', async () => {
    const view = fullLocalView({ issues: [issue('brand_new_code', 'warning', 'transcription')] });
    const { container } = await renderView(view);
    expect(
      text(card(container, 'transcription').querySelector('[data-testid="pipeline-issue"]'))
    ).toContain('fixed sentence for brand_new_code');
  });

  it('every step has a Change link to the one place that edits it', async () => {
    const { container, onNavigate } = await renderView(fullLocalView());
    const links = Array.from(
      container.querySelectorAll<HTMLElement>('[data-testid="pipeline-change"]')
    );
    expect(links.map((l) => l.dataset.section)).toEqual([
      'asr-provider',
      'speaker-identification',
      'speaker-identification',
      'llm-provider',
    ]);
    await fireEvent.click(links[3]);
    expect(onNavigate).toHaveBeenCalledWith('llm-provider');
  });

  it('shows the lite banner on lite and a stored local selection as a blocking issue with its fix', async () => {
    const view = liteCloudView({
      issues: [issue('local_diarization_unavailable', 'error', 'diarization')],
    });
    view.steps[1] = step({ step: 'diarization', enabled: false, where: null, engine: 'off' });
    const { container } = await renderView(view);
    expect(container.querySelector('[data-testid="pipeline-lite-banner"]')).not.toBeNull();
    const row = card(container, 'diarization').querySelector<HTMLElement>(
      '[data-testid="pipeline-issue"]'
    )!;
    expect(row.dataset.code).toBe('local_diarization_unavailable');
    expect(
      row.querySelector<HTMLElement>('[data-testid="pipeline-fix-link"]')?.dataset.section
    ).toBe('speaker-identification');
  });

  it('does not show the lite banner on a full deployment', async () => {
    const { container } = await renderView(fullLocalView());
    expect(container.querySelector('[data-testid="pipeline-lite-banner"]')).toBeNull();
  });

  it('offers no fix link for a setting the administrator owns', async () => {
    const view = fullLocalView({
      issues: [issue('source_locked', 'info', 'diarization')],
      diarization_source_locked: true,
    });
    const { container } = await renderView(view);
    const row = card(container, 'diarization').querySelector('[data-testid="pipeline-issue"]')!;
    expect(row.querySelector('[data-testid="pipeline-fix-link"]')).toBeNull();
  });

  it('says it could not load, and loads again on request', async () => {
    api.getPipelineView.mockRejectedValueOnce(new Error('boom'));
    const { container } = render(PipelineOverviewTestHost, { props: {} });
    await waitFor(() => expect(text(container)).toContain('settings.pipeline.loadFailed'));
    api.getPipelineView.mockResolvedValue(fullLocalView());
    await fireEvent.click(container.querySelector('button.btn') as HTMLButtonElement);
    await waitFor(() => expect(cards(container)).toHaveLength(4));
    expect(api.getPipelineView).toHaveBeenCalledTimes(2);
  });
});
