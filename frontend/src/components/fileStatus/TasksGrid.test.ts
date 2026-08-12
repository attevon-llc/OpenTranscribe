/**
 * DEFECT THESE CATCH: `TasksGrid.svelte` had no test, and no test anywhere in the
 * suite even named it — yet it renders `task.progress` and `task.task_type`, the
 * two fields the backend only recently started reporting truthfully (progress was
 * a hardcoded `0.5` for eleven months).
 *
 * Two concrete failures were reachable:
 *   1. `style="width: {task.progress * 100}%"` had NO undefined guard, so a task
 *      row missing `progress` emitted `width: NaN%` — an invalid declaration the
 *      browser silently drops, leaving a zero-width bar next to the text "NaN%".
 *   2. If the backend ever switched `progress` to a 0-100 scale, the unclamped
 *      multiply would render a 5000%-wide bar and nothing would fail.
 *
 * The bar width is asserted from the inline style, not from a mock, because the
 * inline style IS the rendered artefact that was wrong.
 */

import { describe, it, expect, vi } from 'vitest';
import { render } from '@testing-library/svelte';

vi.mock('$stores/locale', async () => {
  const { readable } = await import('svelte/store');
  const en = (await import('$lib/i18n/locales/en.json')).default as Record<string, string>;
  return { t: readable((key: string) => en[key] ?? key) };
});

import TasksGrid from './TasksGrid.svelte';

interface TaskRow {
  id: number;
  task_type: string;
  status: string;
  progress?: number | null;
  media_file?: { uuid: string; filename: string };
}

function task(overrides: Partial<TaskRow> = {}): TaskRow {
  return {
    id: 1,
    task_type: 'transcription',
    status: 'in_progress',
    progress: 0.42,
    media_file: { uuid: 'file-1', filename: 'interview.mp4' },
    ...overrides,
  };
}

function renderGrid(tasks: TaskRow[]) {
  return render(TasksGrid, {
    props: { tasks, filteredTasks: tasks, tasksLoading: false, tasksError: null },
  });
}

function barWidth(container: HTMLElement): string | undefined {
  const bar = container.querySelector<HTMLElement>('.progress-bar');
  return bar?.style.width;
}

describe('TasksGrid — progress rendering', () => {
  it('renders a 0..1 progress as a percentage width and label', () => {
    const { container } = renderGrid([task({ progress: 0.42 })]);

    expect(barWidth(container)).toBe('42%');
    expect(container.textContent).toContain('42%');
  });

  it('renders 0%, never NaN%, when progress is missing', () => {
    const { container } = renderGrid([task({ progress: undefined })]);

    expect(barWidth(container)).toBe('0%');
    expect(container.textContent).not.toContain('NaN');
  });

  it('renders 0%, never NaN%, when progress is null', () => {
    // `null !== undefined`, so an `undefined`-only guard lets this through.
    const { container } = renderGrid([task({ progress: null })]);

    expect(barWidth(container)).toBe('0%');
    expect(container.textContent).not.toContain('NaN');
  });

  it('clamps a 0-100-scaled value to 100% instead of a 5000%-wide bar', () => {
    // The scale-change scenario: backend starts sending 50 instead of 0.5.
    const { container } = renderGrid([task({ progress: 50 })]);

    expect(barWidth(container)).toBe('100%');
    expect(container.textContent).toContain('100%');
    expect(container.textContent).not.toContain('5000%');
  });

  it('clamps a negative value to 0%', () => {
    const { container } = renderGrid([task({ progress: -0.5 })]);

    expect(barWidth(container)).toBe('0%');
  });

  it('shows no progress bar for a task that is not in progress', () => {
    const { container } = renderGrid([task({ status: 'completed', progress: 1 })]);

    expect(container.querySelector('.progress-bar')).toBeNull();
  });

  it('labels each task by task_type — the other recently-changed backend field', () => {
    const { container } = renderGrid([
      task({ id: 1, task_type: 'transcription' }),
      task({ id: 2, task_type: 'search_indexing' }),
      task({ id: 3, task_type: 'summarization' }),
    ]);

    // Real copy, so a renamed task_type shows up as a wrong label rather than a
    // missing translation key.
    expect(container.textContent).toContain('Transcription');
    expect(container.textContent).toContain('Search');
    expect(container.textContent).toContain('Summarization');
  });

  it('falls back to the summarization label for an unrecognised task_type', () => {
    // Pinning current behaviour: the {:else} branch is a catch-all, so a NEW
    // backend task type is silently mislabelled "Summarization". Documented here
    // so the next task type added is a conscious change to this component.
    const { container } = renderGrid([task({ task_type: 'redaction' })]);

    expect(container.textContent).toContain('Summarization');
  });
});
