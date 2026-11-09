import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/svelte';

vi.mock('$stores/locale', async () =>
  (await import('$testMocks/localeMocks')).keyWithOptionsLocale()
);

const notify = vi.hoisted(() => ({
  show: vi.fn(),
  success: vi.fn(),
  error: vi.fn(),
  warning: vi.fn(),
  info: vi.fn(),
  dismiss: vi.fn(),
  clear: vi.fn(),
}));
vi.mock('$stores/toast', () => ({ toastStore: notify }));

const tagsApi = vi.hoisted(() => ({
  listTags: vi.fn(),
  addTagToFile: vi.fn(),
  removeTagFromFile: vi.fn(),
}));
vi.mock('$lib/api/tags', () => tagsApi);

import TagsEditor from './TagsEditor.svelte';

const FILE = 'file-77';

function tag(uuid: string, name: string, extra: Record<string, unknown> = {}) {
  return { uuid, name, source: 'manual', usage_count: 1, can_write: true, ...extra };
}

let quiet: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  vi.clearAllMocks();
  quiet = vi.spyOn(console, 'error').mockImplementation(() => undefined);
  tagsApi.listTags.mockResolvedValue([tag('lib-1', 'quarterly-review', { usage_count: 6 })]);
  tagsApi.addTagToFile.mockImplementation(async (_file: string, name: string) =>
    tag(`made-${name}`, name)
  );
  tagsApi.removeTagFromFile.mockResolvedValue(undefined);
});

afterEach(() => {
  quiet.mockRestore();
});

async function editor(
  props: Record<string, unknown> = {},
  events: Record<string, (event: CustomEvent) => void> = {}
) {
  const view = render(TagsEditor, {
    props: { fileId: FILE, tags: [], aiSuggestions: [], ...props },
    events,
  });
  await waitFor(() => expect(tagsApi.listTags).toHaveBeenCalledTimes(1));
  await Promise.resolve();
  return view;
}

const field = () => document.querySelector('.tag-input') as HTMLInputElement;
const nearMatch = () => document.querySelector('.near-match');

async function typeAndEnter(value: string) {
  await fireEvent.input(field(), { target: { value } });
  await fireEvent.keyDown(field(), { key: 'Enter' });
}

async function acceptSuggestion(name: string) {
  await fireEvent.click(document.querySelector('.ai-suggestions-header') as HTMLElement);
  const chip = Array.from(document.querySelectorAll('.suggestion-chip')).find(
    (candidate) => candidate.querySelector('.chip-name')?.textContent === name
  ) as HTMLElement;
  await fireEvent.click(chip.querySelector('.chip-add') as HTMLElement);
}

describe('chip remove buttons', () => {
  it('are named after the tag they remove, with the glyph hidden from assistive tech', async () => {
    await editor({ tags: [tag('t1', 'Budget'), tag('t2', 'Q3 plan', { source: 'auto_ai' })] });
    const buttons = Array.from(document.querySelectorAll('.tag-remove'));
    expect(buttons.map((b) => b.getAttribute('aria-label'))).toEqual([
      'gallery.bulk.tagModal.removeChip {"name":"Budget"}',
      'gallery.bulk.tagModal.removeChip {"name":"Q3 plan"}',
    ]);
    expect(buttons.map((b) => b.getAttribute('type'))).toEqual(['button', 'button']);
    expect(buttons[0]).toHaveAttribute('title', 'tags.removeTag');
    expect(buttons[0].querySelector('[aria-hidden="true"]')?.textContent).toBe('×');
    expect(
      screen.getByRole('button', { name: 'gallery.bulk.tagModal.removeChip {"name":"Q3 plan"}' })
    ).toBe(buttons[1]);
  });

  it('removing a tag also withdraws a pending near-match question', async () => {
    await editor({ tags: [tag('t1', 'Budget')] });
    await typeAndEnter('quarterly-reviews');
    await waitFor(() => expect(nearMatch()).not.toBeNull());

    await fireEvent.click(
      screen.getByRole('button', { name: 'gallery.bulk.tagModal.removeChip {"name":"Budget"}' })
    );
    await waitFor(() => expect(tagsApi.removeTagFromFile).toHaveBeenCalledWith(FILE, 'Budget'));
    expect(nearMatch()).toBeNull();
  });
});

