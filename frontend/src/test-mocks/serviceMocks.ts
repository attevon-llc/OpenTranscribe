/**
 * Factories for the `$stores/toast` and `$lib/axios` module mocks. `vi.mock` is hoisted and must
 * stay in each test file; the factory bodies live here: `vi.mock('$stores/toast', async () =>
 * (await import('$testMocks/serviceMocks')).toastStoreMock())`.
 */
import { vi } from 'vitest';

/** A `toastStore` whose four notifiers are spies. */
export const toastStoreMock = () => ({
  toastStore: { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() },
});

/** An axios instance with the four verbs the API modules use, plus the cancellation probe. */
export const axiosInstanceMock = () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
  isRequestCancelled: () => false,
});
