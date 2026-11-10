import { describe, it, expect, afterEach } from 'vitest';
import { focusOnMount } from './focusOnMount';

describe('focusOnMount action', () => {
  afterEach(() => {
    document.body.innerHTML = '';
  });

  it('moves focus to the element it is applied to', () => {
    const other = document.createElement('input');
    const target = document.createElement('input');
    document.body.append(other, target);
    other.focus();
    expect(document.activeElement).toBe(other);

    focusOnMount(target);

    expect(document.activeElement).toBe(target);
  });
});
