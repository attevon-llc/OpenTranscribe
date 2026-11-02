import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/svelte';

vi.mock('$stores/locale', async () => {
  const { readable } = await import('svelte/store');
  return {
    t: readable((key: string, opts?: Record<string, unknown>) =>
      opts ? `${key} ${JSON.stringify(opts)}` : key
    ),
  };
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
}));
vi.mock('$lib/api/tags', () => api);

import TagsEditor from './TagsEditor.svelte';

type Row = { uuid: string; name: string; source?: string; usage_count?: number };

const row = (uuid: string, name: string, usage_count = 1, source = 'manual'): Row => ({
  uuid,
  name,
  source,
  usage_count,
});

function library(...rows: unknown[]) {
  api.listTags.mockResolvedValue(rows);
}

function deferred<V>() {
  let resolve!: (value: V) => void;
  const promise = new Promise<V>((res) => (resolve = res));
  return { promise, resolve };
}

beforeEach(() => {
  vi.clearAllMocks();
  library();
  api.addTagToFile.mockImplementation(async (_f: string, name: string) => row('new', name));
  api.removeTagFromFile.mockResolvedValue(undefined);
});

async function mounted(
  props: Record<string, unknown> = {},
  events: Record<string, (event: CustomEvent) => void> = {}
) {
  const view = render(TagsEditor, {
    props: { fileId: 'file-1', tags: [], aiSuggestions: [], ...props },
    events,
  });
  await waitFor(() => expect(api.listTags).toHaveBeenCalledTimes(1));
  await Promise.resolve();
  return view;
}

const input = () => document.querySelector('.tag-input') as HTMLInputElement;
const typeIn = (value: string) => fireEvent.input(input(), { target: { value } });
const addButton = () => document.querySelector('.tag-add-button') as HTMLButtonElement | null;
const detail = (fn: ReturnType<typeof vi.fn>, call = 0) => fn.mock.calls[call][0].detail;

async function submit(value: string) {
  await typeIn(value);
  await fireEvent.keyDown(input(), { key: 'Enter' });
}

describe('loading the library', () => {
  it('drops rows without a uuid or a name', async () => {
    library(row('a', 'keepme', 9), { uuid: '', name: 'nouuid' }, { uuid: 'b', name: '' }, null);
    await mounted();
    expect(await screen.findByRole('button', { name: 'keepme' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'nouuid' })).toBeNull();
  });

  it.each([
    ['401', { response: { status: 401 } }, 'tags.unauthorizedLogin'],
    ['a network failure', { code: 'ERR_NETWORK' }, 'tags.networkError'],
    ['anything else', { response: { status: 500 } }, 'tags.failedToLoad'],
  ])('toasts the right key for %s', async (_what, error, key) => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    api.listTags.mockRejectedValue(error);
    await mounted();
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith(key));
    expect(toast.error).toHaveBeenCalledTimes(1);
    spy.mockRestore();
  });
});

describe('what is shown', () => {
  it('says the file has no tags when it has none', async () => {
    await mounted();
    expect(screen.getByText('tags.noTagsYet')).toBeInTheDocument();
    expect(document.querySelectorAll('.tag-remove')).toHaveLength(0);
  });

  it('shows a chip with a remove button per tag, marking auto-applied ones', async () => {
    await mounted({
      tags: [row('m', 'manual-one'), row('a', 'ai-one', 1, 'auto_ai')],
    });
    expect(screen.queryByText('tags.noTagsYet')).toBeNull();
    const chips = Array.from(document.querySelectorAll('.tags-list .tag'));
    expect(chips).toHaveLength(2);
    expect(chips.map((c) => c.classList.contains('ai-tag'))).toEqual([false, true]);
    expect(chips[0].querySelector('.ai-icon')).toBeNull();
    expect(chips[1].querySelector('.ai-icon')).toHaveAttribute('title', 'autoLabel.autoAppliedTag');
    expect(document.querySelectorAll('.tag-remove')).toHaveLength(2);
    expect(chips[0]).toHaveTextContent('manual-one');
  });

  it('configures the input', async () => {
    await mounted();
    // D16 (8f96d6e10): the width is enforced by clampTagNameInput in code points, not a
    // native maxlength, which counts UTF-16 units and stops an emoji name at half width.
    expect(input()).not.toHaveAttribute('maxlength');
    expect(input()).toHaveAttribute('placeholder', 'tags.addTagPlaceholder');
    expect(input()).toHaveAttribute('title', 'tags.typeNewTagHint');
  });

  it('shows the Add button only for a non-blank input, with the trimmed name in its tooltip', async () => {
    await mounted();
    expect(addButton()).toBeNull();
    await typeIn('   ');
    expect(addButton()).toBeNull();
    await typeIn('  new thing ');
    expect(addButton()).toHaveAttribute('title', 'tags.createAndAddHint {"tagName":"new thing"}');
    expect(addButton()).toHaveTextContent('tags.add');
  });
});

