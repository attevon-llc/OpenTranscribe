import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/svelte';

vi.mock('$lib/api/speakerHistory', () => ({ getSpeakerIdentityHistory: vi.fn() }));

// Real English copy with interpolation, so the rendered sentences are the shipped ones.
vi.mock('$stores/locale', async () => {
  const { readable } = await import('svelte/store');
  const en = (await import('$lib/i18n/locales/en.json')).default as Record<string, string>;
  const t = (key: string, vars?: Record<string, unknown>) =>
    Object.entries(vars ?? {}).reduce(
      (text, [name, value]) => text.replace(`{{${name}}}`, String(value)),
      en[key] ?? key
    );
  return { t: readable(t), locale: readable('en') };
});

import { getSpeakerIdentityHistory } from '$lib/api/speakerHistory';
import SpeakerIdentityHistory from './SpeakerIdentityHistory.svelte';

const load = vi.mocked(getSpeakerIdentityHistory);

beforeEach(() => {
  vi.clearAllMocks();
});

describe('SpeakerIdentityHistory', () => {
  it('loads nothing until opened, then lists the changes with who made them', async () => {
    load.mockResolvedValue([
      {
        uuid: 'e-2',
        kind: 'rename',
        at: '2026-11-07T10:00:00Z',
        actor: { kind: 'user', user: { uuid: 'u-1', name: 'Dana' } },
        before: { withheld: ['name'] },
        after: { name: 'Alicia' },
      },
      {
        uuid: 'e-1',
        kind: 'rename',
        at: '2026-11-06T10:00:00Z',
        actor: { kind: 'system', user: null },
        model: 'wespeaker-r34',
        confidence: 0.91,
        before: { name: null },
        after: { name: 'Alice' },
      },
    ]);
    render(SpeakerIdentityHistory, { props: { speakerUuid: 's-1' } });
    expect(load).not.toHaveBeenCalled();

    await fireEvent.click(screen.getByRole('button'));
    expect(load).toHaveBeenCalledWith('s-1');
    expect(await screen.findByText('Dana')).toBeTruthy();
    expect(screen.getByText(/to .Alicia/)).toBeTruthy();
    expect(screen.getByText('Model: wespeaker-r34')).toBeTruthy();
    expect(screen.getByText('91% confidence')).toBeTruthy();
    expect(screen.getByText('Automatic')).toBeTruthy();
    // The old name was a voiceprint's: the person's entry says so instead of repeating it.
    expect(screen.getByText(/\(from a voiceprint\)/)).toBeTruthy();
    expect(screen.getAllByRole('listitem')).toHaveLength(2);
  });

  it('says so when the history cannot be loaded', async () => {
    load.mockRejectedValue(new Error('boom'));
    render(SpeakerIdentityHistory, { props: { speakerUuid: 's-1' } });
    await fireEvent.click(screen.getByRole('button'));
    expect(await screen.findByText('Could not load the history.')).toBeTruthy();
    expect(screen.queryAllByRole('listitem')).toHaveLength(0);
  });
});
