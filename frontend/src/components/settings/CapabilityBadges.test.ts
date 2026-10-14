import { describe, it, expect, vi } from 'vitest';
import { render } from '@testing-library/svelte';

vi.mock('$stores/locale', () => {
  const t = (key: string, vars?: Record<string, unknown>) =>
    vars ? `${key}${JSON.stringify(vars)}` : key;
  return { t: { subscribe: (run: (v: typeof t) => void) => (run(t), () => {}) } };
});

import CapabilityBadges from './CapabilityBadges.svelte';

const text = (el: Element) => el.textContent?.replace(/\s+/g, ' ').trim() ?? '';

describe('CapabilityBadges (#1243)', () => {
  it('draws the speakers and timing pills from the flags', () => {
    const { container } = render(CapabilityBadges, {
      props: { returnsSpeakers: false, timing: 'segment' },
    });
    expect(
      container.querySelector('[data-testid="badge-speakers"]')?.getAttribute('data-value')
    ).toBe('text-only');
    expect(text(container)).toContain('settings.asrProvider.timing.segment');
    expect(container.querySelector('[data-testid="capability-notes"]')).toBeNull();
  });

  it('explains the flags only when asked', () => {
    const { container } = render(CapabilityBadges, {
      props: { returnsSpeakers: true, timing: 'word', explain: true, local: true },
    });
    expect(text(container)).toContain('settings.asrProvider.implication.speakersLocal');
  });

  it('marks the no-timing note as blocking', () => {
    const { container } = render(CapabilityBadges, {
      props: { returnsSpeakers: false, timing: 'none', explain: true },
    });
    const note = container.querySelector('[data-testid="capability-note"]') as HTMLElement;
    expect(note.classList.contains('blocking')).toBe(true);
    expect(note.dataset.key).toBe('settings.asrProvider.timing.noneInfo');
  });

  it('renders the key to every pill, once each, in the legend', () => {
    const { container } = render(CapabilityBadges, { props: { legend: true } });
    const rows = Array.from(container.querySelectorAll('.legend-row')).map(text);
    expect(rows).toHaveLength(5);
    expect(rows.join(' ')).toContain('settings.asrProvider.legend.timingNone');
    expect(container.querySelector('[data-testid="badge-speakers"]')).toBeNull();
  });
});