describe('typed names', () => {
  it('trims and clamps the name sent to the server', async () => {
    await mounted();
    // D16 (8f96d6e10): the field clamps the raw text to 50 code points while typing, so the
    // two leading spaces use up width before the trim: 2 spaces + 48 x -> 48 x.
    await submit(`  ${'x'.repeat(60)}  `);
    await waitFor(() => expect(api.addTagToFile).toHaveBeenCalledWith('file-1', 'x'.repeat(48)));
  });

  it('clamps the field to 50 code points as the user types and sends exactly that', async () => {
    await mounted();
    await typeIn('x'.repeat(60));
    expect(input().value).toBe('x'.repeat(50));
    await fireEvent.keyDown(input(), { key: 'Enter' });
    await waitFor(() => expect(api.addTagToFile).toHaveBeenCalledWith('file-1', 'x'.repeat(50)));

    // 50 astral characters are 100 UTF-16 units; a native maxlength would have cut them to 25.
    await typeIn('\u{1F600}'.repeat(60));
    expect(Array.from(input().value)).toHaveLength(50);
  });

  it('ignores Enter on a blank input and submits through the Add button', async () => {
    await mounted();
    await typeIn('   ');
    await fireEvent.keyDown(input(), { key: 'Enter' });
    expect(api.addTagToFile).not.toHaveBeenCalled();
    expect(toast.error).not.toHaveBeenCalled();

    await typeIn('thing');
    await fireEvent.click(addButton()!);
    await waitFor(() => expect(api.addTagToFile).toHaveBeenCalledWith('file-1', 'thing'));
  });

  it('refuses a separator-only name with a toast and no request', async () => {
    await mounted();
    await typeIn('-_-');
    await fireEvent.click(addButton()!);
    expect(toast.error).toHaveBeenCalledWith('tags.nameRequired');
    expect(api.addTagToFile).not.toHaveBeenCalled();
  });

  it('appends the returned tag, emits the new list and clears the input', async () => {
    const tagsUpdated = vi.fn();
    await mounted({ tags: [row('old', 'old')] }, { tagsUpdated });
    await submit('fresh');
    await waitFor(() => expect(tagsUpdated).toHaveBeenCalledTimes(1));
    expect(detail(tagsUpdated).tags.map((t: Row) => t.name)).toEqual(['old', 'fresh']);
    expect(input().value).toBe('');
    expect(screen.queryByRole('status')).toBeNull();
    expect(toast.info).not.toHaveBeenCalled();
  });

  it('goes straight to the server on a normalized-exact library hit, near matches notwithstanding', async () => {
    library(row('a', 'q3 earnings-x', 9), row('b', 'Q3_Earnings', 2));
    await mounted();
    await submit('q3-earnings');
    await waitFor(() => expect(api.addTagToFile).toHaveBeenCalledWith('file-1', 'q3-earnings'));
    expect(document.querySelector('.near-match')).toBeNull();
  });

  it('announces a different resolved name in a live region and a toast', async () => {
    api.addTagToFile.mockResolvedValue(row('a', 'interview'));
    await mounted();
    await submit('Interview');
    const text = 'tags.resolvedTo {"name":"interview","typed":"Interview"}';
    expect(await screen.findByRole('status')).toHaveTextContent(text);
    expect(toast.info).toHaveBeenCalledWith(text);
  });

  it('announces a tag already on the file as a no-op, without emitting', async () => {
    api.addTagToFile.mockResolvedValue(row('a', 'interview'));
    const tagsUpdated = vi.fn();
    await mounted({ tags: [row('a', 'interview')] }, { tagsUpdated });
    await submit('INTERVIEW');
    const text = 'tags.alreadyOnFile {"name":"interview"}';
    expect(await screen.findByRole('status')).toHaveTextContent(text);
    expect(toast.info).toHaveBeenCalledWith(text);
    expect(tagsUpdated).not.toHaveBeenCalled();
    expect(input().value).toBe('');
  });

  it.each([
    ['no uuid', { name: 'x' }],
    ['no name', { uuid: 'u' }],
    ['nothing', null],
  ])('treats a response with %s as a failure', async (_what, response) => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    api.addTagToFile.mockResolvedValue(response);
    const tagsUpdated = vi.fn();
    await mounted({}, { tagsUpdated });
    await submit('thing');
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith('tags.failedToCreate'));
    expect(tagsUpdated).not.toHaveBeenCalled();
    expect(input().value).toBe('thing');
    spy.mockRestore();
  });

  it.each([
    ['401', { response: { status: 401 } }, 'tags.unauthorizedLogin'],
    [
      'a detail',
      { response: { status: 403, data: { detail: 'Requires editor permission on this file' } } },
      'tags.failedToCreateWithDetail {"detail":"Requires editor permission on this file"}',
    ],
    ['no detail', { response: { status: 500, data: {} } }, 'tags.failedToCreate'],
    ['no response', new Error('x'), 'tags.failedToCreate'],
  ])('toasts the right message for %s and keeps the text', async (_what, error, message) => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    api.addTagToFile.mockRejectedValue(error);
    await mounted();
    await submit('thing');
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith(message));
    expect(input().value).toBe('thing');
    spy.mockRestore();
  });

  it('disables the input while a request runs and ignores a second submit', async () => {
    const pending = deferred<Row>();
    api.addTagToFile.mockReturnValue(pending.promise);
    await mounted();
    await submit('thing');
    await waitFor(() => expect(input()).toBeDisabled());
    await fireEvent.keyDown(input(), { key: 'Enter' });
    expect(api.addTagToFile).toHaveBeenCalledTimes(1);
    pending.resolve(row('n', 'thing'));
    await waitFor(() => expect(input()).toBeEnabled());
  });
});

