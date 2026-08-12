/**
 * DEFECT THESE CATCH: `FileDetailModal.svelte` had no test and rendered task
 * progress behind `{#if task.progress !== undefined}`. The backend schema is a
 * non-optional `float`, so the guard read as safe — but `null !== undefined` is
 * TRUE, so the moment the field is loosened to nullable (or an older/partial
 * payload arrives) the modal prints `Math.round(null * 100)` = **"0%"**. A
 * confident wrong number is worse than a visibly broken one: "0%" is
 * indistinguishable from a task that genuinely has not started.
 *
 * The guard now requires a finite number, and the value goes through
 * `taskProgressPercent`, which also clamps a 0-100-scaled value instead of
 * rendering 5000%.
 */

import { describe, it, expect, vi } from 'vitest';
import { render } from '@testing-library/svelte';

vi.mock('$stores/locale', async () => {
  const { readable } = await import('svelte/store');
  const en = (await import('$lib/i18n/locales/en.json')).default as Record<string, string>;
  return { t: readable((key: string) => en[key] ?? key) };
});

import FileDetailModal from './FileDetailModal.svelte';

interface TaskDetail {
  task_type: string;
  status: string;
  progress?: number | null;
  created_at: string;
}

function detailedStatus(taskOverrides: Partial<TaskDetail> = {}) {
  return {
    file: {
      uuid: 'file-1',
      filename: 'interview.mp4',
      status: 'processing',
      display_status: 'processing',
      upload_time: '2026-08-01T10:00:00Z',
    },
    is_stuck: false,
    can_retry: false,
    task_details: [
      {
        task_type: 'transcription',
        status: 'in_progress',
        progress: 0.37,
        created_at: '2026-08-01T10:00:05Z',
        ...taskOverrides,
      },
    ],
  };
}

function renderModal(taskOverrides: Partial<TaskDetail> = {}) {
  return render(FileDetailModal, {
    props: {
      detailedStatus: detailedStatus(taskOverrides),
      selectedFile: 'file-1',
      retryingFiles: new Set<string>(),
    },
  });
}

/** The rendered "Progress:" metadata value, or null when the row is absent. */
function progressRow(container: HTMLElement): string | null {
  const labels = Array.from(container.querySelectorAll('.metadata-label'));
  const label = labels.find((el) => el.textContent?.trim().startsWith('Progress'));
  return label?.nextElementSibling?.textContent?.trim() ?? null;
}

describe('FileDetailModal — task progress guard', () => {
  it('shows the progress row for an in-progress task with a real value', () => {
    const { container } = renderModal({ progress: 0.37 });

    expect(progressRow(container)).toBe('37%');
  });

  it('HIDES the row when progress is null, rather than printing a confident "0%"', () => {
    const { container } = renderModal({ progress: null });

    expect(progressRow(container)).toBeNull();
  });

  it('hides the row when progress is absent', () => {
    const { container } = renderModal({ progress: undefined });

    expect(progressRow(container)).toBeNull();
  });

  it('hides the row when progress is NaN', () => {
    const { container } = renderModal({ progress: Number.NaN });

    expect(progressRow(container)).toBeNull();
  });

  it('clamps rather than rendering 5000% if the scale changes to 0-100', () => {
    const { container } = renderModal({ progress: 50 });

    expect(progressRow(container)).toBe('100%');
  });

  it('hides the row for a task that is not in progress', () => {
    const { container } = renderModal({ status: 'completed', progress: 1 });

    expect(progressRow(container)).toBeNull();
  });

  it('renders 0% only when progress is genuinely zero', () => {
    // The distinction the null-guard destroyed: "not started" must be
    // distinguishable from "we do not know".
    const { container } = renderModal({ progress: 0 });

    expect(progressRow(container)).toBe('0%');
  });
});
