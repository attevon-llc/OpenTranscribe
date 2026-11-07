<script lang="ts">
  import { domId } from '$lib/utils/domId';
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import { formatClock, parseClock } from '$lib/utils/formatting';

  /** The comment's time in seconds. Changes leave through `change`, never by mutation. */
  export let value = 0;
  /** Live playback position, for "Use current time". */
  export let currentTime = 0;
  /** Media length in seconds when known; an entered time past it is refused. */
  export let duration: number | null = null;
  /** False while the typed text is not a usable time; the form refuses to save until fixed. */
  export let valid = true;

  const id = domId('comment-time');
  const dispatch = createEventDispatcher<{ change: number; pin: void }>();

  let input: HTMLInputElement;
  let draft = formatClock(value);
  let focused = false;
  let error: 'format' | 'range' | null = null;

  $: maxTime = duration && duration > 0 ? duration : null;
  // Follow the value (it moves with the player until pinned) except while the user is typing in
  // this box: overwriting their half-typed text every tick would make it impossible to edit.
  $: if (!focused) draft = formatClock(value);
  $: valid = error === null;
  $: errorText =
    error === 'range'
      ? $t('comments.timeOutOfRange', { max: formatClock(maxTime) })
      : $t('comments.timeInvalidFormat');

  function clamp(seconds: number): number {
    const lower = Math.max(0, seconds);
    return maxTime !== null ? Math.min(lower, maxTime) : lower;
  }

  function set(seconds: number) {
    error = null;
    draft = formatClock(seconds);
    dispatch('change', seconds);
  }

  function commit() {
    // An untouched box keeps the exact (sub-second) time it was given: re-parsing its rounded
    // label would move the comment up to a second earlier than the moment it was pinned to.
    if (draft.trim() === formatClock(value)) {
      error = null;
      return;
    }
    const parsed = parseClock(draft);
    if (parsed === null) error = 'format';
    else if (maxTime !== null && parsed > maxTime) error = 'range';
    else set(parsed);
  }

  /** Called by the form before it saves; true when the time is usable. */
  export function validate(): boolean {
    commit();
    if (error) input?.focus();
    return error === null;
  }

  function onKeydown(event: KeyboardEvent) {
    if (event.key === 'Enter') {
      event.preventDefault();
      commit();
    }
  }
</script>

<span class="time-field">
  <button
    type="button"
    class="nudge"
    aria-label={$t('comments.timeNudgeBack')}
    title={$t('comments.timeNudgeBack')}
    on:click={() => set(clamp(value - 1))}>{$t('comments.timeNudgeBackShort')}</button
  >
  <input
    bind:this={input}
    bind:value={draft}
    type="text"
    inputmode="numeric"
    dir="ltr"
    class="time-input"
    aria-label={$t('comments.timeFieldLabel')}
    aria-invalid={error !== null}
    aria-describedby={error ? `${id}-error` : undefined}
    on:focus={() => {
      focused = true;
      dispatch('pin');
    }}
    on:blur={() => {
      focused = false;
      commit();
    }}
    on:keydown={onKeydown}
  />
  <button
    type="button"
    class="nudge"
    aria-label={$t('comments.timeNudgeForward')}
    title={$t('comments.timeNudgeForward')}
    on:click={() => set(clamp(value + 1))}>{$t('comments.timeNudgeForwardShort')}</button
  >
  <button type="button" class="link-action" on:click={() => set(clamp(currentTime))}>
    {$t('comments.useCurrentTime', { time: formatClock(currentTime) })}
  </button>
</span>
{#if error}
  <p id="{id}-error" class="time-error" role="alert">{errorText}</p>
{/if}

<style>
  .time-field {
    display: inline-flex;
    align-items: center;
    flex-wrap: wrap;
    gap: var(--space-1);
  }

  .time-input {
    inline-size: 6.5em;
    padding: var(--space-0-5) var(--space-1-5);
    font-variant-numeric: tabular-nums;
    text-align: center;
  }

  .nudge {
    padding: var(--space-0-5) var(--space-1-5);
    border-radius: var(--radius-sm);
    box-shadow: none;
    font-size: var(--font-size-xs);
    font-variant-numeric: tabular-nums;
  }

  .nudge:hover {
    box-shadow: none;
  }

  .link-action {
    background: none;
    border: none;
    box-shadow: none;
    padding: 0;
    margin-inline-start: var(--space-1);
    font-size: var(--font-size-sm);
    color: var(--primary-on-surface);
    text-decoration: underline;
    cursor: pointer;
  }

  .time-error {
    flex-basis: 100%;
    margin: 0;
    font-size: var(--font-size-sm);
    color: var(--error-text);
  }
</style>