describe('near match prompt', () => {
  beforeEach(() => library(row('q3', 'q3-earnings', 9)));

  async function prompt() {
    await mounted();
    await submit('q4-earnings');
    return await waitFor(() => {
      const panel = document.querySelector('.near-match') as HTMLElement;
      expect(panel).not.toBeNull();
      return panel;
    });
  }

  it('asks instead of applying, in a polite live region with two named buttons', async () => {
    const panel = await prompt();
    expect(panel).toHaveAttribute('role', 'status');
    expect(panel).toHaveAttribute('aria-live', 'polite');
    expect(panel).toHaveTextContent(
      'tags.nearMatch.question {"name":"q3-earnings","typed":"q4-earnings"}'
    );
    expect(
      screen.getByRole('button', { name: 'tags.nearMatch.accept {"name":"q3-earnings"}' })
    ).toBeEnabled();
    expect(
      screen.getByRole('button', { name: 'tags.nearMatch.decline {"typed":"q4-earnings"}' })
    ).toBeEnabled();
    expect(api.addTagToFile).not.toHaveBeenCalled();
  });

  it('accepting applies the existing name and announces the substitution', async () => {
    await prompt();
    api.addTagToFile.mockResolvedValue(row('q3', 'q3-earnings'));
    await fireEvent.click(screen.getByRole('button', { name: /nearMatch\.accept/ }));
    await waitFor(() => expect(api.addTagToFile).toHaveBeenCalledWith('file-1', 'q3-earnings'));
    await waitFor(() => expect(document.querySelector('.near-match')).toBeNull());
    expect(toast.info).toHaveBeenCalledWith(
      'tags.resolvedTo {"name":"q3-earnings","typed":"q4-earnings"}'
    );
  });

  it('declining applies the typed name', async () => {
    await prompt();
    await fireEvent.click(screen.getByRole('button', { name: /nearMatch\.decline/ }));
    await waitFor(() => expect(api.addTagToFile).toHaveBeenCalledWith('file-1', 'q4-earnings'));
    expect(api.addTagToFile).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(document.querySelector('.near-match')).toBeNull());
  });

  it('stays a single prompt when the same input is submitted again', async () => {
    await prompt();
    await fireEvent.keyDown(input(), { key: 'Enter' });
    expect(document.querySelectorAll('.near-match')).toHaveLength(1);
    expect(api.addTagToFile).not.toHaveBeenCalled();
  });
});

