import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/svelte';
import MenuIcon from './MenuIcon.svelte';
import { MENU_ICONS, RTL_MIRRORED, type MenuIconName } from '$lib/ui/menuIcons';

const svgOf = (container: HTMLElement) => container.querySelector('svg') as SVGSVGElement;

describe('MenuIcon', () => {
  it('draws every named glyph as decorative, currentColor SVG with at least one shape', () => {
    for (const name of Object.keys(MENU_ICONS) as MenuIconName[]) {
      const { container, unmount } = render(MenuIcon, { props: { name } });
      const svg = svgOf(container);
      expect(svg.getAttribute('aria-hidden'), name).toBe('true');
      expect(svg.getAttribute('stroke'), name).toBe('currentColor');
      expect(svg.getAttribute('data-icon'), name).toBe(name);
      expect(svg.children.length, name).toBeGreaterThan(0);
      unmount();
    }
  });

  it('takes a pixel size as a fixed box', () => {
    const svg = svgOf(render(MenuIcon, { props: { name: 'check', size: 10 } }).container);
    expect(svg.getAttribute('width')).toBe('10');
    expect(svg.getAttribute('height')).toBe('10');
    expect(svg.getAttribute('style')).toBeNull();
  });

  it('takes a size token, read from the --icon-size-* custom property with a pixel fallback', () => {
    const svg = svgOf(render(MenuIcon, { props: { name: 'check', size: 'md' } }).container);
    expect(svg.getAttribute('width')).toBe('16');
    expect(svg.getAttribute('style')).toContain('var(--icon-size-md, 16px)');
  });

  it('marks exactly the directional glyphs for RTL mirroring', () => {
    for (const name of ['chevron-right', 'chevron-down', 'arrow-right', 'check'] as const) {
      const svg = svgOf(render(MenuIcon, { props: { name } }).container);
      expect(svg.classList.contains('mirror'), name).toBe(RTL_MIRRORED.has(name));
    }
    expect(RTL_MIRRORED.has('chevron-right')).toBe(true);
    expect(RTL_MIRRORED.has('chevron-down')).toBe(false);
  });

  it('spins only when asked', () => {
    expect(
      svgOf(render(MenuIcon, { props: { name: 'status-working' } }).container).classList.contains(
        'spin'
      )
    ).toBe(false);
    expect(
      svgOf(
        render(MenuIcon, { props: { name: 'status-working', spin: true } }).container
      ).classList.contains('spin')
    ).toBe(true);
  });
});
