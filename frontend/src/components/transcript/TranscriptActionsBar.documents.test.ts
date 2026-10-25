import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/svelte';

vi.mock('$stores/supportSession', async () => {
  const { writable } = await import('svelte/store');
  return { supportSessionGate: writable({ active: false, readOnly: false }) };
});
vi.mock('$stores/locale', async () => {
  const { readable } = await import('svelte/store');
  return { t: readable((k: string) => k) };
});

import TranscriptActionsBar from './TranscriptActionsBar.svelte';
import { capabilities } from '$stores/capabilities';

const file = { uuid: 'f1', download_url: 'https://minio.example/x' } as never;

function setDocuments(enabled: boolean | undefined) {
  capabilities.set({
    edition: 'community',
    loaded: true,
    capabilities: enabled === undefined ? {} : { 'exports.documents': enabled },
    audience: {},
    maxUploadBytes: undefined,
  });
}

async function openMenu(
  props: Record<string, unknown> = {},
  events: Record<string, (event: CustomEvent) => void> = {}
) {
  const view = render(TranscriptActionsBar, {
    props: { file, diarizationDisabled: false, ...props },
    events,
  });
  await fireEvent.click(document.querySelector('.export-transcript-button') as HTMLElement);
  return view;
}

beforeEach(() => setDocuments(undefined));

describe('TranscriptActionsBar document exports (issue #1278)', () => {
  it('offers Markdown, Word and PDF beside the older formats', async () => {
    await openMenu();
    for (const key of [
      'transcript.exportText',
      'transcript.exportVtt',
      'transcript.exportMarkdown',
      'transcript.exportDocx',
      'transcript.exportPdf',
    ]) {
      expect(screen.getByText(key).closest('button'), key).toBeInstanceOf(HTMLButtonElement);
    }
  });

  it('hides Word and PDF when the deployment turns the capability off, and keeps Markdown', async () => {
    setDocuments(false);
    await openMenu();
    expect(screen.queryByText('transcript.exportDocx')).toBeNull();
    expect(screen.queryByText('transcript.exportPdf')).toBeNull();
    expect(screen.queryByText('transcript.exportMarkdown')).not.toBeNull();
  });

  it('dispatches the format the user picked', async () => {
    const picked: string[] = [];
    await openMenu(
      {},
      { exportTranscript: (e: CustomEvent<{ format: string }>) => picked.push(e.detail.format) }
    );
    await fireEvent.click(screen.getByText('transcript.exportPdf'));
    await fireEvent.click(document.querySelector('.export-transcript-button') as HTMLElement);
    await fireEvent.click(screen.getByText('transcript.exportMarkdown'));
    expect(picked).toEqual(['pdf', 'md']);
  });

  it('shows the busy label and disables both document entries while a job renders', async () => {
    await openMenu({ documentBusy: true });
    expect(screen.queryByText('documentExport.preparing')).not.toBeNull();
    const docx = screen.getByText('transcript.exportDocx').closest('button') as HTMLButtonElement;
    const pdf = screen.getByText('transcript.exportPdf').closest('button') as HTMLButtonElement;
    expect(docx.disabled).toBe(true);
    expect(pdf.disabled).toBe(true);
    const text = screen.getByText('transcript.exportText').closest('button') as HTMLButtonElement;
    expect(text.disabled).toBe(false);
  });
});