describe('removing a tag', () => {
  it('detaches by name, drops the chip and emits the remaining list', async () => {
    const tagsUpdated = vi.fn();
    await mounted({ tags: [row('a', 'alpha'), row('b', 'bravo')] }, { tagsUpdated });
    await fireEvent.click(document.querySelectorAll('.tag-remove')[0]);
    await waitFor(() => expect(api.removeTagFromFile).toHaveBeenCalledWith('file-1', 'alpha'));
    await waitFor(() => expect(tagsUpdated).toHaveBeenCalledTimes(1));
    expect(detail(tagsUpdated).tags.map((t: Row) => t.name)).toEqual(['bravo']);
    expect(document.querySelectorAll('.tags-list .tag')).toHaveLength(1);
  });

  it('keeps the tag and toasts by error kind when detaching fails', async () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    const tagsUpdated = vi.fn();
    await mounted({ tags: [row('a', 'alpha')] }, { tagsUpdated });

    for (const [error, key] of [
      [{ response: { status: 401 } }, 'tags.unauthorizedLogin'],
      [{ code: 'ERR_NETWORK' }, 'tags.networkError'],
      [{ response: { status: 500 } }, 'tags.failedToRemove'],
    ] as const) {
      api.removeTagFromFile.mockRejectedValueOnce(error);
      await fireEvent.click(document.querySelector('.tag-remove') as HTMLElement);
      await waitFor(() => expect(toast.error).toHaveBeenLastCalledWith(key));
    }
    expect(toast.error).toHaveBeenCalledTimes(3);
    expect(tagsUpdated).not.toHaveBeenCalled();
    expect(document.querySelectorAll('.tags-list .tag')).toHaveLength(1);
    spy.mockRestore();
  });

  it('clears an earlier announcement', async () => {
    api.addTagToFile.mockResolvedValue(row('a', 'interview'));
    await mounted({ tags: [] });
    await submit('Interview');
    await screen.findByRole('status');
    await fireEvent.click(document.querySelector('.tag-remove') as HTMLElement);
    await waitFor(() => expect(screen.queryByRole('status')).toBeNull());
  });
});

describe('library suggestions', () => {
  it('shows the five most used offerable tags, most used first, as buttons named by the tag', async () => {
    library(
      row('1', 'one', 1),
      row('2', 'two', 7),
      row('3', 'three', 3),
      row('4', 'four', 9),
      row('5', 'five', 5),
      row('6', 'six', 2),
      row('7', 'seven', 8)
    );
    await mounted();
    await screen.findByText('tags.suggested');
    const chips = Array.from(document.querySelectorAll<HTMLButtonElement>('.suggested-tag'));
    expect(chips.map((c) => c.textContent?.trim())).toEqual([
      'four',
      'seven',
      'two',
      'five',
      'three',
    ]);
    expect(chips[0]).toHaveAttribute('title', 'tags.addExistingTagHint {"tagName":"four"}');
  });

  it('leaves out tags already on the file, by uuid or by normalized name', async () => {
    library(
      row('1', 'kept', 9),
      row('2', 'Q3_Review', 8),
      row('3', 'other', 1),
      row('4', 'same-uuid', 7)
    );
    await mounted({ tags: [row('x', 'q3 review'), row('4', 'renamed')] });
    await screen.findByText('tags.suggested');
    const names = Array.from(document.querySelectorAll('.suggested-tag')).map(
      (c) => c.textContent?.trim()
    );
    expect(names).toEqual(['kept', 'other']);
  });

  it('leaves out a tag that an AI suggestion already offers', async () => {
    library(row('1', 'road map', 9), row('2', 'budget', 3));
    await mounted({ aiSuggestions: [{ name: 'Road-Map', confidence: 0.9 }] });
    await screen.findByText('tags.suggested');
    expect(
      Array.from(document.querySelectorAll('.suggested-tag')).map((c) => c.textContent?.trim())
    ).toEqual(['budget']);
  });

  it('shows no suggestion area when nothing is offerable', async () => {
    library(row('1', 'on-file'));
    await mounted({ tags: [row('1', 'on-file')] });
    expect(screen.queryByText('tags.suggested')).toBeNull();
    expect(screen.queryByText('tags.selectFromAllTags')).toBeNull();
  });

  it('clicking a chip adds that tag by name, emits, and does not announce', async () => {
    library(row('1', 'roadmap', 9));
    api.addTagToFile.mockResolvedValue(row('1', 'roadmap'));
    const tagsUpdated = vi.fn();
    await mounted({}, { tagsUpdated });
    await fireEvent.click(await screen.findByRole('button', { name: 'roadmap' }));
    await waitFor(() => expect(api.addTagToFile).toHaveBeenCalledWith('file-1', 'roadmap'));
    await waitFor(() => expect(tagsUpdated).toHaveBeenCalledTimes(1));
    expect(detail(tagsUpdated).tags).toEqual([row('1', 'roadmap')]);
    expect(toast.info).not.toHaveBeenCalled();
    expect(screen.queryByRole('status')).toBeNull();
    expect(screen.queryByRole('button', { name: 'roadmap' })).toBeNull();
  });

  it('does not emit twice when the server returns a tag the file already has', async () => {
    library(row('1', 'roadmap', 9));
    api.addTagToFile.mockResolvedValue(row('9', 'elsewhere'));
    const tagsUpdated = vi.fn();
    await mounted({ tags: [row('9', 'elsewhere')] }, { tagsUpdated });
    await fireEvent.click(await screen.findByRole('button', { name: 'roadmap' }));
    await waitFor(() => expect(api.addTagToFile).toHaveBeenCalled());
    await Promise.resolve();
    expect(tagsUpdated).not.toHaveBeenCalled();
  });

  it.each([
    ['401', { response: { status: 401 } }, 'tags.unauthorizedLogin'],
    ['anything else', { response: { status: 500 } }, 'tags.failedToAdd'],
  ])('toasts the right key when adding an existing tag fails with %s', async (_w, error, key) => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    library(row('1', 'roadmap', 9));
    api.addTagToFile.mockRejectedValue(error);
    await mounted();
    await fireEvent.click(await screen.findByRole('button', { name: 'roadmap' }));
    await waitFor(() => expect(toast.error).toHaveBeenCalledWith(key));
    spy.mockRestore();
  });

  it('lists every offerable tag with its count in the dropdown and adds on selection', async () => {
    library(row('1', 'roadmap', 9), row('2', 'budget', 3));
    api.addTagToFile.mockResolvedValue(row('2', 'budget'));
    const tagsUpdated = vi.fn();
    await mounted({}, { tagsUpdated });
    expect(await screen.findByText('tags.selectFromAllTags')).toBeInTheDocument();

    await fireEvent.click(screen.getByRole('button', { name: 'tags.addFromLibraryPlaceholder' }));
    const options = Array.from(document.querySelectorAll('.option-item'));
    expect(options.map((o) => o.querySelector('.option-name')?.textContent)).toEqual([
      'roadmap',
      'budget',
    ]);
    expect(options.map((o) => o.querySelector('.option-count')?.textContent)).toEqual(['9', '3']);

    await fireEvent.click(options[1].querySelector('input') as HTMLInputElement);
    await waitFor(() => expect(api.addTagToFile).toHaveBeenCalledWith('file-1', 'budget'));
    await waitFor(() => expect(tagsUpdated).toHaveBeenCalledTimes(1));
  });
});

