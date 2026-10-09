import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/svelte';

const h = vi.hoisted(() => ({ gate: null as unknown as import('svelte/store').Writable<unknown> }));

vi.mock('$stores/supportSession', async () => {
  const { writable } = await import('svelte/store');
  h.gate = writable({ active: false, readOnly: false });
  return { supportSessionGate: h.gate };
});
vi.mock('$stores/locale', async () => {
  const { readable } = await import('svelte/store');
  return { t: readable((k: string) => k) };
});

import TranscriptActionsBar from './TranscriptActionsBar.svelte';

const file = { uuid: 'f1', download_url: 'https://minio.example/x' } as never;

function open() {
  return render(TranscriptActionsBar, { props: { file, diarizationDisabled: false } });
}

beforeEach(() => h.gate.set({ active: false, readOnly: false }));

describe('TranscriptActionsBar under a support session', () => {
  it('normally offers export, speaker editing and download', () => {
    open();
    expect(screen.getByText('transcript.export')).toBeTruthy();
    expect(screen.getByText('transcript.download')).toBeTruthy();
    expect(document.querySelector('.edit-speakers-button')).not.toBeNull();
  });

  it('a WRITE grant loses export and download (refused server-side) but keeps speaker editing', () => {
    h.gate.set({ active: true, readOnly: false });
    open();
    expect(screen.queryByText('transcript.export')).toBeNull();
    expect(screen.queryByText('transcript.download')).toBeNull();
    expect(document.querySelector('.edit-speakers-button')).not.toBeNull();
  });

  it('a READ grant additionally loses speaker editing', () => {
    h.gate.set({ active: true, readOnly: true });
    open();
    expect(screen.queryByText('transcript.export')).toBeNull();
    expect(screen.queryByText('transcript.download')).toBeNull();
    expect(document.querySelector('.edit-speakers-button')).toBeNull();
  });
});
