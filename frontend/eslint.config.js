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
      // (tsconfig.unused.json) instead. Re-test upstream before removing this block.
      '@typescript-eslint/no-unused-vars': 'off',
      'no-unused-vars': 'off',
    },
  },
  {
    // Lenient baseline — downgrade pre-existing legacy-debt rules to warnings so the
    // gate is green today; specific rules are ratcheted back to 'error' as the debt
    // is cleared (Phase 6 keys each → require-each-key; Phase 4.7 → no-explicit-any).
    rules: {
      '@typescript-eslint/no-explicit-any': 'warn',
      '@typescript-eslint/no-unused-vars': [
        'error',
        { argsIgnorePattern: '^_', varsIgnorePattern: '^_', caughtErrorsIgnorePattern: '^_' },
      ],
      '@typescript-eslint/no-unused-expressions': 'error',
      'no-undef': 'off', // TS + svelte handle this; avoids false positives on browser/Svelte globals
      'no-case-declarations': 'warn',
      'no-empty': 'warn',
      'prefer-const': 'warn',
      // New in this codebase's eslint.config.js as of the eslint 9->10 bump (@eslint/js
      // 10 added it to `recommended`). False-positives across ~25 files on the standard
      // `let x = initial; $: x = ...` idiom: the declaration's initial value looks
      // "unused" to a plain-JS analyzer, but Svelte's reactive statement doesn't run
      // synchronously right after it — it's a separate reactive effect, not the next
      // line of imperative code. Same class of gap as the svelte/* rules below.
      'no-useless-assignment': 'warn',
      // Svelte legacy-debt → warn for now (real, but pre-existing; fixed in later phases).
      'svelte/require-each-key': 'warn', // Phase 6.2 adds keys
      'svelte/no-navigation-without-resolve': 'warn',
      'svelte/prefer-svelte-reactivity': 'warn',
      'svelte/infinite-reactive-loop': 'warn',
      'svelte/no-unused-svelte-ignore': 'warn',
      'svelte/no-at-html-tags': 'warn', // all {@html} sites audited as DOMPurify-sanitized
      'svelte/no-useless-mustaches': 'warn',
      'svelte/no-immutable-reactive-statements': 'warn',
      'svelte/no-reactive-reassign': 'warn',
      // Repo convention is the $lib/$components/$stores aliases. Assets and the build scripts
      // in frontend/scripts (imported by the src/bundleBudget*.test.ts gate tests) have no alias, and
      // neither has the project-root vite.config / kit.config.js (imported by src/viteConfig.test.ts).
      'no-restricted-imports': [
        'warn',
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
    // max-lines-per-function 150 counts code only. `warn` + scripts/guards/check_eslint_ratchet.py
    // means no new finding can appear; each rule flips to 'error' when its count reaches 0.
    rules: {
      complexity: ['warn', 15],
      'max-depth': ['warn', 4],
      'max-lines-per-function': ['warn', { max: 150, skipBlankLines: true, skipComments: true }],
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
