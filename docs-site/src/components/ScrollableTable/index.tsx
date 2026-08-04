import {useCallback, useEffect, useRef, useState} from 'react';
import type {ComponentProps, ReactNode} from 'react';

import styles from './styles.module.css';

/**
 * Horizontally scrollable wrapper applied to every markdown table.
 *
 * Infima's default is `table { display: block; overflow: auto }`. That scrolls, but
 * overriding a table's display drops its implicit table semantics in several screen
 * readers, and it gives the reader no hint that content extends past the right edge.
 * Wrapping instead keeps the table a real `display: table` and lets us expose the
 * scroll region to keyboard users only when it actually scrolls.
 */
export default function ScrollableTable(props: ComponentProps<'table'>): ReactNode {
  const scrollerRef = useRef<HTMLDivElement>(null);
  const [overflow, setOverflow] = useState({left: false, right: false});

  const measure = useCallback(() => {
    const el = scrollerRef.current;
    if (!el) return;
    // 1px tolerance: sub-pixel layout rounding otherwise reports a permanent 0.5px overflow.
    setOverflow({
      left: el.scrollLeft > 1,
      right: el.scrollLeft + el.clientWidth < el.scrollWidth - 1,
    });
  }, []);

  useEffect(() => {
    const el = scrollerRef.current;
    if (!el) return undefined;
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    // The table itself changes width independently of the scroller (fonts, images).
    const table = el.querySelector('table');
    if (table) observer.observe(table);
    return () => observer.disconnect();
  }, [measure]);

  const scrollable = overflow.left || overflow.right;

  return (
    <div
      className={styles.container}
      data-overflow-left={overflow.left || undefined}
      data-overflow-right={overflow.right || undefined}>
      <div
        ref={scrollerRef}
        className={styles.scroller}
        onScroll={measure}
        // Only a genuinely scrollable region becomes a tab stop, so keyboard users can
        // reach content they'd otherwise never see without adding noise to every table.
        role={scrollable ? 'region' : undefined}
        aria-label={scrollable ? 'Scrollable table' : undefined}
        tabIndex={scrollable ? 0 : undefined}>
        <table {...props} />
      </div>
    </div>
  );
}
