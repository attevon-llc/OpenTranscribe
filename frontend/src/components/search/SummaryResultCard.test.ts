/**
 * `SummaryResultCard.svelte` — a summary search hit (issue #462): file title/link,
 * a per-leaf match list (each row addressable by `key_path`), and a "view summary"
 * fallback for a hit whose document-level match produced no single-leaf match
 * (`search_summaries` keeps such a file in the page with `matches: []` rather than
 * dropping it — see `summary_search.py`'s docstring).
 *
 * DEFECT THIS CATCHES: `formatKeyPath` is the only thing turning a raw backend path
 * like `major_topics[0].key_points[2]` into a readable label — a regex slip there
 * silently prints the raw path (or throws) with no type error, since `key_path` is a
 * plain string all the way through.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, fireEvent } from '@testing-library/svelte';

vi.mock('$stores/locale', async () => {
  const { readable } = await import('svelte/store');
  const en = (await import('$lib/i18n/locales/en.json')).default as Record<string, string>;
  return {
    t: readable((key: string, vars?: Record<string, unknown>) => {
      const template = en[key] ?? key;
      if (!vars) return template;
      return Object.entries(vars).reduce(
        (acc, [k, v]) => acc.replace(new RegExp(`{{${k}}}`, 'g'), String(v)),
        template
      );
    }),
  };
});

import SummaryResultCard from './SummaryResultCard.svelte';
import type { SummaryHit } from '$stores/search';

const baseHit: SummaryHit = {
  file_uuid: 'uuid-1',
  file_id: 42,
  title: 'Q3 Planning Meeting',
  matches: [
    { key_path: 'major_topics[0].key_points[2]', snippet: 'Cut travel spend by 20%.' },
    { key_path: 'bluf', snippet: 'Ship the redesign by Friday.' },
  ],
};

describe('SummaryResultCard', () => {
  it('renders the file title as a link to the file detail page', () => {
    const { getByRole } = render(SummaryResultCard, { props: { hit: baseHit } });
    const link = getByRole('link', { name: /Q3 Planning Meeting/ });
    expect(link).toHaveAttribute('href', '/files/uuid-1');
  });

  it('formats a nested array key_path into a readable label', () => {
    const { getByText } = render(SummaryResultCard, { props: { hit: baseHit } });
    expect(getByText('Major Topics #1 › Key Points #3')).toBeInTheDocument();
  });

  it('formats a bare top-level key_path into a readable label', () => {
    const { getByText } = render(SummaryResultCard, { props: { hit: baseHit } });
    expect(getByText('Bluf')).toBeInTheDocument();
  });

  it('renders every match snippet as plain text', () => {
    const { getByText } = render(SummaryResultCard, { props: { hit: baseHit } });
    expect(getByText('Cut travel spend by 20%.')).toBeInTheDocument();
    expect(getByText('Ship the redesign by Friday.')).toBeInTheDocument();
  });

  it('dispatches openMatch with the file id and key_path when a match row is clicked', async () => {
    const openMatch = vi.fn();
    const { getByText } = render(SummaryResultCard, {
      props: { hit: baseHit },
      events: { openMatch },
    });

    await fireEvent.click(getByText('Cut travel spend by 20%.').closest('button')!);

    expect(openMatch).toHaveBeenCalledTimes(1);
    expect(openMatch.mock.calls[0][0].detail).toEqual({
      fileUuid: 'uuid-1',
      title: 'Q3 Planning Meeting',
      keyPath: 'major_topics[0].key_points[2]',
    });
  });

  it('dispatches openMatch with a null key_path when "view summary" is clicked', async () => {
    const openMatch = vi.fn();
    const { getByRole } = render(SummaryResultCard, {
      props: { hit: baseHit },
      events: { openMatch },
    });

    await fireEvent.click(getByRole('button', { name: 'View summary' }));

    expect(openMatch).toHaveBeenCalledTimes(1);
    expect(openMatch.mock.calls[0][0].detail.keyPath).toBeNull();
  });

  it('renders no match list, only the view-summary fallback, when matches is empty', () => {
    const emptyHit: SummaryHit = { ...baseHit, matches: [] };
    const { queryByText, getByRole } = render(SummaryResultCard, { props: { hit: emptyHit } });

    expect(queryByText('Bluf')).not.toBeInTheDocument();
    expect(getByRole('button', { name: 'View summary' })).toBeInTheDocument();
  });
});
