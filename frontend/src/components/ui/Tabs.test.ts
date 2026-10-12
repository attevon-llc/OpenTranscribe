import { afterEach, describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/svelte';
import Tabs from './Tabs.svelte';

const tabs = [
  { id: 'a', label: 'Alpha' },
  { id: 'b', label: 'Beta', badge: 3 },
  { id: 'c', label: 'Gamma' },
];

describe('Tabs', () => {
  it('renders tabs, marks the active one, and shows badges', () => {
    render(Tabs, { props: { tabs, activeId: 'a', ariaLabel: 'Sections' } });
    expect(screen.getByRole('tablist', { name: 'Sections' })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /Alpha/ })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByRole('tab', { name: /Beta/ })).toHaveAttribute('aria-selected', 'false');
    expect(screen.getByText('3')).toBeInTheDocument();
  });

  it('activates a tab on click', async () => {
    render(Tabs, { props: { tabs, activeId: 'a' } });
    await fireEvent.click(screen.getByRole('tab', { name: /Beta/ }));
    expect(screen.getByRole('tab', { name: /Beta/ })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByRole('tab', { name: /Alpha/ })).toHaveAttribute('aria-selected', 'false');
  });

  it('moves selection with arrow keys', async () => {
    render(Tabs, { props: { tabs, activeId: 'a' } });
    await fireEvent.keyDown(screen.getByRole('tab', { name: /Alpha/ }), { key: 'ArrowRight' });
    expect(screen.getByRole('tab', { name: /Beta/ })).toHaveAttribute('aria-selected', 'true');
  });

  describe('in a right-to-left layout', () => {
    afterEach(() => {
      document.documentElement.removeAttribute('dir');
    });

    it('mirrors ArrowLeft/ArrowRight so they follow the visual order', async () => {
      document.documentElement.setAttribute('dir', 'rtl');
      render(Tabs, { props: { tabs, activeId: 'a' } });
      await fireEvent.keyDown(screen.getByRole('tab', { name: /Alpha/ }), { key: 'ArrowLeft' });
      expect(screen.getByRole('tab', { name: /Beta/ })).toHaveAttribute('aria-selected', 'true');
      await fireEvent.keyDown(screen.getByRole('tab', { name: /Beta/ }), { key: 'ArrowRight' });
      expect(screen.getByRole('tab', { name: /Alpha/ })).toHaveAttribute('aria-selected', 'true');
    });

    it('leaves ArrowDown/ArrowUp, Home and End unmirrored', async () => {
      document.documentElement.setAttribute('dir', 'rtl');
      render(Tabs, { props: { tabs, activeId: 'a' } });
      await fireEvent.keyDown(screen.getByRole('tab', { name: /Alpha/ }), { key: 'ArrowDown' });
      expect(screen.getByRole('tab', { name: /Beta/ })).toHaveAttribute('aria-selected', 'true');
      await fireEvent.keyDown(screen.getByRole('tab', { name: /Beta/ }), { key: 'End' });
      expect(screen.getByRole('tab', { name: /Gamma/ })).toHaveAttribute('aria-selected', 'true');
    });
  });

  describe('sticky strip', () => {
    afterEach(() => {
      vi.unstubAllGlobals();
    });

    it('is not sticky unless asked, and renders no sentinel', () => {
      const { container } = render(Tabs, { props: { tabs, activeId: 'a' } });
      expect(screen.getByRole('tablist')).not.toHaveClass('sticky');
      expect(container.querySelector('.tabs-sentinel')).toBeNull();
    });

    it('pins the tablist itself when sticky, keeping roving tabindex intact', () => {
      render(Tabs, { props: { tabs, activeId: 'b', sticky: true } });
      expect(screen.getByRole('tablist')).toHaveClass('sticky');
      expect(screen.getByRole('tab', { name: /Beta/ })).toHaveAttribute('tabindex', '0');
      expect(screen.getByRole('tab', { name: /Alpha/ })).toHaveAttribute('tabindex', '-1');
    });

    it('marks the strip stuck once its sentinel scrolls out of view, and unstuck on return', async () => {
      let notify: (entries: { isIntersecting: boolean }[]) => void = () => {};
      vi.stubGlobal(
        'IntersectionObserver',
        class {
          constructor(cb: typeof notify) {
            notify = cb;
          }
          observe() {}
          disconnect() {}
        }
      );
      render(Tabs, { props: { tabs, activeId: 'a', sticky: true } });
      const list = screen.getByRole('tablist');
      expect(list).not.toHaveClass('stuck');
      notify([{ isIntersecting: false }]);
      await vi.waitFor(() => expect(list).toHaveClass('stuck'));
      notify([{ isIntersecting: true }]);
      await vi.waitFor(() => expect(list).not.toHaveClass('stuck'));
    });
  });
});
