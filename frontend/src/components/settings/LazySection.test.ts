import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/svelte';

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

import LazySectionTestHost from './LazySectionTestHost.svelte';
import Probe from './LazySectionProbe.svelte';

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe('LazySection', () => {
  it('shows a labelled skeleton until the chunk lands, then the panel with its props', async () => {
    const chunk = deferred<{ default: unknown }>();
    render(LazySectionTestHost, { props: { load: () => chunk.promise, label: 'x' } });

    expect(
      screen.getByRole('status', { name: 'settings.modal.sectionLoading' })
    ).toBeInTheDocument();
    expect(screen.queryByTestId('lazy-probe')).toBeNull();

    chunk.resolve({ default: Probe });
    expect(await screen.findByTestId('lazy-probe')).toHaveTextContent('probe:x');
    expect(screen.queryByTestId('settings-section-loading')).toBeNull();
  });

  it('reports a failed load and recovers on retry', async () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});
    const load = vi
      .fn()
      .mockRejectedValueOnce(new Error('chunk 404'))
      .mockResolvedValueOnce({ default: Probe });
    render(LazySectionTestHost, { props: { load, label: 'retry' } });

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('settings.modal.sectionLoadFailed');
    expect(screen.queryByTestId('lazy-probe')).toBeNull();

    await fireEvent.click(screen.getByRole('button', { name: 'settings.modal.sectionRetry' }));
    expect(await screen.findByTestId('lazy-probe')).toHaveTextContent('probe:retry');
    expect(screen.queryByRole('alert')).toBeNull();
    expect(load).toHaveBeenCalledTimes(2);
    consoleError.mockRestore();
  });

  it('drops the previous panel when switched to another loader', async () => {
    const first = deferred<{ default: unknown }>();
    const second = deferred<{ default: unknown }>();
    const view = render(LazySectionTestHost, {
      props: { load: () => first.promise, label: 'one' },
    });
    first.resolve({ default: Probe });
    expect(await screen.findByTestId('lazy-probe')).toHaveTextContent('probe:one');

    await view.rerender({ load: () => second.promise, label: 'two' });
    await waitFor(() => expect(screen.queryByTestId('lazy-probe')).toBeNull());
    expect(screen.getByTestId('settings-section-loading')).toBeInTheDocument();

    second.resolve({ default: Probe });
    expect(await screen.findByTestId('lazy-probe')).toHaveTextContent('probe:two');
  });

  it('ignores a stale chunk that resolves after the loader changed', async () => {
    const slow = deferred<{ default: unknown }>();
    const fast = deferred<{ default: unknown }>();
    const view = render(LazySectionTestHost, {
      props: { load: () => slow.promise, label: 'slow' },
    });
    await view.rerender({ load: () => fast.promise, label: 'fast' });

    fast.resolve({ default: Probe });
    expect(await screen.findByTestId('lazy-probe')).toHaveTextContent('probe:fast');
    slow.resolve({ default: Probe });
    await Promise.resolve();
    expect(screen.getByTestId('lazy-probe')).toHaveTextContent('probe:fast');
  });
});
