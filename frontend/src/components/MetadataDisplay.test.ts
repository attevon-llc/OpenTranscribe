/**
 * Which diarization engine actually served THIS file (#706). `diarization_provider` is
 * `"native"` | `"pyannote"` | `null` — `null` means "not diarized / not recorded" and must
 * never render a default. Distinct from the admin panel's configured-vs-effective display
 * (`ProcessingDetailsModal.svelte`) — this answers "what ran on this file", not "what is the
 * deployment set to".
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/svelte';
import type { MediaFileDetail } from '$lib/types/media';

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

vi.mock('$stores/auth', () => ({
  authStore: {
    subscribe: (run: (value: unknown) => void) => {
      run({ user: { email: 'admin@example.com' } });
      return () => {};
    },
  },
}));

vi.mock('../lib/axios', () => ({ default: { put: vi.fn() } }));

import MetadataDisplay from './MetadataDisplay.svelte';

function baseFile(overrides: Partial<MediaFileDetail> = {}): MediaFileDetail {
  return {
    uuid: 'file-1',
    filename: 'meeting.mp3',
    status: 'completed',
    ...overrides,
  } as MediaFileDetail;
}

describe('MetadataDisplay diarization engine field (#706)', () => {
  it('renders "Native" when diarization_provider is native', () => {
    render(MetadataDisplay, {
      file: baseFile({ diarization_provider: 'native' }),
      showMetadata: true,
    });
    expect(screen.getByText('metadata.diarizationEngineLabel')).toBeTruthy();
    expect(screen.getByText('metadata.diarizationEngineNative')).toBeTruthy();
  });

  it('renders "PyAnnote" when diarization_provider is pyannote (direct or fallen-back)', () => {
    render(MetadataDisplay, {
      file: baseFile({ diarization_provider: 'pyannote' }),
      showMetadata: true,
    });
    expect(screen.getByText('metadata.diarizationEngineLabel')).toBeTruthy();
    expect(screen.getByText('metadata.diarizationEnginePyannote')).toBeTruthy();
  });

  it('does NOT render the diarization engine field, or invent a default, when null', () => {
    render(MetadataDisplay, { file: baseFile({ diarization_provider: null }), showMetadata: true });
    expect(screen.queryByText('metadata.diarizationEngineLabel')).toBeNull();
    expect(screen.queryByText('metadata.diarizationEngineNative')).toBeNull();
    expect(screen.queryByText('metadata.diarizationEnginePyannote')).toBeNull();
  });

  it('does NOT render the diarization engine field when undefined (not recorded)', () => {
    render(MetadataDisplay, { file: baseFile(), showMetadata: true });
    expect(screen.queryByText('metadata.diarizationEngineLabel')).toBeNull();
  });
});
