/**
 * The empty composer is tall enough for its placeholder.
 *
 * With no LLM configured the placeholder is a sentence ("Chat needs a language model. Add a
 * provider in Settings..."), two lines in most locales. The field auto-grows from
 * `scrollHeight`, which browsers compute from the VALUE only, so the empty field stayed one
 * line tall and the notice was clipped mid-line. jsdom has no layout, so `scrollHeight` is
 * stubbed to behave like Chromium's: the height of the text actually in the field.
 *
 * Docs: MDN `Element.scrollHeight`, MDN `HTMLTextAreaElement.placeholder`.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/svelte';

vi.mock('$stores/locale', async () => (await import('$testMocks/localeMocks')).keyLocale());

import ChatComposer from './ChatComposer.svelte';

const LINE = 24;
/** Field width in px; 10px per character, so 200px holds 20 characters a line. */
let fieldWidth = 200;
let observed: (() => void) | null = null;

beforeEach(() => {
  fieldWidth = 200;
  observed = null;
  // Lines of VALUE at the current width; the placeholder never counts, as in a real browser.
  Object.defineProperty(HTMLTextAreaElement.prototype, 'scrollHeight', {
    configurable: true,
    get(this: HTMLTextAreaElement) {
      return Math.max(1, Math.ceil((this.value.length * 10) / fieldWidth)) * LINE;
    },
  });
  Object.defineProperty(HTMLTextAreaElement.prototype, 'clientWidth', {
    configurable: true,
    get: () => fieldWidth,
  });
  vi.stubGlobal(
    'ResizeObserver',
    class {
      constructor(callback: () => void) {
        observed = callback;
      }
      observe() {}
      disconnect() {}
    }
  );
});

afterEach(() => {
  delete (HTMLTextAreaElement.prototype as { scrollHeight?: number }).scrollHeight;
  delete (HTMLTextAreaElement.prototype as { clientWidth?: number }).clientWidth;
  vi.unstubAllGlobals();
});

const field = () => screen.getByTestId('chat-composer-input') as HTMLTextAreaElement;

describe('ChatComposer empty-field height', () => {
  it('grows the empty, disabled field to fit the no-provider notice', async () => {
    render(ChatComposer, { props: { value: '', disabled: true } });
    const placeholderLines = Math.ceil((field().placeholder.length * 10) / fieldWidth);
    expect(placeholderLines).toBeGreaterThan(1);
    await waitFor(() => expect(field().style.height).toBe(`${placeholderLines * LINE}px`));
    // The measurement must not leave the placeholder behind as typed text.
    expect(field().value).toBe('');
  });

  it('re-measures when the field narrows, as when the trace panel opens beside it', async () => {
    render(ChatComposer, { props: { value: '', disabled: true } });
    const lines = (width: number) => Math.ceil((field().placeholder.length * 10) / width);
    await waitFor(() => expect(field().style.height).toBe(`${lines(200) * LINE}px`));

    fieldWidth = 100;
    expect(observed).not.toBeNull();
    observed!();
    await waitFor(() => expect(field().style.height).toBe(`${lines(100) * LINE}px`));
    expect(lines(100)).toBeGreaterThan(lines(200));
  });

  it('sizes to the typed text, not the placeholder, once there is a value', async () => {
    render(ChatComposer, { props: { value: 'hi' } });
    await waitFor(() => expect(field().style.height).toBe(`${LINE}px`));
    expect(field().value).toBe('hi');
  });
});