describe('near match', () => {
  it('asks only when the name is not a normalized-exact hit and is close to one', async () => {
    await editor();
    await typeAndEnter('Quarterly Reviews');
    await waitFor(() => expect(nearMatch()).not.toBeNull());
    expect(nearMatch()).toHaveTextContent(
      'tags.nearMatch.question {"name":"quarterly-review","typed":"Quarterly Reviews"}'
    );
    expect(tagsApi.addTagToFile).not.toHaveBeenCalled();
  });

  it('applies a clearly different name straight away', async () => {
    await editor();
    await typeAndEnter('hiring');
    await waitFor(() => expect(tagsApi.addTagToFile).toHaveBeenCalledWith(FILE, 'hiring'));
    expect(nearMatch()).toBeNull();
  });

  it('keeps the question open when the chosen apply fails', async () => {
    await editor();
    await typeAndEnter('quarterly-reviews');
    await waitFor(() => expect(nearMatch()).not.toBeNull());
    tagsApi.addTagToFile.mockRejectedValue({ response: { status: 500, data: {} } });

    await fireEvent.click(screen.getByRole('button', { name: /nearMatch\.accept/ }));
    await waitFor(() => expect(notify.error).toHaveBeenCalledWith('tags.failedToCreate'));
    expect(nearMatch()).not.toBeNull();
  });

  it('disables both answers while the chosen apply runs', async () => {
    let finish!: (value: unknown) => void;
    await editor();
    await typeAndEnter('quarterly-reviews');
    await waitFor(() => expect(nearMatch()).not.toBeNull());
    tagsApi.addTagToFile.mockReturnValue(new Promise((resolve) => (finish = resolve)));

    await fireEvent.click(screen.getByRole('button', { name: /nearMatch\.decline/ }));
    expect(screen.getByRole('button', { name: /nearMatch\.accept/ })).toBeDisabled();
    expect(screen.getByRole('button', { name: /nearMatch\.decline/ })).toBeDisabled();
    finish(tag('n', 'quarterly-reviews'));
    await waitFor(() => expect(nearMatch()).toBeNull());
  });
});

describe('aiSuggestionAccepted fires only once the tag is on the file', () => {
  it('waits for the near-match answer, then reports the suggestion after accept', async () => {
    const aiSuggestionAccepted = vi.fn();
    const suggestion = { name: 'quarterly reviews', confidence: 0.7 };
    await editor({ aiSuggestions: [suggestion] }, { aiSuggestionAccepted });
    await acceptSuggestion('quarterly reviews');
    await waitFor(() => expect(nearMatch()).not.toBeNull());
    expect(aiSuggestionAccepted).not.toHaveBeenCalled();

    tagsApi.addTagToFile.mockResolvedValue(tag('lib-1', 'quarterly-review'));
    await fireEvent.click(screen.getByRole('button', { name: /nearMatch\.accept/ }));
    await waitFor(() => expect(aiSuggestionAccepted).toHaveBeenCalledTimes(1));
    expect(aiSuggestionAccepted.mock.calls[0][0].detail).toEqual({ suggestion });
    expect(tagsApi.addTagToFile).toHaveBeenCalledWith(FILE, 'quarterly-review');
  });

  it('reports the suggestion after decline too', async () => {
    const aiSuggestionAccepted = vi.fn();
    const suggestion = { name: 'quarterly reviews', confidence: 0.7 };
    await editor({ aiSuggestions: [suggestion] }, { aiSuggestionAccepted });
    await acceptSuggestion('quarterly reviews');
    await waitFor(() => expect(nearMatch()).not.toBeNull());
    await fireEvent.click(screen.getByRole('button', { name: /nearMatch\.decline/ }));
    await waitFor(() => expect(aiSuggestionAccepted).toHaveBeenCalledTimes(1));
    expect(tagsApi.addTagToFile).toHaveBeenCalledWith(FILE, 'quarterly reviews');
  });

  it('stays silent when the near-match apply fails', async () => {
    const aiSuggestionAccepted = vi.fn();
    await editor(
      { aiSuggestions: [{ name: 'quarterly reviews', confidence: 0.7 }] },
      {
        aiSuggestionAccepted,
      }
    );
    await acceptSuggestion('quarterly reviews');
    await waitFor(() => expect(nearMatch()).not.toBeNull());
    tagsApi.addTagToFile.mockRejectedValue({ response: { status: 403, data: { detail: 'No' } } });
    await fireEvent.click(screen.getByRole('button', { name: /nearMatch\.decline/ }));
    await waitFor(() => expect(notify.error).toHaveBeenCalledTimes(1));
    expect(aiSuggestionAccepted).not.toHaveBeenCalled();
  });

  it('stays silent when the direct apply fails', async () => {
    const aiSuggestionAccepted = vi.fn();
    tagsApi.addTagToFile.mockRejectedValue({ response: { status: 500, data: {} } });
    await editor(
      { aiSuggestions: [{ name: 'hiring', confidence: 0.9 }] },
      { aiSuggestionAccepted }
    );
    await acceptSuggestion('hiring');
    await waitFor(() => expect(notify.error).toHaveBeenCalledWith('tags.failedToCreate'));
    expect(aiSuggestionAccepted).not.toHaveBeenCalled();
  });

  it('stays silent for a suggestion whose name is only separators', async () => {
    const aiSuggestionAccepted = vi.fn();
    await editor({ aiSuggestions: [{ name: '--', confidence: 0.9 }] }, { aiSuggestionAccepted });
    await acceptSuggestion('--');
    await waitFor(() => expect(notify.error).toHaveBeenCalledWith('tags.nameRequired'));
    expect(tagsApi.addTagToFile).not.toHaveBeenCalled();
    expect(aiSuggestionAccepted).not.toHaveBeenCalled();
  });

  it('reports a suggestion that resolves to a tag already on the file', async () => {
    const aiSuggestionAccepted = vi.fn();
    const tagsUpdated = vi.fn();
    tagsApi.addTagToFile.mockResolvedValue(tag('t1', 'Hiring'));
    await editor(
      { tags: [tag('t1', 'Hiring')], aiSuggestions: [{ name: 'new hires', confidence: 0.6 }] },
      { aiSuggestionAccepted, tagsUpdated }
    );
    await acceptSuggestion('new hires');
    await waitFor(() => expect(aiSuggestionAccepted).toHaveBeenCalledTimes(1));
    expect(tagsUpdated).not.toHaveBeenCalled();
    expect(notify.info).toHaveBeenCalledWith('tags.alreadyOnFile {"name":"Hiring"}');
  });
});

