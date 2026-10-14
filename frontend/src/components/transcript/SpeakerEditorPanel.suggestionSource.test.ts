/**
 * The backend writes `suggestion_source` as `voice_match` or `profile_match`
 * (`EMBEDDING_SUGGESTION_SOURCES` in backend/app/utils/speaker_labels.py) or
 * `llm_analysis`. The panel tested for `profile_embedding`, which nothing writes, so
 * a profile match fell into the generic branch by accident and the profile-specific
 * branches were unreachable. Both voiceprint sources must render identically.
 */
import { describe, it, expect } from 'vitest';
import fs from 'fs';
import path from 'path';
import { render } from '@testing-library/svelte';
import SpeakerEditorPanel from './SpeakerEditorPanel.svelte';

function chipFor(source: string) {
  const speaker = {
    uuid: 's1',
    name: 'SPEAKER_00',
    display_name: '',
    suggested_name: 'Ada Lovelace',
    suggestion_source: source,
    confidence: 0.9,
    has_llm_suggestion: true,
    show_suggestions_section: true,
    showSuggestions: true,
    segment_timestamps: [],
    segment_count: 0,
  };
  const { container } = render(SpeakerEditorPanel, {
    props: { file: { uuid: 'f1' }, speakerList: [speaker] },
  });
  const chip = container.querySelector('.llm-chip') as HTMLElement;
  return {
    title: chip.getAttribute('title'),
    label: chip.querySelector('.chip-source-label')?.textContent?.trim(),
    icon: chip.querySelector('.source-icon')?.innerHTML,
  };
}

describe('SpeakerEditorPanel suggestion source', () => {
  it('only branches on suggestion_source values the backend actually writes', () => {
    // Rendering cannot catch this: an unreachable branch falls through to the generic one.
    const src = fs.readFileSync(path.join(__dirname, 'SpeakerEditorPanel.svelte'), 'utf8');
    const WRITTEN = new Set(['llm_analysis', 'voice_match', 'profile_match']);
    const tested = [...src.matchAll(/suggestion_source\s*===\s*'([a-z_]+)'/g)].map((m) => m[1]);
    expect(tested.length).toBeGreaterThan(0);
    expect(tested.filter((s) => !WRITTEN.has(s))).toEqual([]);
  });

  it('shows a profile_match like a voice_match (voiceprint icon, label, tooltip)', () => {
    const voice = chipFor('voice_match');
    const profile = chipFor('profile_match');
    expect(voice.label).toBeTruthy();
    expect(profile).toEqual(voice);
  });

  it('keeps the LLM suggestion distinct from the voiceprint sources', () => {
    const llm = chipFor('llm_analysis');
    const voice = chipFor('voice_match');
    expect(llm.label).not.toEqual(voice.label);
    expect(llm.title).not.toEqual(voice.title);
  });
});
