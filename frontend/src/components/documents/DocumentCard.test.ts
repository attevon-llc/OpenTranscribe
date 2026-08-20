/**
 * DocumentCard — the retry affordance (#362 lane C3).
 *
 * A failed document parse was previously a dead end in the gallery: no button led
 * anywhere. These tests pin that an error-status card renders a retry control, that
 * clicking it dispatches `retry` with the document's uuid rather than navigating (the
 * whole card is an `<a>`), and that a non-error card never shows the control at all.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/svelte';

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string, opts?: Record<string, unknown>) => string) => void) => {
      run((key: string, opts?: Record<string, unknown>) =>
        opts ? `${key}:${JSON.stringify(opts)}` : key
      );
      return () => {};
    },
  },
}));

import DocumentCard from './DocumentCard.svelte';

const BASE_DOC = {
  uuid: 'doc-uuid-1',
  filename: 'report.pdf',
  file_size: 1024,
  content_type: 'application/pdf',
  status: 'completed',
  display_status: 'Ready',
  error_category: null,
  last_error_message: null,
  parser: null,
  page_count: null,
  word_count: 0,
  chunk_count: 0,
  language: null,
  has_embedded_text: null,
  ocr_applied: false,
  parse_warnings: [],
  created_at: '2026-08-19T00:00:00Z',
  updated_at: '2026-08-19T00:00:00Z',
  parsed_at: null,
  is_quarantined: false,
  legal_hold: false,
  my_permission: null,
};

describe('DocumentCard — retry (#362 lane C3)', () => {
  it('shows a retry control for an errored document', () => {
    render(DocumentCard, {
      props: {
        doc: { ...BASE_DOC, status: 'error', last_error_message: 'bad zip' },
      },
    });

    expect(screen.getByText('gallery.retry')).toBeInTheDocument();
  });

  it('shows no retry control for a completed document', () => {
    render(DocumentCard, { props: { doc: BASE_DOC } });

    expect(screen.queryByText('gallery.retry')).toBeNull();
  });

  it('dispatches retry with the document uuid, without navigating the parent link', async () => {
    const retryHandler = vi.fn();
    render(DocumentCard, {
      props: {
        doc: { ...BASE_DOC, status: 'error', last_error_message: 'bad zip' },
      },
      events: { retry: retryHandler },
    });

    const button = screen.getByText('gallery.retry');
    const clickEvent = await fireEvent.click(button);

    expect(retryHandler).toHaveBeenCalledTimes(1);
    expect(retryHandler.mock.calls[0][0].detail).toEqual({ uuid: 'doc-uuid-1' });
    // jsdom's fireEvent reports whether preventDefault() was called — the card is an
    // <a href="/documents/...">, so a retry click must not also trigger navigation.
    expect(clickEvent).toBe(false);
  });
});

describe('DocumentCard — bulk select (v400, #362 lane C3-remainder)', () => {
  it('shows no checkbox outside selection mode', () => {
    render(DocumentCard, { props: { doc: BASE_DOC } });

    expect(screen.queryByRole('checkbox')).toBeNull();
  });

  it('shows an unchecked checkbox in selection mode when not selected', () => {
    render(DocumentCard, { props: { doc: BASE_DOC, selectionMode: true, selected: false } });

    const checkbox = screen.getByRole('checkbox') as HTMLInputElement;
    expect(checkbox.checked).toBe(false);
  });

  it('shows a checked checkbox when selected', () => {
    render(DocumentCard, { props: { doc: BASE_DOC, selectionMode: true, selected: true } });

    const checkbox = screen.getByRole('checkbox') as HTMLInputElement;
    expect(checkbox.checked).toBe(true);
  });

  it('dispatches toggleSelect with the uuid on checkbox click, without navigating', async () => {
    const toggleHandler = vi.fn();
    render(DocumentCard, {
      props: { doc: BASE_DOC, selectionMode: true, selected: false },
      events: { toggleSelect: toggleHandler },
    });

    const checkbox = screen.getByRole('checkbox');
    const clickEvent = await fireEvent.click(checkbox);

    expect(toggleHandler).toHaveBeenCalledTimes(1);
    expect(toggleHandler.mock.calls[0][0].detail).toEqual({ uuid: 'doc-uuid-1' });
    expect(clickEvent).toBe(true); // checkbox click is stopped, not prevented
  });

  it('dispatches toggleSelect on a card click in selection mode, preventing navigation', async () => {
    const toggleHandler = vi.fn();
    render(DocumentCard, {
      props: { doc: BASE_DOC, selectionMode: true, selected: false },
      events: { toggleSelect: toggleHandler },
    });

    const card = screen.getByText('report.pdf').closest('a') as HTMLAnchorElement;
    const clickEvent = await fireEvent.click(card);

    expect(toggleHandler).toHaveBeenCalledTimes(1);
    expect(clickEvent).toBe(false);
  });
});
