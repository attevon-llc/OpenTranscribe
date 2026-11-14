import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/svelte';
import { vi } from 'vitest';

vi.mock('$stores/locale', async () => (await import('$testMocks/localeMocks')).keyLocale());

import ProcessingDetailsModal from './ProcessingDetailsModal.svelte';
import {
  initialSystemStats,
  type DiarizationModelStats,
  type SystemStats,
} from '$lib/settings/adminLoaders';

function statsWithDiarization(diarization: DiarizationModelStats): SystemStats {
  const base = initialSystemStats((key) => key);
  return { ...base, models: { ...base.models, diarization } };
}

function diarizationModel(overrides: Partial<DiarizationModelStats> = {}): DiarizationModelStats {
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
      stats: statsWithDiarization(diarizationModel()),
    });

    // The warning must name BOTH what was asked for and what is actually running —
    // a neutral "fallback active" line without either name is not actionable.
    expect(screen.getByText('settings.statistics.diarizationFallbackWarning')).toBeInTheDocument();
    expect(screen.getByText('settings.statistics.diarizationConfigured')).toBeInTheDocument();
    expect(screen.getByText('settings.statistics.diarizationEffective')).toBeInTheDocument();
    // Engines are named from their stable code in the UI language, not the server's English.
    expect(screen.getByText('settings.statistics.diarEngine.native')).toBeInTheDocument();
    expect(screen.getAllByText('settings.statistics.diarEngine.pyannote').length).toBeGreaterThan(
      0
    );
    expect(screen.queryByText('Native diarization sidecar')).not.toBeInTheDocument();
    expect(screen.getByText('settings.statistics.modelPurpose.diarization')).toBeInTheDocument();
  });

  it('keeps the server description for an engine code this build does not know', () => {
    render(ProcessingDetailsModal, {
      isOpen: true,
      section: 'models',
      stats: statsWithDiarization(
        diarizationModel({ effective_backend: 'future', description: 'Future engine' })
      ),
    });

    expect(screen.getAllByText('Future engine').length).toBeGreaterThan(0);
  });

  it('shows configured/effective rows but no warning when native is actually serving', () => {
    render(ProcessingDetailsModal, {
      isOpen: true,
      section: 'models',
      stats: statsWithDiarization(
        diarizationModel({
          description: 'Native diarization sidecar',
          effective_backend: 'native',
          using_fallback: false,
        })
      ),
    });

    expect(screen.getByText('settings.statistics.diarizationConfigured')).toBeInTheDocument();
    expect(screen.getByText('settings.statistics.diarizationEffective')).toBeInTheDocument();
    expect(
      screen.queryByText('settings.statistics.diarizationFallbackWarning')
    ).not.toBeInTheDocument();
  });
});
