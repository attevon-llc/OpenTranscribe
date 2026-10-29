import { describe, it, expect, vi } from 'vitest';
import { get } from 'svelte/store';
import { createConfirmDialog } from './confirmDialog';

describe('createConfirmDialog', () => {
  it('starts closed and empty', () => {
    const d = createConfirmDialog();
    expect(get(d.isOpen)).toBe(false);
    expect(get(d.title)).toBe('');
    expect(get(d.message)).toBe('');
    expect(get(d.confirmText)).toBe('');
  });

  it('request opens the dialog with its text and does not run anything yet', () => {
    const d = createConfirmDialog();
    const cb = vi.fn();
    d.request('Title', 'Message', cb, 'Do it');
    expect(get(d.isOpen)).toBe(true);
    expect(get(d.title)).toBe('Title');
    expect(get(d.message)).toBe('Message');
    expect(get(d.confirmText)).toBe('Do it');
    expect(cb).not.toHaveBeenCalled();
  });

  it('confirm closes the dialog, runs the callback once, and a second confirm is a no-op', () => {
    const d = createConfirmDialog();
    const cb = vi.fn();
    d.request('t', 'm', cb);
    d.confirm();
    expect(get(d.isOpen)).toBe(false);
    expect(cb).toHaveBeenCalledTimes(1);
    d.confirm();
    expect(cb).toHaveBeenCalledTimes(1);
  });

  it('cancel closes without running confirm, and runs onCancel exactly once', () => {
    const d = createConfirmDialog();
    const cb = vi.fn();
    const onCancel = vi.fn();
    d.request('t', 'm', cb, '', onCancel);
    d.cancel();
    expect(get(d.isOpen)).toBe(false);
    expect(cb).not.toHaveBeenCalled();
    expect(onCancel).toHaveBeenCalledTimes(1);
    d.cancel();
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it('confirm discards onCancel so a later close cannot fire it', () => {
    const d = createConfirmDialog();
    const onCancel = vi.fn();
    d.request('t', 'm', vi.fn(), '', onCancel);
    d.confirm();
    d.cancel();
    expect(onCancel).not.toHaveBeenCalled();
  });

  it('a new request replaces the previous callbacks and confirm label', () => {
    const d = createConfirmDialog();
    const first = vi.fn();
    const second = vi.fn();
    d.request('a', 'a', first, 'First');
    d.request('b', 'b', second);
    expect(get(d.confirmText)).toBe('');
    d.confirm();
    expect(first).not.toHaveBeenCalled();
    expect(second).toHaveBeenCalledTimes(1);
  });

  it('a throwing callback still leaves the dialog closed and disarmed', () => {
    const d = createConfirmDialog();
    const cb = vi.fn(() => {
      throw new Error('boom');
    });
    d.request('t', 'm', cb);
    expect(() => d.confirm()).toThrow('boom');
    expect(get(d.isOpen)).toBe(false);
    d.confirm();
    expect(cb).toHaveBeenCalledTimes(1);
  });

  it('an async callback that rejects leaves the dialog closed and disarmed', async () => {
    const d = createConfirmDialog();
    const cb = vi.fn(() => Promise.reject(new Error('async boom')));
    d.request('t', 'm', cb);
    d.confirm();
    expect(get(d.isOpen)).toBe(false);
    await expect(cb.mock.results[0].value).rejects.toThrow('async boom');
    d.confirm();
    expect(cb).toHaveBeenCalledTimes(1);
  });

  it('a callback may chain a second dialog on the same instance', () => {
    const d = createConfirmDialog();
    const second = vi.fn();
    d.request('one', 'one', () => d.request('two', 'two', second));
    d.confirm();
    expect(get(d.isOpen)).toBe(true);
    expect(get(d.title)).toBe('two');
    d.confirm();
    expect(second).toHaveBeenCalledTimes(1);
  });

  it('instances are independent', () => {
    const a = createConfirmDialog();
    const b = createConfirmDialog();
    a.request('t', 'm', vi.fn());
    expect(get(b.isOpen)).toBe(false);
  });
});
