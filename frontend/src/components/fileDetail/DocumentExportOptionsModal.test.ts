import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/svelte';

vi.mock('$stores/locale', async () => {
  const { readable } = await import('svelte/store');
  return { t: readable((k: string) => k) };
});

import DocumentExportOptionsModal from './DocumentExportOptionsModal.svelte';

function open(
  props: Record<string, unknown> = {},
  events: Record<string, (event: CustomEvent) => void> = {}
) {
  return render(DocumentExportOptionsModal, { props: { show: true, ...props }, events });
}

describe('DocumentExportOptionsModal', () => {
  it('offers line numbering and page size with every choice', () => {
    open();
    const numbering = document.getElementById(
      'document-export-line-numbering'
    ) as HTMLSelectElement;
    const size = document.getElementById('document-export-page-size') as HTMLSelectElement;
    expect([...numbering.options].map((o) => o.value)).toEqual(['per_page', 'continuous', 'none']);
    expect([...size.options].map((o) => o.value)).toEqual(['a4', 'letter']);
  });

  it('shows the note for the format being prepared', () => {
    const { unmount } = open({ format: 'docx' });
    expect(screen.queryByText('documentExport.docxNote')).not.toBeNull();
    expect(screen.queryByText('documentExport.pdfNote')).toBeNull();
    unmount();
    open({ format: 'pdf' });
    expect(screen.queryByText('documentExport.pdfNote')).not.toBeNull();
  });

  it('offers the comments toggle only when the file has comments', () => {
    const { unmount } = open({ hasComments: false });
    expect(screen.queryByText('exportOptions.includeComments')).toBeNull();
    unmount();
    open({ hasComments: true });
    expect(screen.queryByText('exportOptions.includeComments')).not.toBeNull();
  });

  it('explains masking only while redaction is active and the original is not shown', () => {
    const { unmount } = open({ redactionActive: true, showOriginal: false });
    expect(screen.queryByText(/settings.contentRedaction.exportRedactedNote/)).not.toBeNull();
    unmount();
    open({ redactionActive: true, showOriginal: true });
    expect(screen.queryByText(/settings.contentRedaction.exportRedactedNote/)).toBeNull();
  });

  it('confirms with the chosen options', async () => {
    const onConfirm = vi.fn();
    open({}, { confirm: onConfirm });
    await fireEvent.click(screen.getByText('exportOptions.export'));
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });
});
