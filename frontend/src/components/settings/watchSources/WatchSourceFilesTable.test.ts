/**
 * `WatchSourceFilesTable` renders the diagnostic detail an operator opens this table
 * for, and — just as importantly — refuses to offer actions the API would reject.
 *
 * The i18n store is mocked as an identity translator, so assertions match on the key
 * rather than English prose.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/svelte';

/**
 * Mimics i18next rather than being a plain identity function: a real `t` returns the
 * KEY when there is no translation, which is exactly the signal the component's
 * unknown-status fallback keys off. A pure identity translator makes every lookup
 * look missing, so the fallback fires for everything and neither branch is testable.
 */
const TRANSLATED = new Set([
  'settings.watchSources.files.status.error',
  'settings.watchSources.files.status.imported',
  'settings.watchSources.files.status.waiting_for_parts',
  'settings.watchSources.files.status.skipped_old',
  'settings.watchSources.files.status.skipped_duplicate',
  'settings.watchSources.files.status.skipped_invalid',
  'settings.watchSources.files.status.importing',
  'settings.watchSources.files.status.downloading',
  'settings.watchSources.files.status.stitched_part',
  'settings.watchSources.files.skipReason.too_old',
]);

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string, vars?: Record<string, unknown>) => string) => void) => {
      run((key: string) => (TRANSLATED.has(key) ? `tr:${key}` : key));
      return () => {};
    },
  },
}));

import WatchSourceFilesTable from './WatchSourceFilesTable.svelte';
import type { WatchSourceFile } from '$lib/api/watchSourcesApi';

function file(overrides: Partial<WatchSourceFile> = {}): WatchSourceFile {
  return {
    uuid: 'f1',
    remote_path: '/watch/board.mp4',
    filename: 'board.mp4',
    status: 'error',
    retry_count: 0,
    error_message: null,
    skip_reason: null,
    ...overrides,
  } as WatchSourceFile;
}

function renderTable(
  props: Record<string, unknown> = {},
  events: Record<string, (e: CustomEvent) => void> = {}
) {
  return render(WatchSourceFilesTable, {
    props: { files: [file()], busyUuids: new Set(), selectedUuids: new Set(), ...props },
    events,
  } as never);
}

describe('diagnostic detail', () => {
  it('shows the error message, which is the reason the table exists', () => {
    renderTable({ files: [file({ error_message: 'download produced no bytes' })] });
    expect(screen.getByText('download produced no bytes')).toBeInTheDocument();
  });

  it('renders a skip reason through its own translated label', () => {
    renderTable({ files: [file({ status: 'skipped_old', skip_reason: 'too_old' })] });
    expect(
      screen.getByText('tr:settings.watchSources.files.skipReason.too_old')
    ).toBeInTheDocument();
  });

  it('renders a known status through its translated label, not the raw value', () => {
    // The other half of the fallback: a status the enum DOES know must never leak its
    // wire value into the UI.
    renderTable({ files: [file({ status: 'imported', media_file_uuid: null })] });
    expect(screen.getByText('tr:settings.watchSources.files.status.imported')).toBeInTheDocument();
    expect(screen.queryByText('imported')).not.toBeInTheDocument();
  });

  it('falls back to the raw status when the enum does not know it', () => {
    // Deployments carry statuses this UI does not enumerate — `skipped_too_large` is
    // written by the document ingest path and is not an enum member yet (#547). A
    // blank cell there would read as missing data rather than an unfamiliar state.
    renderTable({ files: [file({ status: 'skipped_too_large' })] });
    expect(screen.getByText('skipped_too_large')).toBeInTheDocument();
  });

  it('labels the count as attempts on an ordinary row', () => {
    renderTable({ files: [file({ status: 'error', retry_count: 3 })] });
    expect(screen.getByText('settings.watchSources.files.attempts')).toBeInTheDocument();
  });

  it('labels the SAME count as scans-waited on a multipart row', () => {
    // `retry_count` is the wait-scan counter for `waiting_for_parts`, not an attempt
    // count. One heading for both would misreport one of them.
    renderTable({ files: [file({ status: 'waiting_for_parts', retry_count: 3 })] });
    expect(screen.getByText('settings.watchSources.files.scansWaited')).toBeInTheDocument();
    expect(screen.queryByText('settings.watchSources.files.attempts')).not.toBeInTheDocument();
  });

  it('links an imported row to the library file it produced', () => {
    renderTable({
      files: [file({ status: 'imported', media_file_uuid: 'media-1' })],
    });
    expect(screen.getByRole('link', { name: 'board.mp4' })).toHaveAttribute(
      'href',
      '/files/media-1'
    );
  });

  it('hints that an age-skipped row will just be skipped again', () => {
    renderTable({
      files: [file({ status: 'skipped_old', skip_reason: 'too_old' })],
      hasAgeLimit: true,
    });
    expect(screen.getByText('settings.watchSources.files.ageLimitHint')).toBeInTheDocument();
  });

  it('omits that hint when the source has no age limit', () => {
    // The negative control: an unconditional hint would be noise on every skipped row
    // and would tell the operator to clear a limit that does not exist.
    renderTable({
      files: [file({ status: 'skipped_old', skip_reason: 'too_old' })],
      hasAgeLimit: false,
    });
    expect(screen.queryByText('settings.watchSources.files.ageLimitHint')).not.toBeInTheDocument();
  });
});

describe('retry is offered only where the API accepts it', () => {
  it.each(['error', 'skipped_duplicate', 'skipped_old', 'skipped_invalid'])(
    'offers retry for %s',
    (status) => {
      renderTable({ files: [file({ status })] });
      expect(screen.getByText('settings.watchSources.files.retry')).toBeInTheDocument();
    }
  );

  it.each(['imported', 'importing', 'downloading', 'waiting_for_parts', 'stitched_part'])(
    'hides retry for %s, which the backend refuses',
    (status) => {
      // Each refusal prevents a distinct harm — duplicating an imported file, racing an
      // in-flight claim, corrupting a multipart wait counter, re-importing a part
      // already folded into a stitched recording. Offering the button anyway would
      // just produce an error the user cannot act on.
      renderTable({ files: [file({ status })] });
      expect(screen.queryByText('settings.watchSources.files.retry')).not.toBeInTheDocument();
      expect(screen.getByText('settings.watchSources.files.deleteRecord')).toBeInTheDocument();
    }
  );
});

describe('events', () => {
  it('dispatches retry with the row, so the parent can batch it', async () => {
    const onRetry = vi.fn();
    renderTable({}, { retry: onRetry });

    await fireEvent.click(screen.getByText('settings.watchSources.files.retry'));

    expect(onRetry).toHaveBeenCalledTimes(1);
    expect(onRetry.mock.calls[0][0].detail.uuid).toBe('f1');
  });

  it('disables both actions while that row is in flight', () => {
    renderTable({ busyUuids: new Set(['f1']) });
    expect(screen.getByText('settings.watchSources.files.deleteRecord')).toBeDisabled();
  });

  it('dispatches select-all with the new checked state', async () => {
    const onToggleAll = vi.fn();
    renderTable({}, { toggleSelectAll: onToggleAll });

    await fireEvent.click(screen.getByLabelText('settings.watchSources.files.selectAll'));

    expect(onToggleAll.mock.calls[0][0].detail).toBe(true);
  });
});