describe('AI suggestions', () => {
  async function accept(name: string) {
    await fireEvent.click(document.querySelector('.ai-suggestions-header') as HTMLElement);
    const chips = Array.from(document.querySelectorAll('.suggestion-chip'));
    const chip = chips.find((c) => c.querySelector('.chip-name')?.textContent === name)!;
    await fireEvent.click(chip.querySelector('.chip-add') as HTMLElement);
  }

  it('offers only suggestions whose normalized name is not on the file', async () => {
    await mounted({
      tags: [row('a', 'On File')],
      aiSuggestions: [
        { name: 'on-file', confidence: 0.9 },
        { name: 'fresh', confidence: 0.8 },
      ],
    });
    await fireEvent.click(document.querySelector('.ai-suggestions-header') as HTMLElement);
    expect(Array.from(document.querySelectorAll('.chip-name')).map((c) => c.textContent)).toEqual([
      'fresh',
    ]);
  });

  it('hides the whole control when every suggestion is already applied', async () => {
    await mounted({
      tags: [row('a', 'fresh')],
      aiSuggestions: [{ name: 'Fresh', confidence: 0.8 }],
    });
    expect(document.querySelector('.ai-suggestions')).toBeNull();
  });

  it('accepting applies the name through the typed path and reports the acceptance', async () => {
    const aiSuggestionAccepted = vi.fn();
    const tagsUpdated = vi.fn();
    const suggestion = { name: 'fresh', confidence: 0.8 };
    await mounted({ aiSuggestions: [suggestion] }, { aiSuggestionAccepted, tagsUpdated });
    await accept('fresh');
    await waitFor(() => expect(api.addTagToFile).toHaveBeenCalledWith('file-1', 'fresh'));
    await waitFor(() => expect(aiSuggestionAccepted).toHaveBeenCalledTimes(1));
    expect(detail(aiSuggestionAccepted)).toEqual({ suggestion });
    expect(tagsUpdated).toHaveBeenCalledTimes(1);
  });

  it('asks about a near match instead of substituting it', async () => {
    library(row('q3', 'q3-earnings', 9));
    await mounted({ aiSuggestions: [{ name: 'q4-earnings', confidence: 0.8 }] });
    await accept('q4-earnings');
    await waitFor(() => expect(document.querySelector('.near-match')).not.toBeNull());
    expect(api.addTagToFile).not.toHaveBeenCalled();
  });
});
