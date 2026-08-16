<script lang="ts">
  /**
   * A derived value, WHERE IT CAME FROM, and a way to correct it.
   *
   * Built for "derived metadata with provenance" rather than for one date field —
   * participants, topics and titles have the same shape and the next one must not need a
   * second component. Only the formatter and the editor input are date-specific, and both
   * are props.
   *
   * The reason this exists at all, in the owner's words: *"If we need UI components for
   * users to add and edit metadata, then that is required, otherwise it would be false
   * data reported."* A derived value the user cannot see the origin of, or fix, makes the
   * product answer "3 meetings in March" with confidence when the truth is 5, and gives
   * nobody a way to find out. Rendering the bare value with no badge would be exactly that
   * — so the badge is not decoration and must not be dropped to tidy the layout.
   *
   * It follows the precedent already set in this repo for LLM speaker-ID suggestions:
   * surfaced with a confidence score for manual verification, never presented as settled.
   */
  import { createEventDispatcher } from 'svelte';
  import { t } from '$stores/locale';
  import type { DerivedFieldProvenance } from '$lib/types/media';

  export let label: string;
  export let value: string | null | undefined = null;
  export let provenance: DerivedFieldProvenance | null | undefined = null;
  /** Renders the stored value for display. Injected so this stays field-agnostic. */
  export let format: (raw: string) => string = (raw) => raw;
  /** Hide the edit affordance on read-only surfaces (the gallery card). */
  export let editable = true;

  const dispatch = createEventDispatcher<{ save: { value: string | null } }>();

  let editing = false;
  let draft = '';
  let showDetails = false;

  /**
   * `null` provenance means the resolver has never run — genuinely unknown. That is a
   * different statement from `source: 'none'`, which means every source was consulted and
   * none answered. Collapsing them would make an un-swept library look identical to a
   * library of undatable recordings, and only one of those is fixable.
   */
  $: unresolved = !provenance;
  $: hasValue = Boolean(value);
  $: sourceKey = provenance?.source ?? 'unresolved';
  $: sourceLabel = $t(`provenance.source.${sourceKey}`);

  function startEditing() {
    // The <input type="date"> wants YYYY-MM-DD; the stored value is a full ISO instant.
    draft = value ? String(value).slice(0, 10) : '';
    editing = true;
  }

  function save() {
    // An empty draft CLEARS the correction rather than storing a blank. A user who set a
    // date by mistake has to be able to take it back — and the backend treats an explicit
    // null as "unlock and re-derive", not as "lock at nothing".
    dispatch('save', { value: draft ? new Date(`${draft}T00:00:00Z`).toISOString() : null });
    editing = false;
  }

  function cancel() {
    editing = false;
  }
</script>

<div class="provenance-field">
  <span class="metadata-label">{label}:</span>

  {#if editing}
    <span class="editor">
      <input type="date" bind:value={draft} aria-label={label} />
      <button type="button" class="btn btn-small btn-primary" on:click={save}>
        {$t('provenance.save')}
      </button>
      <button type="button" class="btn btn-small" on:click={cancel}>
        {$t('provenance.cancel')}
      </button>
    </span>
  {:else}
    <span class="metadata-value">
      {hasValue ? format(String(value)) : $t('provenance.noneRecorded')}

      {#if hasValue && provenance}
        <!-- The badge travels with the value, always. -->
        <button
          type="button"
          class="source-badge"
          class:manual={provenance.locked}
          class:conflict={provenance.conflict}
          title={$t('provenance.sourceTooltip', { source: sourceLabel })}
          on:click={() => (showDetails = !showDetails)}
        >
          {sourceLabel}
          {#if provenance.conflict}<span aria-hidden="true">⚠</span>{/if}
        </button>
      {:else if unresolved}
        <span class="source-badge unresolved">{$t('provenance.source.unresolved')}</span>
      {/if}

      {#if editable}
        <button type="button" class="edit-link" on:click={startEditing}>
          {hasValue ? $t('provenance.edit') : $t('provenance.add')}
        </button>
      {/if}
    </span>

    {#if showDetails && provenance}
      <div class="details">
        {#if provenance.conflict}
          <!-- Sources legitimately disagree — a recording made on the 14th about the
               15th's meeting is ordinary. So this asks rather than announcing an error. -->
          <p class="conflict-note">{$t('provenance.conflictNote')}</p>
        {/if}
        <ul>
          {#each provenance.candidates as candidate (candidate.source + (candidate.evidence ?? ''))}
            <li>
              <strong>{$t(`provenance.source.${candidate.source}`)}</strong>
              {#if candidate.date}— {format(candidate.date)}{/if}
              {#if candidate.evidence}<em>{candidate.evidence}</em>{/if}
            </li>
          {/each}
        </ul>
      </div>
    {/if}
  {/if}
</div>

<style>
  .provenance-field {
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
  }

  .editor {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    flex-wrap: wrap;
  }

  .source-badge {
    margin-left: 0.4rem;
    padding: 0.05rem 0.4rem;
    border: 1px solid var(--border-color);
    border-radius: 0.75rem;
    background: var(--surface-color);
    color: var(--text-secondary);
    font-size: 0.72rem;
    cursor: pointer;
  }

  /* A hand-entered value is the one fact here nobody derived, so it reads differently
     from an inference rather than sharing its styling. */
  .source-badge.manual {
    border-color: var(--success-color, #2e7d32);
    color: var(--success-color, #2e7d32);
  }

  .source-badge.conflict {
    border-color: var(--warning-color, #b26a00);
    color: var(--warning-color, #b26a00);
  }

  .source-badge.unresolved {
    cursor: default;
    font-style: italic;
  }

  .edit-link {
    margin-left: 0.4rem;
    border: none;
    background: none;
    padding: 0;
    color: var(--primary-color);
    font-size: 0.78rem;
    cursor: pointer;
    text-decoration: underline;
  }

  .details {
    border-left: 2px solid var(--border-color);
    margin-left: 0.2rem;
    padding-left: 0.6rem;
    color: var(--text-secondary);
    font-size: 0.78rem;
  }

  .details ul {
    margin: 0.2rem 0;
    padding-left: 1rem;
  }

  .conflict-note {
    margin: 0.2rem 0;
    color: var(--warning-color, #b26a00);
  }
</style>
