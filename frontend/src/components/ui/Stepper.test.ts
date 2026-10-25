import { describe, it, expect } from 'vitest';
import { render, screen, within } from '@testing-library/svelte';
import Stepper from './Stepper.svelte';

const steps = [
  { id: 'terms', label: 'Terms', done: true },
  { id: 'password', label: 'Password', done: true },
  { id: 'posture', label: 'Deployment type', done: false },
  { id: 'summary', label: 'Summary', done: false },
];

function renderAt(current: number) {
  render(Stepper, { steps, current, label: 'Setup progress', doneLabel: 'done' });
  return within(screen.getByRole('list', { name: 'Setup progress' })).getAllByRole('listitem');
}

describe('Stepper', () => {
  it('marks exactly one step as the current step', () => {
    const items = renderAt(2);
    expect(items).toHaveLength(4);
    expect(items.map((li) => li.getAttribute('aria-current'))).toEqual([null, null, 'step', null]);
  });

  it('gives every step one of complete / current / upcoming', () => {
    const items = renderAt(2);
    expect(items.map((li) => li.dataset.state)).toEqual([
      'complete',
      'complete',
      'current',
      'upcoming',
    ]);
  });

  it('the current step wins over done, so returning to a finished step still shows where you are', () => {
    const items = renderAt(1);
    expect(items[1].dataset.state).toBe('current');
    expect(items[1].getAttribute('aria-current')).toBe('step');
  });

  it('draws the done mark as an icon, not a text glyph, and announces it', () => {
    const items = renderAt(2);
    const doneMark = items[0].querySelector('.stepper-dot');
    expect(doneMark?.querySelector('svg')).not.toBeNull();
    expect(items[0].textContent).not.toContain('✓');
    expect(items[0].textContent).toContain('done');
  });

  it('numbers upcoming and current steps and labels every step', () => {
    const items = renderAt(2);
    expect(items[2].querySelector('.stepper-dot')?.textContent?.trim()).toBe('3');
    expect(items[3].querySelector('.stepper-dot')?.textContent?.trim()).toBe('4');
    expect(items[3].querySelector('svg')).toBeNull();
    steps.forEach((step, i) => expect(items[i].textContent).toContain(step.label));
  });
});
