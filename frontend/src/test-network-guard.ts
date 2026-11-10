// Fail-fast guard: any request that reaches the real network layer in a unit test is a bug.
//
// Vitest's "Mocking requests" guide recommends intercepting at the network boundary and
// erroring on unhandled requests (MSW `onUnhandledRequest: 'error'`). We keep the same
// contract without adding a dependency: nothing in this suite stubs responses at the
// network level (tests mock `$lib/api/*` modules or `vi.stubGlobal('fetch')`), so the only
// thing the guard has to do is refuse what is left over. Tests that stub fetch/XHR replace
// the guarded functions and are unaffected.
//
// An unmocked axios call otherwise fails asynchronously with "Network Error" and, under
// load, logs AFTER the test file has torn down, which aborts the whole run with
// `EnvironmentTeardownError: Closing rpc while "onUserConsoleLog" was pending`.
// The guard throws at the call site AND records the violation so `assertNoNetworkAttempts`
// (run from an afterEach hook) fails the test even if the component swallows the error.

const attempts: string[] = [];

function describeRequest(method: string, url: string | URL): string {
  return `${method.toUpperCase()} ${String(url)}`;
}

function refuse(method: string, url: string | URL): Error {
  const what = describeRequest(method, url);
  attempts.push(what);
  return new Error(
    `Unmocked network request in a unit test: ${what}. ` +
      'Mock the module that issues it (vi.mock of the $lib/api/* module, or a helper from ' +
      'src/test-mocks/) instead of letting it reach the network.'
  );
}

export function installNetworkGuard(): void {
  if (typeof XMLHttpRequest !== 'undefined') {
    XMLHttpRequest.prototype.open = function guardedOpen(method: string, url: string | URL) {
      throw refuse(method, url);
    } as typeof XMLHttpRequest.prototype.open;
  }
  globalThis.fetch = function guardedFetch(input: RequestInfo | URL, init?: RequestInit) {
    const method = init?.method ?? (input instanceof Request ? input.method : 'GET');
    const url = input instanceof Request ? input.url : input;
    return Promise.reject(refuse(method, url));
  } as typeof fetch;
}

/** Returns and clears the requests attempted since the last call. */
export function takeNetworkAttempts(): string[] {
  return attempts.splice(0, attempts.length);
}

export function assertNoNetworkAttempts(): void {
  const made = takeNetworkAttempts();
  if (made.length > 0) {
    throw new Error(`Test reached the real network:\n  ${made.join('\n  ')}`);
  }
}