describe('structured error detail', () => {
  it('renders a 422 validation list as its messages, not as an object', async () => {
    tagsApi.addTagToFile.mockRejectedValue({
      response: {
        status: 422,
        data: {
          detail: [
            { loc: ['body', 'name'], msg: 'String should have at most 50 characters' },
            { loc: ['body', 'name'], msg: 'Value error' },
          ],
        },
      },
    });
    await editor();
    await typeAndEnter('hiring');
    await waitFor(() => expect(notify.error).toHaveBeenCalledTimes(1));
    const shown = notify.error.mock.calls[0][0] as string;
    expect(shown).toBe(
      'tags.failedToCreateWithDetail {"detail":"String should have at most 50 characters. Value error"}'
    );
    expect(shown).not.toContain('[object Object]');
  });

  it('renders an object detail by its message', async () => {
    tagsApi.addTagToFile.mockRejectedValue({
      response: { status: 409, data: { detail: { code: 'x', message: 'Tag is locked' } } },
    });
    await editor();
    await typeAndEnter('hiring');
    await waitFor(() =>
      expect(notify.error).toHaveBeenCalledWith(
        'tags.failedToCreateWithDetail {"detail":"Tag is locked"}'
      )
    );
  });

  it("never shows axios' own message when the server sent no detail", async () => {
    tagsApi.addTagToFile.mockRejectedValue(
      Object.assign(new Error('Request failed with status code 500'), {
        response: { status: 500, data: {} },
      })
    );
    await editor();
    await typeAndEnter('hiring');
    await waitFor(() => expect(notify.error).toHaveBeenCalledWith('tags.failedToCreate'));
  });

  it('uses the generic key when the detail list holds no message', async () => {
    tagsApi.addTagToFile.mockRejectedValue({
      response: { status: 422, data: { detail: [{ loc: ['body'] }] } },
    });
    await editor();
    await typeAndEnter('hiring');
    await waitFor(() => expect(notify.error).toHaveBeenCalledWith('tags.failedToCreate'));
  });

  it('gives 401 the login message even when a detail is present', async () => {
    tagsApi.addTagToFile.mockRejectedValue({
      response: { status: 401, data: { detail: 'Not authenticated' } },
    });
    await editor();
    await typeAndEnter('hiring');
    await waitFor(() => expect(notify.error).toHaveBeenCalledWith('tags.unauthorizedLogin'));
  });
});

describe('a tag the server created is remembered', () => {
  it('offers the new tag as a suggestion once it is removed from the file again', async () => {
    tagsApi.listTags.mockResolvedValue([]);
    await editor();
    await typeAndEnter('offsite');
    await waitFor(() => expect(document.querySelectorAll('.tag-remove')).toHaveLength(1));
    expect(screen.queryByRole('button', { name: 'offsite' })).toBeNull();

    await fireEvent.click(
      screen.getByRole('button', { name: 'gallery.bulk.tagModal.removeChip {"name":"offsite"}' })
    );
    expect(await screen.findByRole('button', { name: 'offsite' })).toHaveAttribute(
      'title',
      'tags.addExistingTagHint {"tagName":"offsite"}'
    );
  });
});
