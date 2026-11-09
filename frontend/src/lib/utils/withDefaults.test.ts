import { describe, expect, it } from 'vitest';
import { withDefaults } from './withDefaults';

const DEFAULTS = { enabled: false, url: '', timeout: 30, pkce: true };

describe('withDefaults', () => {
  it('fills every missing key from the defaults', () => {
    expect(withDefaults(DEFAULTS, {})).toEqual(DEFAULTS);
  });

  it('takes a provided value, including falsy ones a || fallback would discard', () => {
    expect(withDefaults(DEFAULTS, { pkce: false, timeout: 0, url: '' })).toEqual({
      enabled: false,
      url: '',
      timeout: 0,
      pkce: false,
    });
  });

  it('treats null like undefined, as ?? does', () => {
    expect(withDefaults(DEFAULTS, { timeout: null as unknown as number }).timeout).toBe(30);
  });

  it('drops keys the defaults do not name and never mutates the defaults', () => {
    const merged = withDefaults(DEFAULTS, { secret: 'x', enabled: true } as never);
    expect(merged).not.toHaveProperty('secret');
    expect(merged.enabled).toBe(true);
    expect(DEFAULTS.enabled).toBe(false);
  });
});
