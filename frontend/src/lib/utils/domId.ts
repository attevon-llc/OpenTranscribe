let counter = 0;

/**
 * A page-unique element id, for wiring `for` / `aria-describedby` / `aria-controls` between
 * elements of one component instance that may be mounted more than once.
 */
export function domId(prefix: string): string {
  counter += 1;
  return `${prefix}-${counter}`;
}
