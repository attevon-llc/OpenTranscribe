declare module '*.svelte' {
  import type { ComponentType } from 'svelte';
  const component: ComponentType<any>;
  export default component;
}

// Custom event types for Svelte actions
declare namespace svelteHTML {
  interface HTMLAttributes<_T> {
    'on:click_outside'?: (event: CustomEvent) => void;
  }
}
