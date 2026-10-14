# PyAnnote Optimization — Deployment Checklist
<!-- Updated for v0.4.0 -->

## v0.4.0 Production Deployment Checklist

Use this checklist when deploying or upgrading to v0.4.0. The PyAnnote-specific optimization checklist follows below.

### Pre-Deployment
- [ ] Verify `WHISPER_MODEL` is set (default `large-v3-turbo`; use `large-v3` if translation is needed)
- [ ] Remove `ENABLE_ALIGNMENT` from `.env` if present — silently ignored in v0.4.0 (alignment is always-on via faster-whisper DTW)
- [ ] Remove `TRANSCRIPTION_ENGINE` from `.env` if present — silently ignored in v0.4.0 (single unified engine)
- [ ] Confirm `GPU_WORKER_POOL` is unset or `threads` (default changed from `prefork` to `threads`)
- [ ] Confirm `DEPLOYMENT_MODE` is set (`full` for GPU, `lite` for cloud ASR only)
- [ ] If using cloud ASR: configure at least one provider key (`DEEPGRAM_API_KEY`, etc.)

### Authentication
- [ ] Configure auth method(s) via Admin UI: **Settings → Authentication** (stored in DB, takes precedence over `.env`)
- [ ] If enabling MFA: verify TOTP is compatible with standard authenticator apps (Google Authenticator, Authy)
- [ ] If using PKI: ensure nginx mTLS overlay is active (`--with-pki` flag or `docker-compose.pki.yml`)
- [ ] If using LDAP/AD: confirm LDAP service account credentials and search base
- [ ] If using OIDC/Keycloak: confirm client ID, secret, and discovery URL

### Database
- [ ] Alembic migrations v330–v355 run automatically on backend startup — verify no errors in backend logs
- [ ] Optional: run PyAnnote v4 speaker embedding migration (`POST /admin/embeddings/migrate-v4`) for improved 256-dim accuracy

### OpenSearch
- [ ] Verify OpenSearch ML Commons neural model is registered (check backend logs on startup)
- [ ] If upgrading from v0.3.x: confirm `speakers` alias has been swapped to point to `speakers_v4` index

### File Retention
- [ ] Configure retention policy via Admin UI: **Settings → Data & Storage → File Retention** (GDPR compliance)

### Search Configuration
- [ ] `SEARCH_MAX_OVERFETCH` (default: 1000) — increase if hybrid search returns too few results
- [ ] `SEARCH_RRF_RANK_CONSTANT` (default: 30) — tune RRF scoring if needed

### Post-Deployment Verification
- [ ] Upload a test file and verify full pipeline completes (preprocess → GPU → postprocess)
- [ ] Confirm WebSocket notifications arrive in frontend
- [ ] Test search (keyword + semantic) returns results
- [ ] Verify speaker diarization assigns 256-dim embeddings (check `speakers_v4` index count)

---

## Summary
✅ **GPU Optimization**: 1.28x speedup, 66% VRAM reduction (verified on 5-file suite)
✅ **ONNX Pre-Conversion**: Zero runtime overhead (models cached)
🔄 **CPU ONNX Benchmark**: Running (results pending)
⏳ **Queue Split**: Ready for implementation after CPU benchmark

---

## Pre-Deployment Setup (One-Time)

### 1. Install Dependencies
```bash
cd /mnt/nvm/repos/transcribe-app
source backend/venv/bin/activate
pip install onnx onnxruntime  # For CPU ONNX path
```

### 2. Pre-Convert ONNX Models (One-Time)
```bash
# Run ONCE to convert and cache models
python scripts/preconvert-onnx-models.py

# Verify cached models
ls -lh models/onnx/
# pyannote_segmentation_fp32.onnx  5.7M
# pyannote_segmentation_int8.onnx  1.5M
```

### 3. Verify Pre-Conversion Script
```bash
# Run again - should show "ONNX models already cached!"
python scripts/preconvert-onnx-models.py

# ✓ ONNX models already cached!
# Location: models/onnx
# No conversion needed. Ready to use!
```

---

## GPU Optimization (Use Now)

### Profile Results
✅ **Verified on**: NVIDIA RTX A6000 (49GB)
✅ **File count**: 5 files (0.5h to 4.7h)
✅ **Speedup**: 1.28x overall, 1.44x embeddings
✅ **VRAM**: 66-68% peak reduction (7-17GB → 5.6GB constant)

