/**
 * The file page's "Processing" section (#1229): collapsed by default like "File details",
 * expands to the step rows, and is absent for a payload without them.
 */
import { describe, it, expect, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/svelte';
import type { ProcessingStep } from '$lib/types/media';

vi.mock('$stores/locale', async () => {
  const { readable } = await import('svelte/store');
  const en = (await import('$lib/i18n/locales/en.json')).default as Record<string, string>;
  return { t: readable((key: string) => en[key] ?? key) };
});

import ProcessingSection from './ProcessingSection.svelte';

const STEPS: ProcessingStep[] = [
  {
    step: 'transcription',
    status: 'done',
    recorded: 'full',
    provider: 'local',
    provider_display: 'Whisper',
    model: 'large-v3-turbo',
    where: 'server',
    timing: 'word',
  },
];

describe('ProcessingSection', () => {
  it('starts collapsed and expands to the rows', async () => {
    const { container } = render(ProcessingSection, { steps: STEPS });
    const header = screen.getByRole('button', { name: 'Processing' });
    expect(header.getAttribute('aria-expanded')).toBe('false');
    expect(container.querySelector('.step-row')).toBeNull();

    await fireEvent.click(header);

    expect(header.getAttribute('aria-expanded')).toBe('true');
    const row = container.querySelector('[data-step="transcription"]');
    expect(row?.textContent).toContain('large-v3-turbo');
    expect(row?.textContent).toContain('This server');
  });

  it('renders nothing for a file whose payload has no rows', () => {
    const { container } = render(ProcessingSection, { steps: [] });
    expect(container.querySelector('button')).toBeNull();
  });
});
