import js from '@eslint/js';
import tseslint from 'typescript-eslint';
import svelte from 'eslint-plugin-svelte';
import prettier from 'eslint-config-prettier';
import globals from 'globals';
import svelteConfig from './svelte.config.js';

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
    ignores: ['build/', '.svelte-kit/', 'dist/', 'node_modules/', 'static/', 'scripts/'],
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
        svelteConfig,
      },
    },
    rules: {
      // typescript-eslint's no-unused-vars crashes on svelte-eslint-parser nodes;
      // svelte's own compiler warnings cover unused props. Disable here.
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
        'warn',
        { argsIgnorePattern: '^_', varsIgnorePattern: '^_', caughtErrorsIgnorePattern: '^_' },
      ],
      '@typescript-eslint/no-unused-expressions': 'warn',
      'no-undef': 'off', // TS + svelte handle this; avoids false positives on browser/Svelte globals
      'no-case-declarations': 'warn',
      'no-empty': 'warn',
      'prefer-const': 'warn',
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
    },
  },
  {
    // Test files: allow test globals and looser typing.
    files: ['**/*.{test,spec}.{ts,js}', 'src/test-setup.ts'],
    languageOptions: { globals: { ...globals.node } },
    rules: { '@typescript-eslint/no-explicit-any': 'off' },
  },
  {
    // MUST be last: typescript-eslint's no-unused-vars crashes on svelte-eslint-parser
    // nodes. Keep it disabled for svelte files (svelte's compiler covers unused props).
    files: ['**/*.svelte', '**/*.svelte.ts', '**/*.svelte.js'],
    rules: {
      '@typescript-eslint/no-unused-vars': 'off',
      'no-unused-vars': 'off',
    },
  }
);
