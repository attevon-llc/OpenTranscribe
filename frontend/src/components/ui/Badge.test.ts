import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/svelte';
import Badge from './Badge.svelte';

describe('Badge', () => {
  it('applies the requested variant class', () => {
    const { container } = render(Badge, { props: { variant: 'success' } });
    expect(container.querySelector('.badge.badge-success')).not.toBeNull();
  });

  it('defaults to the default variant', () => {
    const { container } = render(Badge);
    expect(container.querySelector('.badge.badge-default')).not.toBeNull();
  });
});
