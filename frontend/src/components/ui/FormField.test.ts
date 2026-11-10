/**
 * FormField: the label/control/hint/error wiring that WCAG 1.3.1 (label association), 3.3.1
 * (error identification) and WAI-ARIA `aria-describedby` / `aria-invalid` require. Each test
 * checks a relation by id, because a visible label that is not bound to its control passes any
 * snapshot and still fails a screen reader.
 */
import { readFileSync } from 'node:fs';
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/svelte';
import FormFieldHost from './FormFieldTestHost.svelte';
import FormFieldAfterHost from './FormFieldAfterTestHost.svelte';
import FormFieldExtraHost from './FormFieldExtraTestHost.svelte';

describe('FormField', () => {
  it('binds the label to the control with for/id', () => {
    render(FormFieldHost, { id: 'host-name', label: 'Host' });
    const input = screen.getByLabelText('Host');
    expect(input.id).toBe('host-name');
    expect(screen.getByText('Host').getAttribute('for')).toBe('host-name');
  });

  it('describes the control by hint and error ids that exist, and marks it invalid', () => {
    render(FormFieldHost, { id: 'f', label: 'Host', hint: 'Use the FQDN', error: 'Required' });
    const input = screen.getByLabelText('Host');
    const ids = (input.getAttribute('aria-describedby') ?? '').split(' ');
    expect(ids).toEqual(['f-hint', 'f-error']);
    expect(document.getElementById('f-hint')?.textContent).toBe('Use the FQDN');
    expect(document.getElementById('f-error')?.textContent).toBe('Required');
    expect(input.getAttribute('aria-invalid')).toBe('true');
  });

  it('has no describedby, no alert and is not invalid without hint or error', () => {
    render(FormFieldHost, { id: 'f', label: 'Host' });
    const input = screen.getByLabelText('Host');
    expect(input.hasAttribute('aria-describedby')).toBe(false);
    expect(input.getAttribute('aria-invalid')).toBe('false');
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('announces the error with role=alert only when there is one', () => {
    render(FormFieldHost, { id: 'f', label: 'Host', error: 'Required' });
    expect(screen.getByRole('alert').textContent).toBe('Required');
  });

  it('hides the required asterisk from assistive technology', () => {
    render(FormFieldHost, { id: 'f', label: 'Host', required: true });
    const star = document.querySelector('label span');
    expect(star?.getAttribute('aria-hidden')).toBe('true');
  });

  it('applies the large size class only for size=lg', () => {
    const { container, unmount } = render(FormFieldHost, { id: 'f', label: 'Host' });
    expect(container.querySelector('.form-field-lg')).toBeNull();
    unmount();
    const lg = render(FormFieldHost, { id: 'f', label: 'Host', size: 'lg' });
    expect(lg.container.querySelector('.form-field.form-field-lg')).not.toBeNull();
  });

  it('renders the label-after slot beside the label', () => {
    render(FormFieldAfterHost);
    const header = document.querySelector('.form-field-header');
    expect(header?.querySelector('label')?.getAttribute('for')).toBe('f');
    expect(header?.querySelector('button')?.textContent).toBe('toggle');
  });

  it('gives the error paragraph the form-field-error class, which the stylesheet sizes at the md token', () => {
    render(FormFieldHost, { id: 'f', label: 'Host', error: 'Required' });
    expect(screen.getByRole('alert').classList.contains('form-field-error')).toBe(true);
    const css = readFileSync('src/styles/form-elements.css', 'utf8');
    const rule = css.slice(css.indexOf(':where(.form-field-error)'));
    expect(rule.slice(0, rule.indexOf('}'))).toContain('var(--font-size-md)');
  });

  it('passes a class through to the wrapper', () => {
    const { container } = render(FormFieldExtraHost);
    expect(container.querySelector('.form-field')?.className).toContain('narrow');
  });

  it('renders the after-hint slot with an id that is part of aria-describedby', () => {
    render(FormFieldExtraHost);
    const ids = (screen.getByLabelText('Groups').getAttribute('aria-describedby') ?? '').split(' ');
    expect(ids).toEqual(['g-hint', 'g-extra']);
    expect(document.getElementById('g-extra')?.textContent?.trim()).toBe('Second line');
  });

  it('adds no extra id when the after-hint slot is not provided', () => {
    render(FormFieldHost, { id: 'f', label: 'Host', hint: 'h' });
    expect(document.getElementById('f-extra')).toBeNull();
    expect(screen.getByLabelText('Host').getAttribute('aria-describedby')).toBe('f-hint');
  });
});
