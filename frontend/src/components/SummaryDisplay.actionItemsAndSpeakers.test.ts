/**
 * W2.5 Task 0 — SummaryDisplay's BLUF mode renders `action_items` and
 * `speakers_analysis`, which it did not before this change (and
 * `action_items` had ZERO renderers anywhere in the app). Shape-tolerant:
 * the DEFAULT summary prompt emits `{item, owner, ...}` for action items and
 * `{speaker, role, talk_time_percentage, key_contributions}` for speaker
 * analysis; the dead `schemas/summary.py` `ActionItem`/legacy `SpeakerInfo`
 * shapes (`text`/`assigned_to`, `name`/`percentage`/`key_points`) must also
 * render without crashing, since `SummaryData` is `extra="allow"`.
 */
import { describe, expect, it } from 'vitest';
import { render } from '@testing-library/svelte';

import SummaryDisplay from './SummaryDisplay.svelte';
import type { SummaryData } from '$lib/types/summary';

function summary(overrides: Partial<SummaryData> = {}): SummaryData {
  return {
    bluf: 'The team agreed to ship next week.',
    brief_summary: 'A longer paragraph describing the meeting in full.',
    ...overrides,
  };
}

describe('SummaryDisplay — action items (default prompt shape)', () => {
  it('renders item/owner/due_date/priority from the DEFAULT prompt shape', () => {
    const { getByText, container } = render(SummaryDisplay, {
      props: {
        summary: summary({
          action_items: [
            {
              item: 'Update the roadmap',
              owner: 'Alice',
              due_date: 'Friday',
              priority: 'high',
              context: 'Roadmap is stale',
              mentioned_timestamp: '[01:02]',
            },
          ],
        }),
      },
    });

    expect(getByText('Update the roadmap')).toBeTruthy();
    expect(getByText(/Alice/)).toBeTruthy();
    expect(getByText(/Friday/)).toBeTruthy();
    expect(container.querySelector('.action-item-priority.priority-high')).toBeTruthy();
  });

  it('also renders the dead schema shape (text/assigned_to)', () => {
    const { getByText } = render(SummaryDisplay, {
      props: {
        summary: summary({
          action_items: [
            {
              text: 'Send the pricing doc',
              assigned_to: 'Bob',
              due_date: null,
              priority: 'low',
              context: '',
            },
          ],
        }),
      },
    });

    expect(getByText('Send the pricing doc')).toBeTruthy();
    expect(getByText(/Bob/)).toBeTruthy();
  });

  it('renders nothing for action items when there are none', () => {
    const { container } = render(SummaryDisplay, {
      props: { summary: summary({ action_items: [] }) },
    });

    expect(container.querySelector('.action-items-section')).toBeNull();
  });
});

describe('SummaryDisplay — speaker analysis (default prompt shape)', () => {
  it('renders speaker/role/talk_time_percentage/key_contributions', () => {
    const { getByText, container } = render(SummaryDisplay, {
      props: {
        summary: summary({
          speakers_analysis: [
            {
              speaker: 'Dana Whitfield',
              role: 'Presenter',
              talk_time_percentage: 42,
              key_contributions: ['Owns the Q3 roadmap', 'Raised the budget concern'],
            },
          ],
        }),
      },
    });

    expect(getByText('Dana Whitfield')).toBeTruthy();
    expect(getByText('Presenter')).toBeTruthy();
    expect(getByText(/42/)).toBeTruthy();
    expect(getByText('Owns the Q3 roadmap')).toBeTruthy();
    expect(container.querySelectorAll('.speaker-points li')).toHaveLength(2);
  });

  it('also renders the legacy SpeakerInfo shape (name/percentage/key_points)', () => {
    const { getByText } = render(SummaryDisplay, {
      props: {
        summary: summary({
          speakers: [
            {
              name: 'Casey Lee',
              talk_time_seconds: 120,
              percentage: 30,
              key_points: ['Ran the standup'],
            },
          ],
        }),
      },
    });

    expect(getByText('Casey Lee')).toBeTruthy();
    expect(getByText('Ran the standup')).toBeTruthy();
  });

  it('renders nothing for speaker analysis when there is none', () => {
    const { container } = render(SummaryDisplay, {
      props: { summary: summary() },
    });

    expect(container.querySelector('.speaker-analysis-section')).toBeNull();
  });
});
