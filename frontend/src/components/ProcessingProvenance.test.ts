/**
 * How a file was processed (#1229). The component renders the backend's display-ready rows
 * and derives nothing: these tests pin that each status reads as what it is (a recorded
 * cloud step is not shown as "This server", an old file says "Not recorded" rather than
 * inventing an engine), with the real English copy so a missing key shows up as a key.
 */
import { describe, it, expect, vi } from 'vitest';
import { render } from '@testing-library/svelte';
import type { ProcessingStep } from '$lib/types/media';

vi.mock('$stores/locale', async () => {
  const { readable } = await import('svelte/store');
  const en = (await import('$lib/i18n/locales/en.json')).default as Record<string, string>;
  return { t: readable((key: string) => en[key] ?? key) };
});

import ProcessingProvenance from './ProcessingProvenance.svelte';

function step(overrides: Partial<ProcessingStep> & Pick<ProcessingStep, 'step'>): ProcessingStep {
  return {
    status: 'done',
    recorded: 'full',
    provider: null,
    provider_display: null,
    model: null,
    where: null,
    timing: null,
    ...overrides,
  };
}

const RUN: ProcessingStep[] = [
  step({
    step: 'transcription',
    provider: 'openai',
    provider_display: 'OpenAI',
    model: 'whisper-1',
    where: 'cloud',
    timing: 'segment',
  }),
  step({
    step: 'diarization',
    provider: 'native',
    provider_display: 'diar-native',
    model: 'community-1',
    where: 'server',
  }),
  step({ step: 'voiceprints', status: 'off' }),
  step({ step: 'summary', status: 'not_run' }),
  step({ step: 'topics', status: 'not_recorded', recorded: 'none' }),
  step({ step: 'speaker_names', provider_display: 'PyAnnote', recorded: 'legacy' }),
];

function rowOf(container: HTMLElement, name: ProcessingStep['step']): HTMLElement {
  const row = container.querySelector<HTMLElement>(`[data-step="${name}"]`);
  expect(row, name).not.toBeNull();
  return row as HTMLElement;
}

describe('ProcessingProvenance', () => {
  it('lists every step in the order the backend sent, with its translated name', () => {
    const { container } = render(ProcessingProvenance, { steps: RUN });
    const names = Array.from(container.querySelectorAll('.step-name')).map((el) => el.textContent);
    expect(names).toEqual([
      'Transcription',
      'Speaker detection',
      'Voiceprints',
      'Summary',
      'Topics',
      'Speaker name suggestions',
    ]);
    expect(container.querySelector('ul')?.getAttribute('aria-label')).toBe(
      'How this file was processed'
    );
  });

  it('names the provider, the model and where a completed step ran', () => {
    const { container } = render(ProcessingProvenance, { steps: RUN });
    const transcription = rowOf(container, 'transcription');
    expect(transcription.textContent).toContain('OpenAI');
    expect(transcription.textContent).toContain('whisper-1');
    expect(transcription.textContent).toContain('Cloud');
    expect(transcription.textContent).not.toContain('This server');
    expect(transcription.textContent).toContain('Segment timing');

    const diarization = rowOf(container, 'diarization');
    expect(diarization.textContent).toContain('This server');
    expect(diarization.textContent).not.toContain('Cloud');
  });

  it('isolates provider and model text for right-to-left layouts', () => {
    const { container } = render(ProcessingProvenance, { steps: RUN });
    const isolated = Array.from(rowOf(container, 'transcription').querySelectorAll('bdi')).map(
      (el) => el.textContent
    );
    expect(isolated).toEqual(['OpenAI', 'whisper-1']);
  });

  it('says off, not run and not recorded instead of inventing an engine', () => {
    const { container } = render(ProcessingProvenance, { steps: RUN });
    expect(rowOf(container, 'voiceprints').textContent).toContain('Off');
    expect(rowOf(container, 'summary').textContent).toContain('Not run');
    const topics = rowOf(container, 'topics');
    expect(topics.textContent).toContain('Not recorded (processed before this was tracked)');
    expect(topics.querySelector('bdi')).toBeNull();
  });

  it('says the location was not recorded when the backend could not tell', () => {
    const { container } = render(ProcessingProvenance, { steps: RUN });
    const names = rowOf(container, 'speaker_names');
    expect(names.textContent).toContain('PyAnnote');
    expect(names.textContent).toContain('Location not recorded');
    expect(names.textContent).not.toContain('This server');
  });

  it('renders nothing without rows', () => {
    const { container } = render(ProcessingProvenance, { steps: [] });
    expect(container.querySelector('ul')).toBeNull();
  });
});
