#!/usr/bin/env python3
"""Profile MPS-specific operation costs to find optimization opportunities."""

import os
import sys
import time
import warnings

warnings.filterwarnings('ignore')
os.environ['PYANNOTE_METRICS_ENABLED'] = 'false'

# E402 on the block below: warnings.filterwarnings() and PYANNOTE_METRICS_ENABLED
# must be in place BEFORE torch / pyannote import, or the setting is read too late.
import numpy as np  # noqa: E402
import torch  # noqa: E402
import torchaudio  # noqa: E402

token = os.environ.get('HUGGINGFACE_TOKEN') or os.environ.get('HF_TOKEN')
if not token:
    print('ERROR: Set HUGGINGFACE_TOKEN')
    sys.exit(1)

device = torch.device('mps')
print(f'PyTorch: {torch.__version__}')
print(f'MPS available: {torch.backends.mps.is_available()}')
print()

# Load audio
print('Loading audio...')
waveform, sr = torchaudio.load('benchmark/test_audio/0.5h_1899s.wav')
if sr != 16000:
    waveform = torchaudio.functional.resample(waveform, sr, 16000)
duration = waveform.shape[1] / 16000
print(f'Audio: {duration:.1f}s')
print()

# 1. torch.mps.empty_cache() overhead
print('=' * 50)
print('1. torch.mps.empty_cache() overhead')
print('=' * 50)
times = []
for _i in range(100):
    t0 = time.perf_counter()
    torch.mps.empty_cache()
    t1 = time.perf_counter()
    times.append(t1 - t0)
print(f'  Mean: {np.mean(times) * 1000:.3f}ms')
print(f'  Median: {np.median(times) * 1000:.3f}ms')
print(f'  Max: {np.max(times) * 1000:.3f}ms')
print(f'  Min: {np.min(times) * 1000:.3f}ms')
print(f'  100 calls total: {sum(times) * 1000:.1f}ms')
print()

# 2. torch.mps.synchronize() overhead
print('=' * 50)
print('2. torch.mps.synchronize() overhead')
print('=' * 50)
times = []
for _i in range(100):
    t0 = time.perf_counter()
    torch.mps.synchronize()
    t1 = time.perf_counter()
    times.append(t1 - t0)
print(f'  Mean: {np.mean(times) * 1000:.3f}ms')
print(f'  Median: {np.median(times) * 1000:.3f}ms')
print(f'  Max: {np.max(times) * 1000:.3f}ms')
print(f'  Min: {np.min(times) * 1000:.3f}ms')
print()

# 3. Data transfer CPU -> MPS
print('=' * 50)
print('3. CPU -> MPS transfer speed')
print('=' * 50)
for size_mb in [1, 10, 50, 100]:
    n_floats = size_mb * 1024 * 1024 // 4
    tensor = torch.randn(n_floats)
    times = []
    for _ in range(10):
        t0 = time.perf_counter()
        gpu_tensor = tensor.to(device)
        torch.mps.synchronize()
        t1 = time.perf_counter()
        times.append(t1 - t0)
        del gpu_tensor
    mean_ms = np.mean(times) * 1000
    bw = size_mb / np.mean(times) / 1024  # GB/s
    print(f'  {size_mb:>3}MB: {mean_ms:.1f}ms ({bw:.1f} GB/s)')
print()

# 4. Unfold performance (vectorized chunk extraction)
print('=' * 50)
print('4. torch.unfold() performance')
print('=' * 50)
window_samples = int(10.0 * 16000)  # 10s window
step_samples = int(10.0 * 16000 * 0.1)  # 1s step (90% overlap like pyannote)
for dur_s in [1899, 3758, 7998]:
    n_samples = dur_s * 16000
    fake_waveform = torch.randn(1, n_samples)
    times = []
    for _ in range(5):
        t0 = time.perf_counter()
        chunks = fake_waveform.unfold(1, window_samples, step_samples).squeeze(0)
        t1 = time.perf_counter()
        times.append(t1 - t0)
        n_chunks = chunks.shape[0]
        del chunks
    mean_ms = np.mean(times) * 1000
    mem_gb = n_chunks * window_samples * 4 / (1024**3)
    print(f'  {dur_s}s audio: {n_chunks} chunks in {mean_ms:.1f}ms ({mem_gb:.1f}GB)')