### Implementation Status
- ✅ All code in optimized fork: `/mnt/nvm/repos/pyannote-audio-fork/`
- ✅ Drop-in compatible with PyAnnote v4 API
- ✅ No breaking changes
- ✅ All pre-commit hooks pass

### Deploy GPU Path
```bash
# Use optimized GPU variant in OpenTranscribe
source backend/venv/bin/activate
pip install -e /mnt/nvm/repos/pyannote-audio-fork

# Test
python scripts/benchmark-pyannote-direct.py --variant optimized --gpu-index 0
```

### Expected Performance
| File Size | Stock | Optimized | Speedup |
|-----------|-------|-----------|---------|
| 0.5h | 29.4s | 21.4s | 1.37x |
| 1.0h | 56.5s | 42.3s | 1.34x |
| 2.2h | 124.0s | 95.0s | 1.31x |
| 3.2h | 184.6s | 142.6s | 1.29x |
| 4.7h | 323.7s | 261.9s | 1.24x |

---

## CPU ONNX Path (Testing Now)

### Status: Benchmark Running
- 5 files queued for CPU processing
- Estimated runtime: 2-4 hours (CPU-bound)
- Expected: 3-5x slower than GPU, but frees GPU for ASR

### When Ready
```bash
# Test CPU ONNX path
python scripts/benchmark-pyannote-direct.py --variant optimized_cpu

# Expected to load cached ONNX models (no conversion)
```

### Architecture
- **GPU freed**: Entire GPU available for Whisper/ASR
- **CPU utilized**: 8 cores (auto-detect from cpu_count)
- **Embeddings**: Still PyTorch on CPU (acceptable slowdown)
- **Segmentation**: ONNX Runtime INT8 (quantized, 1.5MB)

---

## Queue Split (Phase 4.6)

### Architecture
```
User Upload
    ↓
┌───────────────────┐
│  Task Queue       │
└────┬───────────┬──┘
     ↓           ↓
┌─────────┐  ┌───────────┐
│ GPU     │  │ CPU       │
│ Queue   │  │ Queue     │
└────┬────┘  └─────┬─────┘
     ↓             ↓
  Whisper      ONNX Diarize
  (ASR)        (Speaker ID)
     ↓             ↓
┌───────────────────┐
│ Combined Results  │
└───────────────────┘
```

### Implementation
- Separate Celery queues: `gpu` and `cpu`
- Whisper transcription → GPU queue
- Diarization → CPU queue (using ONNX)
- Auto-route based on file size/resource availability

---

## Recommended Deployment Order

### Phase 1: Use GPU Optimization (Now)
```bash
# Install optimized fork
pip install -e /mnt/nvm/repos/pyannote-audio-fork

# Update OpenTranscribe to use it
# Expected improvement: 1.28x faster diarization
```

### Phase 2: Skip CPU ONNX Path (Not Worth It)
Current CPU ONNX implementation only optimizes segmentation, leaving embeddings (60% of time) on CPU PyTorch.
- **Result**: 30-50x slower than GPU (10+ minutes for 0.5h file)
- **Reason**: Embeddings require full WeSpeaker ResNet model conversion
- **Better alternative**: Queue split (Phase 3) allows GPU to run ASR while CPU does CPU-only work

**Decision**: Skip CPU ONNX unless GPU is completely unavailable (rare deployment).

### Phase 3: Implement Queue Split (Optional)
```bash
# Requires Celery queue changes
# Enables concurrent GPU (ASR) + CPU (diarization)
# Expected throughput: ~2x improvement
```

---

## Production Checklist

### Pre-Deployment
- [ ] Pre-convert ONNX models once: `python scripts/preconvert-onnx-models.py`
- [ ] Verify cached models: `ls -lh models/onnx/` (should show 5.7MB + 1.5MB)
- [ ] Run benchmark to verify speedup: `python scripts/benchmark-pyannote-direct.py --variant optimized`
- [ ] Check GPU memory reduction in prod: monitor VRAM during diarization

### Deployment
- [ ] Install optimized fork: `pip install -e /mnt/nvm/repos/pyannote-audio-fork`
- [ ] Update OpenTranscribe to use optimized pipeline
- [ ] Monitor latency + VRAM in production
- [ ] Document for users: GPU optimization active, expected 1.28x speedup

