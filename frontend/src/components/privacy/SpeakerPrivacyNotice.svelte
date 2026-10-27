<!--
  SpeakerPrivacyNotice.svelte — says what the privacy policy is doing on a speaker surface (#1280).

  Two statements, each rendered only when it is true:
    * voiceprint matching is OFF: voice-based name suggestions and speaker clusters are not
      available, and by whom it was turned off (the server hides the data; this explains the gap);
    * gender inference is ON: any gender shown was inferred automatically from voice, can be
      turned off, and a link goes to the switch. Where inference is off no gender is returned, so
      there is nothing to explain.

  A failed or pending read renders nothing. Not an alert: neither statement is an error.
-->
<script lang="ts">
  import { loadEffectivePrivacy } from '$stores/privacyPolicy';
  import type { EffectivePrivacy } from '$lib/api/privacy';
  import { LOCKED_BY_KEYS } from '$lib/settings/privacyPolicy';
  import { settingsModalStore } from '$stores/settingsModalStore';
  import { t } from '$stores/locale';

  /** The file whose owner and organization decide the policy; omitted means the signed-in user. */
  export let fileUuid: string | undefined = undefined;
  /** Which statements this surface can make: gender is only worth saying where it is shown. */
  export let show: 'voiceprints' | 'gender' | 'both' = 'both';

  let effective: EffectivePrivacy | null = null;

  async function read(uuid: string | undefined) {
    const answer = await loadEffectivePrivacy(uuid);
    if (uuid === fileUuid) effective = answer;
  }

  // Re-read when the file changes (the editor panel outlives a navigation between files).
  $: void read(fileUuid);

  $: voiceprintsOff =
    effective !== null && !effective.voiceprint_matching.enabled && show !== 'gender';
  $: genderOn =
    effective !== null && effective.gender_inference.enabled && show !== 'voiceprints';
  $: voiceLayer = effective?.voiceprint_matching.locked_by ?? null;

  function openSwitch() {
    settingsModalStore.open('speaker-attributes');
  }
</script>

{#if voiceprintsOff || genderOn}
  <div class="privacy-notices" data-testid="speaker-privacy-notice">
    {#if voiceprintsOff}
      <p class="notice" data-testid="voiceprints-off-notice">
        <strong>{$t('privacy.speakerNotice.voiceprintsOffTitle')}</strong>
        {$t('privacy.speakerNotice.voiceprintsOffBody')}
        {#if voiceLayer}{$t(LOCKED_BY_KEYS[voiceLayer])}{/if}
        {#if voiceLayer === 'user'}
          <button type="button" class="link" on:click={openSwitch}>
            {$t('privacy.speakerNotice.openSettings')}
          </button>
        {/if}
      </p>
    {/if}
    {#if genderOn}
      <p class="notice" data-testid="gender-inferred-notice">
        {$t('privacy.gender.inferredNotice')}
        <button type="button" class="link" on:click={openSwitch}>
          {$t('privacy.gender.changeInSettings')}
        </button>
      </p>
    {/if}
  </div>
{/if}

<style>
  .privacy-notices {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
    margin-bottom: 0.75rem;
  }

  .notice {
    margin: 0;
    padding: 0.5rem 0.75rem;
    border: 1px solid var(--border-color);
    border-inline-start: 3px solid var(--primary-color);
    border-radius: 6px;
    background: var(--surface-color);
    color: var(--text-secondary);
    font-size: 0.8125rem;
    line-height: 1.5;
    text-align: start;
  }

  .notice strong {
    color: var(--text-color);
  }

  .link {
    display: inline;
    padding: 0;
    border: none;
    background: none;
    color: var(--primary-on-surface);
    font: inherit;
    text-decoration: underline;
    cursor: pointer;
  }
</style>
