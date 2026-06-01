import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/svelte';
import Avatar from './Avatar.svelte';

describe('Avatar', () => {
  it('shows initials derived from a name', () => {
    render(Avatar, { props: { name: 'David Macey', email: 'd@x.com' } });
    const el = screen.getByRole('img', { name: 'David Macey' });
    expect(el).toHaveTextContent('DM');
  });

  it('renders an <img> when src is provided', () => {
    render(Avatar, { props: { src: '/a.png', name: 'David Macey' } });
    const img = screen.getByRole('img', { name: 'David Macey' });
    expect(img.tagName).toBe('IMG');
    expect(img).toHaveAttribute('src', '/a.png');
  });
});
