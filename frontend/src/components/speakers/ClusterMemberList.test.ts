/**
 * `ClusterMemberList.svelte` has the most real logic of the `speakers/`
 * children: it derives majority/minority member groups from
 * `cluster.gender_composition`, auto-fetches outlier analysis when a
 * gender-conflicted cluster is shown, and drives split/unassign checkbox
 * selection — dispatching `toggleSplitMember` / `toggleUnassignMember` rather
 * than owning selection state itself (the page/coordinator owns that, per
 * `components/speakers/CLAUDE.md`: "children take props + dispatch, the page
 * owns state").
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, fireEvent, waitFor } from '@testing-library/svelte';

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string, vars?: Record<string, unknown>) => string) => void) => {
      run((key: string, vars?: Record<string, unknown>) =>
        vars ? `${key}:${JSON.stringify(vars)}` : key
      );
      return () => {};
    },
  },
}));

vi.mock('$stores/audioPlaybackStore', async () => {
  const { writable } = await import('svelte/store');
  return { audioPlaybackStore: writable({ activeSpeakerUuid: null, isPlaying: false }) };
});

const mockAnalyzeClusterOutliers = vi.hoisted(() => vi.fn());
vi.mock('$lib/api/speakerClusters', () => ({ analyzeClusterOutliers: mockAnalyzeClusterOutliers }));

// Svelte 5 removed `component.$on(...)`, so dispatched events are only
// observable through an `on:event` listener in a consumer's markup.
import ClusterMemberListTestHost from './ClusterMemberListTestHost.svelte';
import type { SpeakerCluster, SpeakerClusterMember } from '$lib/types/speakerCluster';

function member(overrides: Partial<SpeakerClusterMember> = {}): SpeakerClusterMember {
  return {
    speaker_uuid: 'spk-1',
    speaker_name: 'SPEAKER_01',
    display_name: null,
    media_file_title: 'meeting.mp4',
    confidence: 0.9,
    predicted_gender: null,
    gender_confidence: null,
    gender_confirmed_by_user: false,
    has_audio_clip: false,
    verified: false,
    ...overrides,
  } as SpeakerClusterMember;
}

function cluster(overrides: Partial<SpeakerCluster> = {}): SpeakerCluster {
  return {
    uuid: 'cluster-1',
    gender_composition: null,
    ...overrides,
  } as SpeakerCluster;
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe('ClusterMemberList — outlier analysis auto-fetch', () => {
  it('fetches outlier analysis for a gender-conflicted cluster and renders the resulting badge on the matching member', async () => {
    mockAnalyzeClusterOutliers.mockResolvedValue({
      minority_analysis: [
        {
          speaker_uuid: 'spk-minority',
          sim_to_centroid: 0.4,
          avg_sim_to_majority: 0.3,
          recommendation: 'likely_outlier',
        },
      ],
    });
    const conflicted = cluster({
      uuid: 'cluster-conflict',
      gender_composition: {
        male_count: 2,
        female_count: 1,
        unknown_count: 0,
        total_with_gender: 3,
        dominant_gender: 'male',
        gender_coherence: 0.67,
        gender_label: 'mostly male',
        has_gender_conflict: true,
      },
    });

    const { container } = render(ClusterMemberListTestHost, {
      props: {
        members: [member({ speaker_uuid: 'spk-minority', predicted_gender: 'female' })],
        cluster: conflicted,
      },
    });

    await waitFor(() =>
      expect(mockAnalyzeClusterOutliers).toHaveBeenCalledWith('cluster-conflict')
    );
    await waitFor(() => {
      const badge = container.querySelector('.outlier-badge-red');
      expect(badge?.textContent).toContain('Outlier 40%');
    });
  });

  it('does not fetch outlier analysis for a cluster with no gender conflict, and renders no outlier UI at all', async () => {
    const clean = cluster({ uuid: 'cluster-clean', gender_composition: null });
    const { container } = render(ClusterMemberListTestHost, {
      props: { members: [member()], cluster: clean },
    });

    // Give any accidental async fetch a tick to fire, then assert it didn't.
    await new Promise((r) => setTimeout(r, 0));
    expect(mockAnalyzeClusterOutliers).not.toHaveBeenCalled();
    expect(container.querySelector('.outlier-loading')).toBeNull();
    expect(container.querySelector('.outlier-badge')).toBeNull();
    expect(container.querySelector('.gender-separator')).toBeNull();
  });
});

describe('ClusterMemberList — split/unassign selection is dispatched, not owned locally', () => {
  it('toggling a member checkbox in split mode dispatches toggleSplitMember with the speaker uuid, not local state', async () => {
    const toggled: unknown[] = [];
    const c = cluster({ uuid: 'cluster-split' });
    const { container } = render(ClusterMemberListTestHost, {
      props: {
        members: [member({ speaker_uuid: 'spk-target' })],
        cluster: c,
        splitMode: true,
        splitTargetUuid: 'cluster-split',
        splitSelectedMembers: new Set<string>(),
        onToggleSplitMember: (uuid: unknown) => toggled.push(uuid),
      },
    });

    const checkbox = container.querySelector('input[type="checkbox"]') as HTMLInputElement;
    expect(checkbox).not.toBeNull();
    await fireEvent.change(checkbox, { target: { checked: true } });

    expect(toggled).toEqual(['spk-target']);
  });

  it('the checkbox reflects checked state driven entirely by the splitSelectedMembers prop, not internal state', async () => {
    const c = cluster({ uuid: 'cluster-split' });
    const { container, rerender } = render(ClusterMemberListTestHost, {
      props: {
        members: [member({ speaker_uuid: 'spk-target' })],
        cluster: c,
        splitMode: true,
        splitTargetUuid: 'cluster-split',
        splitSelectedMembers: new Set<string>(),
      },
    });

    let checkbox = container.querySelector('input[type="checkbox"]') as HTMLInputElement;
    expect(checkbox.checked).toBe(false);

    await rerender({
      members: [member({ speaker_uuid: 'spk-target' })],
      cluster: c,
      splitMode: true,
      splitTargetUuid: 'cluster-split',
      splitSelectedMembers: new Set(['spk-target']),
    });

    checkbox = container.querySelector('input[type="checkbox"]') as HTMLInputElement;
    expect(checkbox.checked).toBe(true);
  });
});
