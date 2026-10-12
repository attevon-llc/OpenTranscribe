<!--
  IssueRow.svelte — one problem, warning or note about the processing pipeline (issue #1227).

  Shared by the Processing pipeline page and the speaker detection card so both say the same
  thing the same way. The sentence is looked up from the plan's stable code; `fallbackText` is
  the server's fixed sentence for a code this build has no copy for, never raw exception text.
  Severity is shown by a glyph and a screen-reader label as well as colour.
-->
<script lang="ts">
  import { createEventDispatcher } from 'svelte';
  import type { PipelineSeverity } from '$lib/api/pipeline';
  import { PIPELINE_SEVERITY_KEYS } from '$lib/settings/pipelineKeys';
  import { fixLinkKey } from '$lib/settings/pipelineIssues';
  import type { SettingsSection } from '$stores/settingsModalStore';
  import { t } from '$stores/locale';

  export let code: string;
  export let severity: PipelineSeverity;
  export let messageKey: string | null;
  export let fallbackText: string | null = null;
  /** Settings sections that fix it; one link each. */
  export let sections: SettingsSection[] = [];
  export let rowTestId = 'pipeline-issue';
  export let linkTestId = 'pipeline-fix-link';

  const dispatch = createEventDispatcher<{ navigate: SettingsSection }>();
  const GLYPH = { error: '✕', warning: '!', info: 'i' } as const;
</script>

<li
  class={`issue issue-${severity}`}
  data-testid={rowTestId}
  data-code={code}
  data-severity={severity}
>
  <span class="issue-glyph" aria-hidden="true">{GLYPH[severity]}</span>
  <span class="issue-text">
    <span class="sr-only">{$t(PIPELINE_SEVERITY_KEYS[severity])}</span>
    {messageKey ? $t(messageKey) : (fallbackText ?? '')}
  </span>
  {#each sections as section (section)}
    <button
      type="button"
      class="link-button"
      data-testid={linkTestId}
      data-section={section}
      on:click={() => dispatch('navigate', section)}
    >
      {$t(fixLinkKey(section))}
    </button>
  {/each}
</li>

<style>
  .issue {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 0.25rem 0.75rem;
    padding: 0.5rem 0.75rem;
    border-radius: 6px;
    border-inline-start: 3px solid;
    font-size: 0.8rem;
    line-height: 1.45;
    color: var(--text-color);
    list-style: none;
  }

  .issue-error {
    background: rgba(var(--error-color-rgb), 0.08);
    border-color: var(--error-color);
  }

  .issue-warning {
    background: rgba(var(--warning-color-rgb), 0.1);
    border-color: var(--warning-color);
  }

  .issue-info {
    background: rgba(var(--primary-color-rgb), 0.07);
    border-color: var(--primary-color);
  }

  .issue-glyph {
    flex: none;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 1.1rem;
    height: 1.1rem;
    border-radius: 50%;
    font-size: 0.7rem;
    font-weight: 700;
    color: #fff;
  }

  .issue-error .issue-glyph {
    background: var(--error-solid, var(--error-color));
  }

  .issue-warning .issue-glyph {
    background: var(--warning-color);
  }

  .issue-info .issue-glyph {
    background: var(--primary-color);
  }

  .issue-text {
    flex: 1 1 14rem;
  }

  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    margin: -1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
    border: 0;
  }

  .link-button {
    display: inline;
    padding: 0;
    border: none;
    border-radius: 0;
    box-shadow: none;
    background: none;
    color: var(--primary-on-surface);
    font: inherit;
    text-decoration: underline;
    cursor: pointer;
  }

  .link-button:hover,
  .link-button:focus {
    background: none;
    box-shadow: none;
    transform: none;
  }

  .link-button:focus-visible {
    outline: 2px solid var(--primary-color);
    outline-offset: 2px;
  }
</style>
