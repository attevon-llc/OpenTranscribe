import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/svelte';

vi.mock('$stores/locale', async () => {
  const { readable } = await import('svelte/store');
  const en = (await import('$lib/i18n/locales/en.json')).default as Record<string, string>;
  return {
    t: readable((key: string, opts?: Record<string, unknown>) => {
      // Mirror i18next's plural resolution: a `count` option selects the
      // `_one` / `_other` variant. Without it, pluralized keys fall through to
      // the raw key and every assertion on that copy fails for the wrong reason.
      const count = opts?.count;
      const plural =
        typeof count === 'number' ? (count === 1 ? `${key}_one` : `${key}_other`) : undefined;
      let out = (plural && en[plural]) ?? en[key] ?? key;
      for (const [name, value] of Object.entries(opts ?? {})) {
        out = out.split(`{{${name}}}`).join(String(value));
      }
      return out;
    }),
  };
});

const api = vi.hoisted(() => ({
  listTags: vi.fn(),
  createTag: vi.fn(),
  bulkTagFiles: vi.fn(),
}));

vi.mock('$lib/api/tags', () => api);

import BulkTagModal from './BulkTagModal.svelte';
import type { BulkTagActionResult, BulkTagOutcome } from '$lib/types/tag';

const fileUuids = Array.from({ length: 50 }, (_, i) => `file-${i}`);

/** Build a bulk response: `outcomes` is a per-file outcome list, in order. */
function results(outcomes: BulkTagOutcome[]): BulkTagActionResult[] {
  return outcomes.map((outcome, i) => ({
    file_uuid: `file-${i}`,
    success: outcome !== 'failed',
    message: `file-${i}: ${outcome}`,
    error: outcome === 'failed' ? 'TAG_UPDATE_FAILED' : null,
    outcome,
  }));
}

function repeat(outcome: BulkTagOutcome, times: number): BulkTagOutcome[] {
  return Array.from({ length: times }, () => outcome);
}

const tagList = [
  { uuid: 'a', name: 'interview', source: 'manual', usage_count: 12, awaiting_review: false },
  { uuid: 'b', name: 'roadmap', source: 'manual', usage_count: 4, awaiting_review: false },
  { uuid: 'c', name: 'q3 review', source: 'manual', usage_count: 2, awaiting_review: false },
];

beforeEach(() => {
  vi.clearAllMocks();
  api.listTags.mockResolvedValue(tagList);
  api.createTag.mockImplementation(async (name: string) => ({ uuid: 'a', name }));
  api.bulkTagFiles.mockResolvedValue(results(repeat('added', 50)));
});

async function type(value: string) {
  await fireEvent.input(screen.getByRole('combobox'), { target: { value } });
}

