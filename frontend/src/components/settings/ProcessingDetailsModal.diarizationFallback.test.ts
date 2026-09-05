import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/svelte';
import { vi } from 'vitest';

vi.mock('$stores/locale', () => ({
  // Identity translator so assertions can match on the i18n key.
  t: { subscribe: (run: (value: (key: string) => string) => void) => (run((k) => k), () => {}) },
}));

import ProcessingDetailsModal from './ProcessingDetailsModal.svelte';

function diarizationModel(overrides: Record<string, unknown> = {}) {
  return {
    name: 'pyannote/speaker-diarization-community-1',
    description: 'PyAnnote (in-process fallback)',
    purpose: 'Speaker Identification & Segmentation',
    configured_backend: 'native',
    configured_description: 'Native diarization sidecar',
    effective_backend: 'pyannote',
    using_fallback: true,
    ...overrides,
  };
}

describe('ProcessingDetailsModal — diarization configured-vs-effective (issue #656)', () => {
  it('renders a fallback warning when the sidecar is configured but not serving', () => {
    render(ProcessingDetailsModal, {
      isOpen: true,
      section: 'models',
      stats: { models: { diarization: diarizationModel() } },
    });

    // The warning must name BOTH what was asked for and what is actually running —
    // a neutral "fallback active" line without either name is not actionable.
    expect(screen.getByText('settings.statistics.diarizationFallbackWarning')).toBeInTheDocument();
    expect(screen.getByText('settings.statistics.diarizationConfigured')).toBeInTheDocument();
    expect(screen.getByText('settings.statistics.diarizationEffective')).toBeInTheDocument();
    expect(screen.getByText('Native diarization sidecar')).toBeInTheDocument();
    expect(screen.getAllByText('PyAnnote (in-process fallback)').length).toBeGreaterThan(0);
  });

  it('shows configured/effective rows but no warning when native is actually serving', () => {
    render(ProcessingDetailsModal, {
      isOpen: true,
      section: 'models',
      stats: {
        models: {
          diarization: diarizationModel({
            description: 'Native diarization sidecar',
            effective_backend: 'native',
            using_fallback: false,
          }),
        },
      },
    });

    expect(screen.getByText('settings.statistics.diarizationConfigured')).toBeInTheDocument();
    expect(screen.getByText('settings.statistics.diarizationEffective')).toBeInTheDocument();
    expect(
      screen.queryByText('settings.statistics.diarizationFallbackWarning')
    ).not.toBeInTheDocument();
  });
});
