import { describe, it, expect } from 'vitest';
import { findSettingElement } from './findSettingElement';

function section(html: string): HTMLElement {
  const root = document.createElement('div');
  root.innerHTML = html;
  return root;
}

describe('findSettingElement', () => {
  it("prefers a field's label over the same words in help text", () => {
    const root = section('<p>Token limits apply here</p><label>Token limits</label>');
    expect(findSettingElement(root, 'token LIMITS')?.tagName).toBe('LABEL');
  });

  it('falls back to help text, and finds nothing for a blank or absent needle', () => {
    const root = section('<div><p class="help-text">Resets at midnight UTC</p></div>');
    expect(findSettingElement(root, 'midnight')?.tagName).toBe('P');
    expect(findSettingElement(root, '   ')).toBeNull();
    expect(findSettingElement(root, 'nowhere')).toBeNull();
  });
});
