/**
 * PickerDocumentsTab — the document counterpart of PickerFilesTab (#362 lane C3).
 *
 * Without this tab, a document could not be added to a chat's explicit scope at all —
 * `file_uuids` in `FilePickerModal`'s draft was only ever populated from `GET /files`.
 * These tests pin the request shape (status=completed only — a pending/error document
 * has no chunks to retrieve from) and that toggling a row dispatches the SAME shared
 * `change` event shape `PickerFilesTab` uses, since both write into one array
 * (`ChatScope.file_uuids` — see this component's own docstring for why there is no
 * separate `document_uuids` field).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/svelte';

// jsdom has no IntersectionObserver; the infinite-scroll sentinel needs one to mount.
class StubIntersectionObserver {
  observe() {}
  disconnect() {}
  unobserve() {}
  takeRecords(): IntersectionObserverEntry[] {
    return [];
  }
}

// Identity translator — same pattern as PickerFilesTab.ownership.test.ts.
vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

const listDocumentsMock = vi.hoisted(() => vi.fn());
vi.mock('$lib/api/documents', () => ({ listDocuments: listDocumentsMock }));

import PickerDocumentsTab from './PickerDocumentsTab.svelte';

function documentsResponse(documents: unknown[], total?: number) {
  return { documents, total: total ?? documents.length, skip: 0, limit: 30 };
}

describe('PickerDocumentsTab (#362 lane C3)', () => {
  beforeEach(() => {
    listDocumentsMock.mockReset();
    vi.stubGlobal('IntersectionObserver', StubIntersectionObserver);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('requests only completed documents, sorted by filename', async () => {
    listDocumentsMock.mockResolvedValue(documentsResponse([]));

    render(PickerDocumentsTab, { props: { selected: [] } });

    await waitFor(() => expect(listDocumentsMock).toHaveBeenCalled());

    const [, , options] = listDocumentsMock.mock.calls[0];
    expect(options.status).toEqual(['completed']);
    expect(options.sortBy).toBe('filename');
  });

  it('lists documents and lets the caller select one', async () => {
    listDocumentsMock.mockResolvedValue(
      documentsResponse([{ uuid: 'doc-uuid-1', filename: 'report.pdf' }])
    );

    const changeHandler = vi.fn();
    render(PickerDocumentsTab, { props: { selected: [] }, events: { change: changeHandler } });

    const checkbox = await screen.findByTestId('picker-document-checkbox');
    expect(screen.getByText('report.pdf')).toBeInTheDocument();

    await fireEvent.click(checkbox);

    expect(changeHandler).toHaveBeenCalledTimes(1);
    expect(changeHandler.mock.calls[0][0].detail).toEqual(['doc-uuid-1']);
  });

  it('deselects an already-selected document', async () => {
    listDocumentsMock.mockResolvedValue(
      documentsResponse([{ uuid: 'doc-uuid-1', filename: 'report.pdf' }])
    );

    const changeHandler = vi.fn();
    render(PickerDocumentsTab, {
      props: { selected: ['doc-uuid-1', 'other-media-uuid'] },
      events: { change: changeHandler },
    });

    const checkbox = await screen.findByTestId('picker-document-checkbox');
    expect(checkbox).toBeChecked();

    await fireEvent.click(checkbox);

    // The media-file uuid already in the shared array must survive the toggle —
    // this tab must never clobber the other tab's selections.
    expect(changeHandler.mock.calls[0][0].detail).toEqual(['other-media-uuid']);
  });

  it('shows the empty state when no completed documents are found', async () => {
    listDocumentsMock.mockResolvedValue(documentsResponse([]));

    render(PickerDocumentsTab, { props: { selected: [] } });

    await waitFor(() => expect(listDocumentsMock).toHaveBeenCalled());
    expect(await screen.findByText('chat.picker.emptyDocuments')).toBeInTheDocument();
  });
});
