/**
 * A BaseModal's body always scrolls, so its footer is always reachable.
 *
 * The support-access break-glass dialog opted into `allowOverflow` (`overflow: visible` on the
 * body, for an in-body combobox). A flex item that is not a scroll container keeps
 * `min-height: auto`, i.e. its content height, so the card grew past `max-height: 90vh`
 * (scrollHeight 895 in an 800px viewport), the backdrop clips, and the ticket field and the
 * Cancel/Review footer were unreachable. Dropdowns are portalled by anchoredPanel now, so the
 * opt-out is gone; this pins that nothing brings it back, in BaseModal or in a consumer's
 * `:global(.modal-body)` override. jsdom computes no layout; the measured check is a browser pass
 * over the break-glass dialog.
 *
 * Docs: CSS Flexbox Level 1 §4.5 "Automatic Minimum Size of Flex Items" (auto is 0 for a scroll
 * container), MDN `overflow`.
 */
import fs from 'fs';
import { describe, it, expect } from 'vitest';
import {
  SRC,
  rel,
  walkSource,
  styleOf,
  atEveryWidth,
  cssRules,
  declarations,
} from '$styles/sourceTree';

const baseModal = styleOf(fs.readFileSync(`${SRC}/components/ui/BaseModal.svelte`, 'utf8'));

/** Rules that target a modal body and make it stop scrolling. */
function unscrolledBodies(files: Record<string, string>): string[] {
  const out: string[] = [];
  for (const [name, source] of Object.entries(files)) {
    for (const rule of cssRules(styleOf(source))) {
      if (
        /modal-body/.test(rule.selector) &&
        /(?:^|;)\s*overflow(?:-y)?:\s*visible/.test(rule.body)
      ) {
        out.push(`${name} ${rule.selector}`);
      }
    }
  }
  return out;
}

describe('BaseModal body scrolling', () => {
  it('makes the body the scroll container at every width', () => {
    const body = declarations(atEveryWidth(baseModal), '.modal-body');
    expect(body['overflow-y']).toBe('auto');
    expect(body['min-height']).toBe('0');
  });

  it('has no rule anywhere that turns a modal body back into overflow: visible', () => {
    const tree = Object.fromEntries(
      walkSource(SRC, /\.(svelte|css)$/).map((f) => [rel(f), fs.readFileSync(f, 'utf8')])
    );
    expect(unscrolledBodies(tree)).toEqual([]);
  });

  it('flags the removed opt-out shape (must fire)', () => {
    expect(
      unscrolledBodies({
        'x.svelte': '<style>.modal-body.allow-overflow { overflow: visible; }</style>',
      })
    ).toEqual(['x.svelte .modal-body.allow-overflow']);
  });
});
