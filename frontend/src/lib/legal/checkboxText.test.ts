import { describe, it, expect } from 'vitest';
import { extractCheckboxText } from './checkboxText';

describe('extractCheckboxText', () => {
  it('reads the blockquote after "Checkbox text:"', () => {
    const md =
      'Body\n\n**Checkbox text:**\n\n> I have read and accept this.\n> Second line.\n\nTrailing';
    expect(extractCheckboxText(md)).toBe('I have read and accept this. Second line.');
  });

  it('strips quoting and emphasis marks', () => {
    expect(extractCheckboxText('Checkbox text: > "*I agree.*"')).toBe('I agree.');
  });

  it('returns null when the marker is absent', () => {
    expect(extractCheckboxText('# Nothing here')).toBeNull();
    expect(extractCheckboxText(null)).toBeNull();
  });
});
