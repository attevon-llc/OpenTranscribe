#!/usr/bin/env python3
"""Compare stock vs optimized speaker diarization output on MPS."""

import os
import sys
import warnings

warnings.filterwarnings('ignore')
os.environ['PYANNOTE_METRICS_ENABLED'] = 'false'

# E402 on the block below: warnings.filterwarnings() and PYANNOTE_METRICS_ENABLED
# must be in place BEFORE torch / pyannote import, or the setting is read too late.
import torch  # noqa: E402
import torchaudio  # noqa: E402

token = os.environ.get('HUGGINGFACE_TOKEN') or os.environ.get('HF_TOKEN')
if not token:
    print('ERROR: Set HUGGINGFACE_TOKEN')
    sys.exit(1)

# Load audio
print('Loading audio...')
waveform, sr = torchaudio.load('benchmark/test_audio/0.5h_1899s.wav')
if sr != 16000:
    waveform = torchaudio.functional.resample(waveform, sr, 16000)
audio_input = {'waveform': waveform, 'sample_rate': 16000}
duration = waveform.shape[1] / 16000

# E402 on the block below: warnings.filterwarnings() and PYANNOTE_METRICS_ENABLED
# must be in place BEFORE torch / pyannote import, or the setting is read too late.
from pyannote.audio import Pipeline  # noqa: E402

pipeline = Pipeline.from_pretrained('pyannote/speaker-diarization-community-1', token=token)
pipeline = pipeline.to(torch.device('mps'))
pipeline.embedding_batch_size = 32

print(f'Audio: {duration:.1f}s ({duration / 3600:.2f}h)')
print(f'PyAnnote version: {pipeline.__class__.__module__}')
print()

# Run optimized
print('Running OPTIMIZED...')
output = pipeline(audio_input)
if hasattr(output, 'exclusive_speaker_diarization'):
    ann_opt = output.exclusive_speaker_diarization
else:
    ann_opt = output

labels_opt = sorted(ann_opt.labels())
segs_opt = list(ann_opt.itertracks(yield_label=True))

print(f'  Speakers: {len(labels_opt)}')
print(f'  Segments: {len(segs_opt)}')
print()
for spk in labels_opt:
    total_dur = sum(seg.end - seg.start for seg, _, lbl in segs_opt if lbl == spk)
    seg_count = sum(1 for _, _, lbl in segs_opt if lbl == spk)
    pct = total_dur / duration * 100
    print(f'  {spk}: {total_dur:.1f}s ({pct:.1f}%) - {seg_count} segments')

# Now install stock and run
print()
print('Installing STOCK pyannote-audio...')
# E402: deliberately imported here — this is the point in the script where it swaps the
# installed pyannote-audio, and the import reads as part of that step.
import subprocess  # noqa: E402

subprocess.run(
    [
        sys.executable,
        '-m',
        'pip',
        'install',
        'pyannote.audio>=4.0.0',
        '--force-reinstall',
        '--no-deps',
        '-q',
    ],
    check=True,
    capture_output=True,
)

# Must reimport after reinstall - use subprocess for clean module state
print('Running STOCK (in subprocess for clean imports)...')
stock_script = """
import os, warnings, json
warnings.filterwarnings("ignore")
os.environ["PYANNOTE_METRICS_ENABLED"] = "false"
import torch, torchaudio
from pyannote.audio import Pipeline

token = os.environ["HUGGINGFACE_TOKEN"]
waveform, sr = torchaudio.load("benchmark/test_audio/0.5h_1899s.wav")
if sr != 16000:
    waveform = torchaudio.functional.resample(waveform, sr, 16000)

pipeline = Pipeline.from_pretrained("pyannote/speaker-diarization-community-1", token=token)
pipeline = pipeline.to(torch.device("mps"))
pipeline.embedding_batch_size = 32

output = pipeline({"waveform": waveform, "sample_rate": 16000})
if hasattr(output, "exclusive_speaker_diarization"):
    ann = output.exclusive_speaker_diarization
else:
    ann = output

labels = sorted(ann.labels())
segs = list(ann.itertracks(yield_label=True))
result = {
    "speakers": len(labels),
    "segments": len(segs),
    "per_speaker": {}
}
for spk in labels:
    total_dur = sum(seg.end - seg.start for seg, _, lbl in segs if lbl == spk)
    seg_count = sum(1 for _, _, lbl in segs if lbl == spk)
    result["per_speaker"][spk] = {"duration": round(total_dur, 1), "segments": seg_count}
print(json.dumps(result))
"""

result = subprocess.run(
    [sys.executable, '-c', stock_script],
    capture_output=True,
    text=True,
    env={**os.environ, 'HUGGINGFACE_TOKEN': token},
)

if result.returncode != 0:
    print(f'Stock run failed: {result.stderr[-500:]}')
else:
    import json

    stock = json.loads(result.stdout.strip().split('\n')[-1])
    print(f'  Speakers: {stock["speakers"]}')
    print(f'  Segments: {stock["segments"]}')
    print()
    for spk, data in sorted(stock['per_speaker'].items()):
        pct = data['duration'] / duration * 100
        print(f'  {spk}: {data["duration"]}s ({pct:.1f}%) - {data["segments"]} segments')

# Reinstall optimized
print()
print('Reinstalling optimized fork...')
subprocess.run(
    [sys.executable, '-m', 'pip', 'install', '-e', '.', '--no-deps', '-q'],
    check=True,
    capture_output=True,
)
print('Done. Optimized fork restored.')
