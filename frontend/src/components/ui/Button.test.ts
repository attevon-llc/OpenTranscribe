import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/svelte';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import Button from './Button.svelte';

const css = readFileSync(resolve(__dirname, '../../styles/form-elements.css'), 'utf8');

function rule(selector: string): string {
  const escaped = selector.replace(/[.-]/g, (c) => `\\${c}`);
  const m = css.match(new RegExp(`(?:^|[\\n,]\\s*)${escaped}\\s*(?:,[^{}]*)?\\{([^}]*)\\}`));
  return m ? m[1] : '';
}

describe('Button', () => {
  afterEach(() => vi.restoreAllMocks());

  it('defaults to type=button and the secondary variant', () => {
    render(Button);
    const btn = screen.getByRole('button');
    expect(btn).toHaveAttribute('type', 'button');
    expect(btn).toHaveClass('btn', 'btn-secondary');
    expect(btn).not.toHaveClass('btn-sm');
    expect(btn).not.toHaveClass('btn-lg');
  });

  it.each([
    ['sm', 'btn-sm'],
    ['lg', 'btn-lg'],
  ] as const)('size %s adds %s', (size, cls) => {
    render(Button, { props: { size, variant: 'danger-outline' } });
    expect(screen.getByRole('button')).toHaveClass('btn-danger-outline', cls);
  });

  it('forwards rest props and honours submit', () => {
    render(Button, { props: { type: 'submit', form: 'f1', title: 'go', 'data-testid': 'b' } });
    const btn = screen.getByTestId('b');
    expect(btn).toHaveAttribute('type', 'submit');
    expect(btn).toHaveAttribute('form', 'f1');
    expect(btn).toHaveAttribute('title', 'go');
  });

  it('busy sets aria-disabled and aria-busy, keeps focus, and swallows click', async () => {
    const onClick = vi.fn();
    render(Button, { props: { busy: true }, events: { click: onClick } });
    const btn = screen.getByRole('button');
    expect(btn).toHaveAttribute('aria-disabled', 'true');
    expect(btn).toHaveAttribute('aria-busy', 'true');
    expect(btn).not.toBeDisabled();
    btn.focus();
    expect(document.activeElement).toBe(btn);
    await fireEvent.click(btn);
    expect(onClick).not.toHaveBeenCalled();
    expect(btn.querySelector('.spinner')).not.toBeNull();
  });

  it('forwards click when idle', async () => {
    const onClick = vi.fn();
    render(Button, { events: { click: onClick } });
    await fireEvent.click(screen.getByRole('button'));
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it('disabled swallows click', async () => {
    const onClick = vi.fn();
    render(Button, { props: { disabled: true }, events: { click: onClick } });
    await fireEvent.click(screen.getByRole('button'));
    expect(onClick).not.toHaveBeenCalled();
  });

  it('iconOnly adds btn-icon and uses ariaLabel as the name', () => {
    render(Button, { props: { iconOnly: true, ariaLabel: 'Close' } });
    const btn = screen.getByRole('button', { name: 'Close' });
    expect(btn).toHaveClass('btn-icon');
  });

  it('iconOnly without ariaLabel logs a console error', () => {
    const err = vi.spyOn(console, 'error').mockImplementation(() => {});
    render(Button, { props: { iconOnly: true } });
    expect(err).toHaveBeenCalledWith(expect.stringContaining('iconOnly requires ariaLabel'));
  });
});

describe('button classes in form-elements.css', () => {
  it.each([
    '.btn-sm',
    '.btn-lg',
    '.btn-success',
    '.btn-ghost',
    '.btn-link',
    '.btn-danger-outline',
    '.btn-icon',
  ])('%s is defined', (sel) => {
    expect(rule(sel)).not.toBe('');
  });

  it('small and icon buttons meet the WCAG 2.5.8 24px target', () => {
    expect(rule('.btn-sm')).toMatch(/min-height:\s*28px/);
    expect(rule('.btn-icon')).toMatch(/min-width:\s*28px/);
    expect(rule('.btn-icon')).toMatch(/min-height:\s*28px/);
  });

  it('success and danger-outline use tokens', () => {
    expect(rule('.btn-success')).toContain('var(--success-solid)');
    expect(rule('.btn-danger-outline')).toContain('var(--error-text)');
    expect(rule('.btn-danger-outline')).toContain('var(--error-border)');
    expect(rule('.btn-link')).toContain('var(--primary-on-surface)');
  });
});