describe('BulkTagModal', () => {
  it('refuses to submit until a name is supplied', async () => {
    render(BulkTagModal, { props: { isOpen: true, action: 'add_tag', fileUuids } });

    const submit = screen.getByRole('button', { name: 'Add tag' });
    expect(submit).toBeDisabled();
    // The wrapping <label> names the input that lives inside SearchableSelect.
    expect(screen.getByLabelText('Tag name')).toBe(screen.getByRole('combobox'));

    await type('interview');
    expect(submit).toBeEnabled();
    expect(screen.getByRole('heading', { name: 'Add a tag to 50 file(s)' })).toBeInTheDocument();
  });

  it('reports how many files changed and how many already carried the tag', async () => {
    api.bulkTagFiles.mockResolvedValue(
      results([...repeat('added', 44), ...repeat('already_present', 6)])
    );
    const applied = vi.fn();
    render(BulkTagModal, {
      props: { isOpen: true, action: 'add_tag', fileUuids },
      events: { applied },
    });

    await type('interview');
    await fireEvent.click(screen.getByRole('button', { name: 'Add tag' }));

    await waitFor(() =>
      expect(screen.getByText('Tagged 44 file(s) with “interview”')).toBeInTheDocument()
    );
    expect(screen.getByText('6 file(s) already carried it — unchanged')).toBeInTheDocument();
    // An unchanged file is not a failure, so no failure line appears at all.
    expect(screen.queryByText(/could not be changed/)).toBeNull();

    expect(api.bulkTagFiles).toHaveBeenCalledWith(fileUuids, 'add_tag', 'interview');
    expect(applied.mock.calls[0][0].detail).toEqual({
      action: 'add_tag',
      name: 'interview',
      changed: 44,
      unchanged: 6,
      failed: 0,
    });
  });

  it('surfaces a per-file failure without implying the whole batch failed', async () => {
    api.bulkTagFiles.mockResolvedValue(
      results([...repeat('added', 47), ...repeat('already_present', 1), ...repeat('failed', 2)])
    );
    render(BulkTagModal, { props: { isOpen: true, action: 'add_tag', fileUuids } });

    await type('interview');
    await fireEvent.click(screen.getByRole('button', { name: 'Add tag' }));

    await waitFor(() =>
      expect(
        screen.getByText('2 file(s) could not be changed; every other file was applied')
      ).toBeInTheDocument()
    );
    // The successes are still reported, and nothing claims the batch failed.
    expect(screen.getByText('Tagged 47 file(s) with “interview”')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('names the tag a supplied name resolved to, before the outcome counts', async () => {
    // The backend resolves `Interview` to the existing `interview`, so that is
    // the name POST /tags hands back and the name the batch was applied under.
    api.createTag.mockResolvedValue({ uuid: 'a', name: 'interview' });
    api.bulkTagFiles.mockResolvedValue(results(repeat('added', 50)));
    render(BulkTagModal, { props: { isOpen: true, action: 'add_tag', fileUuids } });

    await type('Interview');
    await fireEvent.click(screen.getByRole('button', { name: 'Add tag' }));

    const summary = await screen.findByRole('status');
    const resolved = 'Applied the existing tag “interview” — you typed “Interview”.';
    const counts = 'Tagged 50 file(s) with “interview”';
    expect(summary).toHaveTextContent(resolved);
    expect(summary.textContent!.indexOf(resolved)).toBeLessThan(
      summary.textContent!.indexOf(counts)
    );
    // The resolved name — not what was typed — is what got applied.
    expect(api.bulkTagFiles).toHaveBeenCalledWith(fileUuids, 'add_tag', 'interview');
  });

  it('says nothing about resolution when the applied name is the typed name', async () => {
    render(BulkTagModal, { props: { isOpen: true, action: 'add_tag', fileUuids } });

    await type('interview');
    await fireEvent.click(screen.getByRole('button', { name: 'Add tag' }));

    await screen.findByRole('status');
    expect(screen.queryByText(/you typed/)).toBeNull();
  });

  it('scopes remove suggestions to the tags the selection carries', async () => {
    render(BulkTagModal, {
      props: {
        isOpen: true,
        action: 'remove_tag',
        fileUuids,
        presentTagNames: ['roadmap', 'roadmap', 'q3 review'],
      },
    });

    expect(
      screen.getByText('Only tags the selected files carry are offered.')
    ).toBeInTheDocument();

    await type('r');
    // `interview` matches the query and exists, but nothing in the selection
    // carries it — offering it would be a guaranteed no-op.
    await waitFor(() => expect(screen.getByRole('option', { name: 'roadmap' })).toBeInTheDocument());
    expect(screen.getByRole('option', { name: 'q3 review' })).toBeInTheDocument();
    expect(screen.queryByRole('option', { name: 'interview' })).toBeNull();
  });

  it('offers every tag for remove when the selection carries no tag data, and says so', async () => {
    render(BulkTagModal, { props: { isOpen: true, action: 'remove_tag', fileUuids } });

    expect(screen.getByText(/Every tag is offered/)).toBeInTheDocument();

    await type('e');
    await waitFor(() =>
      expect(screen.getByRole('option', { name: 'interview' })).toBeInTheDocument()
    );
    // Remove never creates a tag, so the resolver is never asked to.
    await fireEvent.click(screen.getByRole('option', { name: 'interview' }));
    await fireEvent.click(screen.getByRole('button', { name: 'Remove tag' }));

    await waitFor(() => expect(api.bulkTagFiles).toHaveBeenCalled());
    expect(api.createTag).not.toHaveBeenCalled();
    expect(api.bulkTagFiles).toHaveBeenCalledWith(fileUuids, 'remove_tag', 'interview');
  });

  it('reports the files that did not carry a removed tag as unchanged', async () => {
    api.bulkTagFiles.mockResolvedValue(
      results([...repeat('removed', 20), ...repeat('not_present', 30)])
    );
    render(BulkTagModal, { props: { isOpen: true, action: 'remove_tag', fileUuids } });

    await type('roadmap');
    await fireEvent.click(screen.getByRole('button', { name: 'Remove tag' }));

    await waitFor(() =>
      expect(screen.getByText('Removed “roadmap” from 20 file(s)')).toBeInTheDocument()
    );
    expect(screen.getByText('30 file(s) did not carry it — unchanged')).toBeInTheDocument();
  });

  it('reports a failed request as a request failure, not as a per-file outcome', async () => {
    api.bulkTagFiles.mockRejectedValue({ response: { data: { detail: 'tag rail exploded' } } });
    const applied = vi.fn();
    render(BulkTagModal, {
      props: { isOpen: true, action: 'add_tag', fileUuids },
      events: { applied },
    });

    await type('interview');
    await fireEvent.click(screen.getByRole('button', { name: 'Add tag' }));

    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('tag rail exploded'));
    expect(screen.queryByRole('status')).toBeNull();
    expect(applied).not.toHaveBeenCalled();
  });
});
