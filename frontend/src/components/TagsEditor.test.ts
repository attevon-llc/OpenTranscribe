import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/svelte';

import en from '$lib/i18n/locales/en.json';

const strings = en as Record<string, string>;

function translate(key: string, opts?: Record<string, unknown>) {
  let out = strings[key] ?? key;
  for (const [name, value] of Object.entries(opts ?? {})) {
    out = out.split(`{{${name}}}`).join(String(value));
  }
  return out;
}

vi.mock('$stores/locale', async () => {
  const { readable } = await import('svelte/store');
  return { t: readable(translate) };
});

const toast = vi.hoisted(() => ({
  show: vi.fn(),
  success: vi.fn(),
  error: vi.fn(),
  warning: vi.fn(),
  info: vi.fn(),
  dismiss: vi.fn(),
  clear: vi.fn(),
}));

vi.mock('$stores/toast', () => ({ toastStore: toast }));

const api = vi.hoisted(() => ({
  listTags: vi.fn(),
  addTagToFile: vi.fn(),
  removeTagFromFile: vi.fn(),
  createTag: vi.fn(),
}));

vi.mock('$lib/api/tags', () => api);

import TagsEditor from './TagsEditor.svelte';

type Row = { uuid: string; name: string; source?: string; usage_count?: number };

function library(...rows: Row[]) {
  api.listTags.mockResolvedValue(
    rows.map((row) => ({ source: 'manual', usage_count: 1, awaiting_review: false, ...row }))
  );
}

/**
 * The server resolves a supplied name to a row. Every case here spells out that
 * mapping rather than echoing the input, because "what the server returned" is
 * exactly what the editor is supposed to report.
 */
function resolvesTo(map: Record<string, Row>) {
  api.addTagToFile.mockImplementation(async (_fileUuid: string, name: string) => {
    const row = map[name];
    if (!row) throw new Error(`test: no resolution configured for ${name}`);
    return { source: 'manual', ...row };
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  library();
  api.addTagToFile.mockResolvedValue({ uuid: 'new', name: 'new', source: 'manual' });
});

async function mounted(props: Record<string, unknown> = {}) {
  const result = render(TagsEditor, { fileId: 'file-1', tags: [], aiSuggestions: [], ...props });
  await waitFor(() => expect(api.listTags).toHaveBeenCalled());
  return result;
}

async function submit(value: string) {
  const input = screen.getByPlaceholderText(strings['tags.addTagPlaceholder']);
  await fireEvent.input(input, { target: { value } });
  await fireEvent.keyDown(input, { key: 'Enter' });
}

describe('TagsEditor resolved-name feedback', () => {
  it('applies the existing tag a case variant resolves to and announces it', async () => {
    library({ uuid: 'a', name: 'interview', usage_count: 12 });
    resolvesTo({ Interview: { uuid: 'a', name: 'interview' } });
    await mounted();

    await submit('Interview');

    await waitFor(() => expect(api.addTagToFile).toHaveBeenCalledWith('file-1', 'Interview'));
    const announcement = translate('tags.resolvedTo', { name: 'interview', typed: 'Interview' });
    expect(toast.info).toHaveBeenCalledWith(announcement);
    expect(await screen.findByRole('status')).toHaveTextContent('interview');
  });

  it('applies an exact new name with no announcement', async () => {
    library({ uuid: 'a', name: 'interview', usage_count: 12 });
    resolvesTo({ budget: { uuid: 'b', name: 'budget' } });
    await mounted();

    await submit('budget');

    await waitFor(() => expect(api.addTagToFile).toHaveBeenCalledWith('file-1', 'budget'));
    expect(toast.info).not.toHaveBeenCalled();
    expect(screen.queryByRole('status')).toBeNull();
  });

  it('offers a near match instead of applying it, and applies it once accepted', async () => {
    library({ uuid: 'q3', name: 'q3-earnings', usage_count: 9 });
    resolvesTo({ 'q3-earnings': { uuid: 'q3', name: 'q3-earnings' } });
    await mounted();

    await submit('q4-earnings');

    // The offer is the whole point: nothing may reach the server yet, because
    // a wrongly collapsed tag cannot be split apart again.
    const accept = await screen.findByRole('button', {
      name: translate('tags.nearMatch.accept', { name: 'q3-earnings' }),
    });
    expect(api.addTagToFile).not.toHaveBeenCalled();

    await fireEvent.click(accept);

    await waitFor(() => expect(api.addTagToFile).toHaveBeenCalledWith('file-1', 'q3-earnings'));
    expect(toast.info).toHaveBeenCalledWith(
      translate('tags.resolvedTo', { name: 'q3-earnings', typed: 'q4-earnings' })
    );
  });

  it('creates the typed tag when the near match is declined', async () => {
    library({ uuid: 'q3', name: 'q3-earnings', usage_count: 9 });
    resolvesTo({ 'q4-earnings': { uuid: 'q4', name: 'q4-earnings' } });
    await mounted();

    await submit('q4-earnings');

    const decline = await screen.findByRole('button', {
      name: translate('tags.nearMatch.decline', { typed: 'q4-earnings' }),
    });
    await fireEvent.click(decline);

    await waitFor(() => expect(api.addTagToFile).toHaveBeenCalledWith('file-1', 'q4-earnings'));
    expect(api.addTagToFile).toHaveBeenCalledTimes(1);
    expect(toast.info).not.toHaveBeenCalled();
  });

  it('does not offer a suggestion that resolves to a tag already on the file', async () => {
    // `Q3-Review` and `q3 review` normalize to the same name, so applying the
    // first would return the row the file already carries — a silent no-op.
    library(
      { uuid: 'dup', name: 'Q3-Review', usage_count: 7 },
      { uuid: 'other', name: 'roadmap', usage_count: 3 }
    );
    await mounted({
      tags: [{ uuid: 'kept', name: 'q3 review', source: 'manual' }],
      aiSuggestions: [{ name: 'Q3_review', confidence: 0.9 }],
    });

    await waitFor(() => expect(screen.getByRole('button', { name: 'roadmap' })).toBeTruthy());
    expect(screen.queryByRole('button', { name: 'Q3-Review' })).toBeNull();
    expect(screen.queryByText('Q3_review')).toBeNull();
  });

  it('refuses a name that is empty once normalized', async () => {
    await mounted();

    await submit('   -_-   ');

    expect(api.addTagToFile).not.toHaveBeenCalled();
    expect(toast.error).toHaveBeenCalledWith(strings['tags.nameRequired']);
  });
});
