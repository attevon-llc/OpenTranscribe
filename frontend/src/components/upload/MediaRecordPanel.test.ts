/**
 * "Recording is not supported in this browser" used to be shown for every browser that
 * lacked `navigator.mediaDevices`. On a LAN address served over plain HTTP the browser is
 * fine and the ADDRESS is the problem (an insecure context hides `mediaDevices`), so the
 * panel now names the real cause there (#1239).
 */
import { afterEach, describe, it, expect, vi } from 'vitest';
import { render } from '@testing-library/svelte';

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

import MediaRecordPanel from './MediaRecordPanel.svelte';

const original = Object.getOwnPropertyDescriptor(window, 'isSecureContext');

function setSecureContext(value: boolean) {
  Object.defineProperty(window, 'isSecureContext', { value, configurable: true });
}

afterEach(() => {
  if (original) {
    Object.defineProperty(window, 'isSecureContext', original);
  } else {
    Reflect.deleteProperty(window, 'isSecureContext');
  }
});

describe('recording unsupported message', () => {
  it('tells the user to use HTTPS when the page is not a secure context', () => {
    setSecureContext(false);
    const { getByTestId } = render(MediaRecordPanel, { props: { recordingSupported: false } });

    expect(getByTestId('recording-unsupported').textContent).toContain(
      'uploader.recordingRequiresHttps'
    );
    expect(getByTestId('recording-unsupported').textContent).not.toContain(
      'uploader.recordingNotSupported'
    );
  });

  it('keeps the browser message when the context is secure but recording is unavailable', () => {
    setSecureContext(true);
    const { getByTestId } = render(MediaRecordPanel, { props: { recordingSupported: false } });

    expect(getByTestId('recording-unsupported').textContent).toContain(
      'uploader.recordingNotSupported'
    );
    expect(getByTestId('recording-unsupported').textContent).not.toContain(
      'uploader.recordingRequiresHttps'
    );
  });

  it('shows neither message when recording is supported, even on an insecure page', () => {
    setSecureContext(false);
    const { queryByTestId } = render(MediaRecordPanel, { props: { recordingSupported: true } });

    expect(queryByTestId('recording-unsupported')).toBeNull();
  });
});