print()

# 5. MPS FFT performance (fbank computation)
print('=' * 50)
print('5. MPS vs CPU FFT (fbank) performance')
print('=' * 50)
# E402 on the block below: warnings.filterwarnings() and PYANNOTE_METRICS_ENABLED
# must be in place BEFORE torch / pyannote import, or the setting is read too late.
from pyannote.audio import Pipeline  # noqa: E402

pipeline = Pipeline.from_pretrained('pyannote/speaker-diarization-community-1', token=token)
pipeline = pipeline.to(device)

embedding_model = pipeline._embedding.model_
batch_sizes = [16, 32, 64, 128]

for bs in batch_sizes:
    # Create fake batch of audio chunks
    chunk_samples = int(10.0 * 16000)
    fake_batch = torch.randn(bs, 1, chunk_samples)

    # MPS FFT
    fake_mps = fake_batch.to(device)
    times_mps = []
    for _ in range(5):
        t0 = time.perf_counter()
        fbank_mps = embedding_model.compute_fbank(fake_mps)
        torch.mps.synchronize()
        t1 = time.perf_counter()
        times_mps.append(t1 - t0)
    del fbank_mps, fake_mps

    # CPU FFT (the stock fallback)
    times_cpu = []
    embedding_model_cpu = embedding_model.cpu()
    for _ in range(5):
        t0 = time.perf_counter()
        fbank_cpu = embedding_model_cpu.compute_fbank(fake_batch)
        t1 = time.perf_counter()
        times_cpu.append(t1 - t0)
    del fbank_cpu
    embedding_model.to(device)

    mps_ms = np.mean(times_mps) * 1000
    cpu_ms = np.mean(times_cpu) * 1000
    speedup = cpu_ms / mps_ms if mps_ms > 0 else 0
    print(f'  batch={bs:>3}: MPS={mps_ms:.1f}ms, CPU={cpu_ms:.1f}ms, speedup={speedup:.2f}x')
print()

# 6. Full embedding forward pass: batch size comparison
print('=' * 50)
print('6. Embedding forward pass by batch size')
print('=' * 50)
for bs in batch_sizes:
    chunk_samples = int(10.0 * 16000)
    fake_batch = torch.randn(bs, 1, chunk_samples).to(device)
    fake_weights = torch.ones(bs, 998).to(device)  # ~998 frames for 10s

    times = []
    for _ in range(5):
        t0 = time.perf_counter()
        with torch.no_grad():
            emb = embedding_model(fake_batch, weights=fake_weights)
        torch.mps.synchronize()
        t1 = time.perf_counter()
        times.append(t1 - t0)
    del emb, fake_batch, fake_weights

    mean_ms = np.mean(times) * 1000
    per_chunk = mean_ms / bs
    print(f'  batch={bs:>3}: {mean_ms:.1f}ms total, {per_chunk:.2f}ms/chunk')
print()

# 7. MPS memory usage
print('=' * 50)
print('7. MPS memory usage')
print('=' * 50)
if hasattr(torch.mps, 'driver_allocated_memory'):
    mem = torch.mps.driver_allocated_memory() / (1024**2)
    print(f'  Driver allocated: {mem:.0f}MB')
if hasattr(torch.mps, 'current_allocated_memory'):
    mem = torch.mps.current_allocated_memory() / (1024**2)
    print(f'  Current allocated: {mem:.0f}MB')
print()

# 8. Segmentation model forward pass
print('=' * 50)
print('8. Segmentation model forward pass')
print('=' * 50)
seg_model = pipeline._segmentation.model_
for bs in [1, 4, 8, 16, 32]:
    chunk_samples = int(10.0 * 16000)
    fake_batch = torch.randn(bs, 1, chunk_samples).to(device)
    times = []
    for _ in range(5):
        t0 = time.perf_counter()
        with torch.no_grad():
            seg_out = seg_model(fake_batch)
        torch.mps.synchronize()
        t1 = time.perf_counter()
        times.append(t1 - t0)
    del seg_out, fake_batch
    mean_ms = np.mean(times) * 1000
    per_chunk = mean_ms / bs
    print(f'  batch={bs:>2}: {mean_ms:.1f}ms total, {per_chunk:.2f}ms/chunk')

print()
print('Done!')
