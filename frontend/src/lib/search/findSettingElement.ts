/**
 * Locate the control a settings-search result points at inside an open section, so the modal
 * can scroll to and flash it. Tiered: labels and headings first, then help text, then any
 * element, so a match in a field's own label wins over the same words in a paragraph.
 */
export function findSettingElement(container: Element, needle: string): HTMLElement | null {
  const target = needle.trim().toLowerCase();
  if (!target) return null;
  const tiers = [
    'label, .section-title, .section-header, h3, h4, h5, legend',
    '.form-text, .field-hint, .help-text, .field-desc, .input-hint, small, p',
    '*',
  ];
  for (const selector of tiers) {
    for (const el of Array.from(container.querySelectorAll(selector))) {
      const text = (el.textContent || '').trim().toLowerCase();
      if (text && text.includes(target)) return el as HTMLElement;
    }
  }
  return null;
}
