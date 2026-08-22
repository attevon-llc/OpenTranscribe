---
sidebar_position: 6
---

# Diarization Boundary Correction

Even with state-of-the-art diarization, speaker labels can drift by a word or two right at the moment one person stops talking and another begins. OpenTranscribe ships two post-processing stages that clean up these turn-boundary mistakes, so the transcript reads the way the conversation actually happened.

Both stages are controlled from **Settings → Engine Configuration** in the admin UI. Changes are saved to the database and take effect on the next transcription — no restart required.

## The two problems it solves

### 1. Wrong-speaker islands

PyAnnote decides "who spoke when" on a coarse time grid (roughly 200–250 ms). A single word that straddles a turn boundary can get assigned to whichever speaker happens to win that one overlap calculation. The result is a short **island** — one to three words attributed to the wrong person, sitting in the middle of an otherwise-correct stretch of speech, with no real pause around it.

**Before** (raw diarization):

```
SPEAKER_00: So I started reading about transformers and
SPEAKER_01: I thought                            ← wrong: a 2-word island
SPEAKER_00: that was the most interesting part.
```

**After** (boundary smoothing):

```
SPEAKER_00: So I started reading about transformers and I thought that was the most interesting part.
```

The two flanking runs are clearly the same speaker with no pause between them, so the stray island is collapsed back into `SPEAKER_00`.

### 2. Absorbed backchannels

When one person says a brief "yeah", "right", or "mm-hmm" while the other is mid-sentence, diarization often **absorbs** that short word into the dominant speaker's turn — it never appears as its own island, so the smoother above can't see it. The word stays attributed to the wrong person.

**Before:**

```
SPEAKER_00: ...and that's when the loss finally started going down, yeah, which surprised me.
                                                              ↑ "yeah" was actually SPEAKER_01
```

**After** (acoustic re-check):

```
SPEAKER_00: ...and that's when the loss finally started going down,
SPEAKER_01: yeah,
SPEAKER_00: which surprised me.
```

The acoustic re-check listens to the disputed word again and reassigns it to the speaker whose voice it actually matches.

## Boundary smoothing (on by default)

Boundary smoothing fixes wrong-speaker islands. It is:

- **On by default** — and it matters *more* under the current diarizer, not less. Measured on the same hand-labeled podcast clip, per diarization backend:

  | Diarizer | WSER off | WSER on | Reduction | Bleed islands |
  |---|---|---|---|---|
  | `diar-native` (speakrs) — **the default** | 0.01152 | 0.00266 | **−76.9%** | 13 → 3 |
  | PyAnnote (the documented failover) | 0.00932 | 0.00621 | −33.3% | 7 → 1 |

  The original 32% figure described PyAnnote, which was the default when smoothing shipped. Re-derived against speakrs for issue #520: speakrs' *raw* boundaries are noisier (more short bleed islands), and the smoother removes them very effectively — so the smoothed result is **better** than PyAnnote's even though the unsmoothed one is worse. Turning smoothing off costs roughly three times as much now as it did then. No regression on the AMI meeting benchmark (diarization error rate unchanged).
- **Fast and pure-CPU** — it only inspects the per-word speaker labels and timings. It never touches the audio or loads a model, so it adds negligible time.
- **Conservative** — it only collapses a short run (1–3 words by default) when it is flanked on *both* sides by genuine, longer runs of the *same* other speaker, and there is *no real pause* at either seam. A pause is treated as evidence of a true turn and left alone.

For most users there is nothing to configure: leave it on.

### Toggle and advanced knobs

In **Settings → Engine Configuration**, the **Boundary Smoothing** toggle turns the whole stage on or off.

The fine-grained knobs (maximum island length, maximum island duration, minimum flank length, minimum silent gap) use safe defaults and are rarely worth changing. They are overridable in the database under the `engine.boundary_*` keys if you need to tune behaviour for an unusual corpus.

## Acoustic backchannel re-check (off by default, experimental)

The acoustic re-check fixes absorbed backchannels. It is **off by default** and **experimental**.

When enabled, it runs during the GPU stage while the audio and the per-speaker voiceprints are still in memory. For each short, disputed word it:

1. Re-embeds just that word's audio with the same voice-fingerprint model the diarizer uses.
2. Compares the result against every speaker's voiceprint.
3. Reassigns the word to the best-matching speaker — but only if that match is clearly better than the current one.

:::info It only relabels existing words
The acoustic re-check **never invents, adds, or removes a word**. It only changes the *speaker* of words the transcription already produced. If the transcript doesn't contain a "yeah", the re-check will not conjure one.
:::

### When to enable it

Consider turning it on for content with a lot of conversational backchannel — two-person interviews and podcasts, where one host frequently affirms the other ("yeah", "right", "exactly"). In validation it removed roughly another **15% of WSER** on top of the smoother.

The cost is modest but real: it adds about **1.9 seconds per 10-minute file**, because it has to re-embed each disputed word. For single-speaker recordings, lectures, or any audio without overlapping backchannel, there is no benefit — leave it off.

### Toggle and settings

In **Settings → Engine Configuration**:

| Setting | Default | What it does |
|---|---|---|
| **Acoustic Backchannel Re-check** | Off | Master switch for the stage. |
| **Cosine margin** | `0.05` | How much *better* another speaker's voiceprint must match before a word is reassigned. Higher = more conservative (fewer changes). Range 0–1. |
| **Max word duration** | `1.0` s | Only words at or below this length are re-checked (backchannels are short). Range 0.1–5 s. |

If you see the re-check making changes you disagree with, raise the cosine margin; to make it more aggressive, lower it.

## How they work together

The two stages are complementary and run at different points in the pipeline:

1. **Acoustic re-check** runs first, inside the GPU stage, correcting absorbed backchannels by voiceprint while the audio is still loaded.
2. **Boundary smoothing** runs at the final post-processing chokepoint, collapsing any remaining wrong-speaker islands before the transcript is split into speaker turns.

Because the corrected labels flow into the same resegmentation step, the cleaned-up speaker assignment is what drives the final segment boundaries you see in the transcript.

## See also

- [Speaker Diarization](./speaker-diarization.md) — how "who spoke when" is determined in the first place
- [Diarization Boundary Correction (developer reference)](../developer-guide/diarization-boundary-correction.md) — architecture, metrics, and the benchmark harness
