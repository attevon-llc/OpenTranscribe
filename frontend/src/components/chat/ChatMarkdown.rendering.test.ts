/**
 * Assistant answer presentation: code-block chrome, lazy math, and the reading face.
 *
 * The typography checks read the component's <style> source because jsdom neither loads
 * web fonts nor applies Svelte's scoped CSS reliably — the same approach as
 * `styles/typography.test.ts`.
 */
import { describe, expect, it } from 'vitest';
import { render, waitFor } from '@testing-library/svelte';
import fs from 'fs';
import path from 'path';

import ChatMarkdown from './ChatMarkdown.svelte';

const FENCE = '```python\nprint("hi")\n```';

describe('ChatMarkdown code blocks', () => {
  it('gives every block a header with its language label and a real copy button', async () => {
    const { container } = render(ChatMarkdown, { props: { content: FENCE } });
    await waitFor(() => expect(container.querySelector('.code-header')).toBeTruthy());

    const header = container.querySelector('pre .code-header')!;
    expect(header.querySelector('.code-lang')?.textContent).toBe('python');
    const button = header.querySelector('button.code-copy') as HTMLButtonElement;
    expect(button.type).toBe('button');
    expect(button.getAttribute('aria-label')).toBeTruthy();
  });

  it('keeps the header out of the copied code', async () => {
    const { container } = render(ChatMarkdown, { props: { content: FENCE } });
    await waitFor(() => expect(container.querySelector('.code-header')).toBeTruthy());
    expect(container.querySelector('pre code')?.textContent).toBe('print("hi")\n');
  });

  it('adds exactly one header per block across re-renders', async () => {
    const { container, rerender } = render(ChatMarkdown, { props: { content: FENCE } });
    await waitFor(() => expect(container.querySelector('.code-header')).toBeTruthy());
    await rerender({ content: `${FENCE}\n\n${FENCE.replace('python', 'bash')}` });
    await waitFor(() => expect(container.querySelectorAll('pre').length).toBe(2));
    await waitFor(() => expect(container.querySelectorAll('.code-header').length).toBe(2));
    const labels = [...container.querySelectorAll('.code-lang')].map((n) => n.textContent);
    expect(labels).toEqual(['python', 'bash']);
  });

  it('omits the label, but keeps the copy button, for a block with no language', async () => {
    const { container } = render(ChatMarkdown, { props: { content: '```\nplain\n```' } });
    await waitFor(() => expect(container.querySelector('.code-header')).toBeTruthy());
    expect(container.querySelector('.code-lang')).toBeNull();
    expect(container.querySelector('.code-copy')).toBeTruthy();
  });
});

describe('ChatMarkdown math', () => {
  it('re-renders once KaTeX has loaded, without a new content change', async () => {
    const { container } = render(ChatMarkdown, { props: { content: 'so $x^2$ holds' } });
    await waitFor(() => expect(container.querySelector('math msup')?.textContent).toBe('x2'));
    // The TeX source is gone from the text (no `$`, no fallback <code>), only the formula.
    expect(container.querySelector('.chat-markdown')?.textContent?.trim()).toBe('so x2 holds');
  });
});

describe('ChatMarkdown reading typography', () => {
  const src = fs.readFileSync(path.join(__dirname, 'ChatMarkdown.svelte'), 'utf8');
  const style = src.slice(src.indexOf('<style')).replace(/\/\*[\s\S]*?\*\//g, '');
  const rootRule = /\.chat-markdown\s*\{([^}]*)\}/.exec(style)?.[1] ?? '';

  it('sets prose in the text face, not the display face (Poppins) the app chrome uses', () => {
    // Poppins is a geometric display face: at weight 400 its stroke reads close to
    // other faces' medium, which is why long answers looked bold.
    expect(rootRule).toMatch(/font-family:\s*var\(--font-family\)/);
    expect(rootRule).toMatch(/font-weight:\s*var\(--font-weight-regular\)/);
  });

  it('does not hide the copy button until hover (touch and keyboard users never see it)', () => {
    const copyRule = /\.chat-markdown :global\(\.code-copy\)\s*\{([^}]*)\}/.exec(style)?.[1];
    expect(copyRule).toBeDefined();
    expect(copyRule).not.toMatch(/opacity:\s*0\s*;/);
  });
});
