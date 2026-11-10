/**
 * Factories for the modules `$stores/auth` pulls in. `vi.mock` is hoisted and stays in each test
 * file; the factory bodies live here: `vi.mock('$lib/axios', async () =>
 * (await import('$testMocks/authMocks')).authAxiosMock())`.
 */
import { vi } from 'vitest';

/**
 * The axios module as `$stores/auth` uses it. With `answering: true` GET/POST resolve `200 {}` and
 * the interceptor registry exists, for suites that run `logout()`/`login()` end to end.
 */
export const authAxiosMock = (options: { answering?: boolean } = {}) => {
  const { answering = false } = options;
  const verb = () => (answering ? vi.fn().mockResolvedValue({ status: 200, data: {} }) : vi.fn());
  return {
    default: {
      get: verb(),
      post: verb(),
      put: vi.fn(),
      ...(answering && {
        interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
      }),
    },
    abortAllRequests: vi.fn(),
  };
};

export const clearUserStateMock = () => ({ clearUserState: vi.fn() });

export const loadCapabilitiesMock = () => ({ loadCapabilities: vi.fn() });
