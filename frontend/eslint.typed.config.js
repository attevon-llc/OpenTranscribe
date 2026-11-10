import tseslint from 'typescript-eslint';
import base from './eslint.config.js';

/**
 * Type-aware ESLint (`npm run lint:types`): eslint.config.js plus the typescript-eslint rules
 * that need the TypeScript program (https://typescript-eslint.io/getting-started/typed-linting/,
 * `parserOptions.projectService`; .svelte via eslint-plugin-svelte's documented
 * `extraFileExtensions` setup, https://sveltejs.github.io/eslint-plugin-svelte/user-guide/).
 *
 * A separate config because building the program costs minutes, not seconds (measured
 * 2026-11-10 on this 48-core host under load ~25: 45 s for `npm run lint`, about 4 min with
 * these rules). The commit-stage `npm run lint` and editors stay fast; this runs at the push
 * stage and in CI (the `eslint-typed` pre-commit hook).
 *
 * Every rule is an error. The bug class they catch is a promise nobody handles: a rejected
 * load that leaves a spinner up, or an async handler passed where a void callback is expected.
 * `void promise` marks a deliberate fire-and-forget and is only correct when the callee
 * handles its own errors (a try/catch that does not rethrow); otherwise attach a `.catch`.
 */
export default tseslint.config(
  ...base,
  {
    files: ['src/**/*.{ts,js,svelte}'],
    languageOptions: {
      parserOptions: {
        projectService: true,
        extraFileExtensions: ['.svelte'],
        tsconfigRootDir: import.meta.dirname,
      },
    },
    rules: {
      '@typescript-eslint/no-floating-promises': 'error',
      '@typescript-eslint/no-misused-promises': 'error',
      '@typescript-eslint/await-thenable': 'error',
      '@typescript-eslint/require-await': 'error',
      // A `default:` branch is how this codebase says "every other value": a switch with one is
      // exhaustive by decision, and a new union member without a default still fails the build.
      '@typescript-eslint/switch-exhaustiveness-check': [
        'error',
        { considerDefaultExhaustiveForUnions: true },
      ],
    },
  },
  {
    // `async () => value` is how a test or a src/test-mocks stub returns a promise from a mock
    // (`mockImplementation`, `vi.fn`): it has nothing to await by design, and a throw inside must
    // stay a rejection. no-floating-promises stays on here and catches a missing `await`.
    files: ['src/**/*.{test,spec}.{ts,js}', 'src/test-mocks/**'],
    rules: { '@typescript-eslint/require-await': 'off' },
  }
);
