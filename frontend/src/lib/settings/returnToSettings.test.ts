import { beforeEach, describe, expect, it } from 'vitest';
import { get } from 'svelte/store';
import { settingsModalStore } from '$stores/settingsModalStore';
import {
  settingsReturnFromUrl,
  reopenSettingsAfterBack,
  settingsReturnQuery,
  stampSettingsReturn,
  takeHistorySettingsReturn,
} from './returnToSettings';

beforeEach(() => {
  settingsModalStore.reset();
  window.history.replaceState({}, '', '/');
  window.sessionStorage.clear();
});

describe('return to Settings', () => {
  it('reads the section back from a stamped history entry exactly once', () => {
    stampSettingsReturn('file-status');
    expect(takeHistorySettingsReturn()).toBe('file-status');
    expect(takeHistorySettingsReturn()).toBeNull();
  });

  it('keeps the router’s own history state when stamping', () => {
    window.history.replaceState({ 'sveltekit:history': 3 }, '');
    stampSettingsReturn('file-status');
    expect(window.history.state['sveltekit:history']).toBe(3);
    takeHistorySettingsReturn();
    expect(window.history.state['sveltekit:history']).toBe(3);
  });

  it('is absent on an entry nobody stamped', () => {
    expect(takeHistorySettingsReturn()).toBeNull();
  });

  it('offers the control only when the URL says the user came from Settings', () => {
    expect(settingsReturnFromUrl(new URL('http://x/file-status'))).toBeNull();
    expect(
      settingsReturnFromUrl(new URL('http://x/file-status?from=elsewhere&section=file-status'))
    ).toBeNull();
    expect(
      settingsReturnFromUrl(new URL(`http://x/file-status?${settingsReturnQuery('file-status')}`))
    ).toEqual({
      section: 'file-status',
      path: '/',
    });
  });

  it('returns to the page the modal was over, as remembered at click time', () => {
    window.history.replaceState({}, '', '/speakers?tab=a');
    stampSettingsReturn('file-status');
    const target = settingsReturnFromUrl(
      new URL('http://x/file-status?from=settings&section=file-status')
    );
    expect(target?.path).toBe('/speakers?tab=a');
  });

  it('refuses a malformed section and an off-site remembered path', () => {
    expect(settingsReturnFromUrl(new URL('http://x/p?from=settings&section=..%2Fx'))).toBeNull();
    window.sessionStorage.setItem('opentr:settingsReturnPath', '//evil.example/x');
    expect(
      settingsReturnFromUrl(new URL('http://x/p?from=settings&section=file-status'))?.path
    ).toBe('/');
  });
});

describe('reopenSettingsAfterBack', () => {
  it('opens the modal on the stamped section, once', () => {
    stampSettingsReturn('file-status');
    reopenSettingsAfterBack();
    expect(get(settingsModalStore)).toMatchObject({ isOpen: true, activeSection: 'file-status' });
    settingsModalStore.close();
    reopenSettingsAfterBack();
    expect(get(settingsModalStore).isOpen).toBe(false);
  });

  it('leaves the modal closed when the entry was never stamped', () => {
    reopenSettingsAfterBack();
    expect(get(settingsModalStore).isOpen).toBe(false);
  });
});
