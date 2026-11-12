import js from '@eslint/js';
import tseslint from 'typescript-eslint';
import svelte from 'eslint-plugin-svelte';
import prettier from 'eslint-config-prettier';
import globals from 'globals';
import { kitConfig } from './kit.config.js';

/**
 * Flat ESLint config for Svelte 5 + TypeScript.
 *
 * Intentionally LENIENT at first (Phase 0.2): the goal is to wire linting into CI
 * and pre-commit without blocking the refactor. Strictness (no-explicit-any →
 * error, a11y cleanup) is ratcheted up in Phase 4.7 / Phase 6 as the codebase is
 * brought up to standard. Prettier owns all formatting (eslint-config-prettier
 * disables stylistic rules).
 */
export default tseslint.config(
  {
    ignores: [
      'build/',
      '.svelte-kit/',
      'dist/',
      // `npm run test:coverage` output (the lcov HTML report ships its own scripts).
      'coverage/',
      'node_modules/',
      'static/',
      'scripts/',
      // Generated build output, vendored third-party sources, and binary test fixtures
      // (issue #473) — not our code. test/fixtures/ specifically because ts_aac.ts is an
      // MPEG transport stream, not TypeScript — eslint's parser chokes on the extension clash.
      'ffmpeg-wasm-build/dist/',
      'ffmpeg-wasm-build/test/node_modules/',
      'ffmpeg-wasm-build/test/fixtures/',
      'ffmpeg-wasm-build/src/',
    ],
  },
  {
    // A disable comment that no longer suppresses anything is dead weight that hides the next
    // real finding on its line (https://eslint.org/docs/latest/use/configure/rules#report-unused-disable-directives).
    linterOptions: { reportUnusedDisableDirectives: 'error' },
  },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  ...svelte.configs.recommended,
  prettier,
  ...svelte.configs.prettier,
  {
    languageOptions: {
      globals: { ...globals.browser, ...globals.node },
    },
  },
  {
    files: ['**/*.svelte', '**/*.svelte.ts', '**/*.svelte.js'],
    languageOptions: {
      parserOptions: {
        parser: tseslint.parser,
        svelteConfig: kitConfig,
      },
    },
    rules: {
      // typescript-eslint's no-unused-vars crashes on svelte-eslint-parser nodes (a bare
      // `$: x = 1` is enough; still true on typescript-eslint 8.72.1 + eslint-plugin-svelte
      // 3.24.1). Unused locals in .svelte are ratcheted by scripts/guards/check_svelte_unused.py
      // (tsconfig.ratchets.json) instead. Re-test upstream before removing this block.
      '@typescript-eslint/no-unused-vars': 'off',
      'no-unused-vars': 'off',
    },
  },
  {
    // Rules that were once ratcheted legacy debt. Every judgement rule below is now an
    // error; the baseline in scripts/guards/baselines/eslint.txt holds no entry for them.
    rules: {
      '@typescript-eslint/no-explicit-any': 'error',
      '@typescript-eslint/no-unused-vars': [
        'error',
        { argsIgnorePattern: '^_', varsIgnorePattern: '^_', caughtErrorsIgnorePattern: '^_' },
      ],
      '@typescript-eslint/no-unused-expressions': 'error',
      'no-undef': 'off', // TS + svelte handle this; avoids false positives on browser/Svelte globals
      'no-case-declarations': 'warn',
      'no-empty': 'error',
      'prefer-const': 'error',
      // Added to `recommended` by the eslint 9->10 bump. It cannot see that a `$:` block re-runs,
      // so the legacy edge-detect idiom (`let seen; $: if (x !== seen) { seen = x; ... }`) reads
      // as a dead store. That bookkeeping lives in a named function called as `$: sync(x)`.
      'no-useless-assignment': 'error',
      'svelte/require-each-key': 'error',
      'svelte/no-navigation-without-resolve': 'error',
      'svelte/prefer-svelte-reactivity': 'error',
      'svelte/infinite-reactive-loop': 'error',
      'svelte/no-unused-svelte-ignore': 'error',
      'svelte/no-at-html-tags': 'error', // only SanitizedHtml, ChatMarkdown and LegalProse hold one, each with a written reason
      'svelte/no-useless-mustaches': 'error',
      'svelte/no-immutable-reactive-statements': 'error',
      'svelte/no-reactive-reassign': 'error',
      // Repo convention is the $lib/$components/$stores aliases. Assets and the build scripts
      // in frontend/scripts (imported by the src/bundleBudget*.test.ts gate tests) have no alias, and
      // neither has the project-root vite.config / kit.config.js (imported by src/viteConfig.test.ts).
      'no-restricted-imports': [
        'error',
        {
          patterns: [
            {
              group: [
                '../*',
                '!../assets',
                '!../assets/*',
                '!../scripts',
                '!../scripts/*',
                '!../vite.config',
                '!../kit.config.js',
              ],
              message:
                'Use the $lib, $components or $stores alias instead of a relative ../ import.',
            },
          ],
        },
      ],
    },
  },
  {
    // Function-size gates (code-debt plan G-b). Thresholds: complexity 15 matches the backend
    // ratchet (ESLint's own default is 20), max-depth 4 is the ESLint default, and
    // max-lines-per-function 150 counts code only. All three are errors: the baseline reached 0
    // and the fix for a finding is to extract a named function or module, not to suppress it.
    rules: {
      complexity: ['error', 15],
      'max-depth': ['error', 4],
      'max-lines-per-function': ['error', { max: 150, skipBlankLines: true, skipComments: true }],
    },
  },
  {
    // Test files: allow test globals and looser typing.
    files: ['**/*.{test,spec}.{ts,js}', 'src/test-setup.ts'],
    languageOptions: { globals: { ...globals.node } },
    rules: { '@typescript-eslint/no-explicit-any': 'off' },
  },
  {
    // MUST be last: re-asserts the disable above after the lenient block re-enables it.
    // See check_svelte_unused.py for what covers .svelte files.
    files: ['**/*.svelte', '**/*.svelte.ts', '**/*.svelte.js'],
    rules: {
      '@typescript-eslint/no-unused-vars': 'off',
      'no-unused-vars': 'off',
    },
  }
);