### Documentation
- [ ] Update README with benchmark results
- [ ] Add pre-conversion setup to deployment docs
- [ ] Document queue split plan (Phase 4.6)
- [ ] Add VRAM reduction benefit to marketing materials

---

## Environment Variables

### Required
```bash
HUGGINGFACE_TOKEN=hf_xxx  # For PyAnnote model access
MODEL_CACHE_DIR=./models  # Location for ONNX cache
```

### Optional
```bash
PYANNOTE_METRICS_ENABLED=false  # Disable metrics computation (faster)
```

---

## Files Changed

### Core Code
- `/mnt/nvm/repos/pyannote-audio-fork/` — optimized fork (complete)
  - `src/pyannote/audio/pipelines/speaker_diarization.py` — TF32, batching, prefetch, ONNX loading
  - `src/pyannote/audio/core/inference.py` — pinned memory

### Scripts
- `scripts/preconvert-onnx-models.py` — pre-conversion (run once)
- `scripts/benchmark-pyannote-direct.py` — benchmarking harness

### Documentation
- `docs/GPU_OPTIMIZATION_RESULTS.md` — technical details + benchmarks
- `docs/PYANNOTE_OPTIMIZATION_SUMMARY.md` — project completion summary
- `docs/ONNX_PRECONVERSION.md` — deployment guide for pre-conversion
- `docs/DEPLOYMENT_CHECKLIST.md` — this file

### Model Cache
- `models/onnx/pyannote_segmentation_fp32.onnx` — cached FP32 (5.7MB)
- `models/onnx/pyannote_segmentation_int8.onnx` — cached INT8 (1.5MB)

---

## Troubleshooting

### "ONNX models not found"
```
FileNotFoundError: ONNX models not found in ./models/onnx
```
**Fix**: `python scripts/preconvert-onnx-models.py`

### "onnxruntime not installed"
```
WARNING: onnxruntime not installed, ONNX CPU mode disabled
```
**Fix**: `pip install onnxruntime`

### Slower than expected
1. Verify GPU is being used: check `nvidia-smi` during run
2. Verify optimized fork is installed: `pip show pyannote-audio` (should show optimized path)
3. Run benchmark to compare: `python scripts/benchmark-pyannote-direct.py --variant optimized`

### Different speaker counts
- Variation of ±1 is normal (VBx clustering non-determinism)
- Verify with multiple runs - should be within 1 speaker difference

---

## Success Criteria

✅ **GPU Optimization**
- [x] 1.28x overall speedup verified
- [x] 1.44x embedding speedup verified
- [x] 66% VRAM reduction verified
- [x] All 5 files run without error
- [x] All pre-commit hooks pass

✅ **ONNX Pre-Conversion**
- [x] Pre-conversion script created
- [x] Models cached (no runtime conversion)
- [x] Zero conversion overhead on subsequent runs
- [x] Documentation complete

🔄 **CPU ONNX Benchmark** (in progress)
- [ ] All 5 files run on CPU
- [ ] Compare CPU vs GPU performance
- [ ] Document trade-offs

⏳ **Queue Split** (planned)
- [ ] Implement Celery queue split
- [ ] Route GPU jobs to GPU queue
- [ ] Route CPU jobs to CPU queue
- [ ] Verify concurrent execution

---

## Questions?

- **GPU Optimization Details**: See `docs/GPU_OPTIMIZATION_RESULTS.md`
- **ONNX Pre-Conversion**: See `docs/ONNX_PRECONVERSION.md`
- **Project Summary**: See `docs/PYANNOTE_OPTIMIZATION_SUMMARY.md`
- **Benchmark Results**: See `benchmark/results/benchmark_*_latest.json`

---

## Timeline

| Date | Milestone |
|------|-----------|
| Mar 8 | Modularity refactor complete |
| Mar 9 | Speaker index alias architecture |
| Mar 11 | GPU optimization verified (1.28x) |
| Mar 11 | ONNX pre-conversion implemented |
| Mar 11+ | CPU ONNX benchmark running |
| TBD | CPU benchmark complete + queue split ready |
| TBD | Production deployment |

---

## Ready for Deployment!

✅ **GPU optimization**: Production-ready now
✅ **ONNX models**: Pre-converted and cached
⏳ **CPU ONNX path**: Benchmark results pending
⏳ **Queue split**: Planned after CPU benchmark

Start deploying GPU optimization today. Results are verified and ready for production.
