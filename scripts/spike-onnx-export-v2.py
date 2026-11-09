#!/usr/bin/env python3
"""Phase 6.2 v2 spike — corrected approach per pyannote #1929 + onnx-community.

Key changes vs v1:
  - WeSpeaker: export ONLY the ResNet backbone (skip compute_fbank's vmap);
               extract fbank separately in plain PyTorch batched code.
               Input: (B, num_frames, 80) pre-computed fbank → Output: (B, 256).
  - Segmentation: follow onnx-community/pyannote-segmentation-3.0 recipe
               (do_constant_folding=True, input_values name, dynamic axes).
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

import torch
from google.protobuf.message import DecodeError

logger = logging.getLogger(__name__)

# What an export / load / session / parity step raises; anything else is a bug and propagates.
_STEP_ERRORS = (RuntimeError, ValueError, TypeError, OSError, ImportError)

_failures: list[str] = []

OUT = Path('/tmp/onnx_spike_v2')
OUT.mkdir(parents=True, exist_ok=True)


def report(name: str, ok: bool, detail: str = '') -> None:
    mark = 'PASS' if ok else 'FAIL'
    if not ok:
        _failures.append(name)
    print(f'[{mark}] {name}: {detail}')


def try_export_wespeaker_backbone() -> None:
    """Export ResNet backbone only (per pyannote-audio discussion #1929)."""
    print('\n=== WeSpeaker ResNet backbone export (input: fbank, not waveform) ===')
    try:
        from pyannote.audio.models.embedding.wespeaker import WeSpeakerResNet34

        model = WeSpeakerResNet34().eval().cuda()
        report('wespeaker/load', True, type(model).__name__)
    except _STEP_ERRORS as exc:
        logger.exception('step failed')
        report('wespeaker/load', False, str(exc))
        return

    # Real WeSpeaker pipeline: fbank is precomputed externally, ResNet backbone
    # sees (batch, num_frames=~200, 80 mel channels).
    class Backbone(torch.nn.Module):
        def __init__(self, m):
            super().__init__()
            self.resnet = m.resnet

        def forward(self, fbank):
            out = self.resnet(fbank)
            return out[-1] if isinstance(out, tuple) else out

    wrapper = Backbone(model).eval().cuda()
    dummy_fbank = torch.randn(2, 200, 80, device='cuda')

    try:
        with torch.no_grad():
            y_eager = wrapper(dummy_fbank)
        report('wespeaker/forward_eager', True, f'out_shape={tuple(y_eager.shape)}')
    except _STEP_ERRORS as exc:
        logger.exception('step failed')
        report('wespeaker/forward_eager', False, str(exc))
        return

    onnx_path = OUT / 'embedding_backbone.onnx'
    try:
        torch.onnx.export(
            wrapper,
            (dummy_fbank,),
            str(onnx_path),
            opset_version=18,
            input_names=['fbank_features'],
            output_names=['embeddings'],
            dynamic_axes={
                'fbank_features': {0: 'batch_size', 1: 'num_frames'},
                'embeddings': {0: 'batch_size'},
            },
            do_constant_folding=True,
        )
        report('wespeaker/export', True, f'{onnx_path} ({onnx_path.stat().st_size // 1024} KiB)')
    except _STEP_ERRORS as exc:
        logger.exception('step failed')
        report('wespeaker/export', False, f'{type(exc).__name__}: {str(exc)[:200]}')
        return

    import onnx

    try:
        m = onnx.load(str(onnx_path))
        onnx.checker.check_model(m)
        report('wespeaker/onnx_check', True, f'opset={m.opset_import[0].version}')
    except (onnx.checker.ValidationError, OSError, DecodeError):
        logger.exception('onnx check failed')
        report('wespeaker/onnx_check', False, 'see traceback')

    try:
        import onnxruntime as ort

        session = ort.InferenceSession(
            str(onnx_path),
            providers=[
                ('CUDAExecutionProvider', {'device_id': 0}),
                'CPUExecutionProvider',
            ],
        )
        report('wespeaker/ort_session', True, f'providers={session.get_providers()}')
    except _STEP_ERRORS as exc:
        logger.exception('step failed')
        report('wespeaker/ort_session', False, str(exc))
        return

    try:
        y_ort = session.run(None, {'fbank_features': dummy_fbank.cpu().numpy()})[0]
        y_ort_t = torch.from_numpy(y_ort).to(y_eager.device)
        diff = (y_eager - y_ort_t).abs().max().item()
        cos = torch.nn.functional.cosine_similarity(y_eager, y_ort_t, dim=-1).mean().item()
        rtol_ok = diff < 1e-4
        report('wespeaker/parity', rtol_ok, f'max_abs_diff={diff:.3e}, mean_cos_sim={cos:.8f}')
    except _STEP_ERRORS as exc:
        logger.exception('step failed')
        report('wespeaker/parity', False, str(exc))


def try_export_segmentation_v2() -> None:
    """Follow onnx-community/pyannote-segmentation-3.0 recipe exactly."""
    print('\n=== Pyannote segmentation-3.0 export (constant-folding, input_values) ===')
    token = os.environ.get('HF_TOKEN') or os.environ.get('HUGGINGFACE_TOKEN')
    try:
        from pyannote.audio import Model

        model = Model.from_pretrained('pyannote/segmentation-3.0', token=token).eval().cuda()
        # Force eval on every submodule (including InstanceNorm/BN)
        for mod in model.modules():
            mod.eval()
            if hasattr(mod, 'training'):
                mod.training = False
        report('seg/load', True, type(model).__name__)
    except _STEP_ERRORS as exc:
        logger.exception('step failed')
        report('seg/load', False, str(exc))
        return

    dummy = torch.zeros(2, 1, 160000, device='cuda')  # onnx-community uses zeros
    try:
        with torch.no_grad():
            y_eager = model(dummy)
        report('seg/forward_eager', True, f'out_shape={tuple(y_eager.shape)}')
    except _STEP_ERRORS as exc:
        logger.exception('step failed')
        report('seg/forward_eager', False, str(exc))
        return

    onnx_path = OUT / 'segmentation.onnx'
    try:
        torch.onnx.export(
            model,
            (dummy,),
            str(onnx_path),
            opset_version=18,
            input_names=['input_values'],
            output_names=['logits'],
            dynamic_axes={
                'input_values': {0: 'batch_size', 1: 'num_channels', 2: 'num_samples'},
                'logits': {0: 'batch_size', 1: 'num_frames'},
            },
            do_constant_folding=True,
        )
        report('seg/export', True, f'{onnx_path} ({onnx_path.stat().st_size // 1024} KiB)')
    except _STEP_ERRORS as exc:
        logger.exception('step failed')
        report('seg/export', False, f'{type(exc).__name__}: {str(exc)[:200]}')
        return

    import onnx

    try:
        m = onnx.load(str(onnx_path))
        onnx.checker.check_model(m)
        report('seg/onnx_check', True, f'opset={m.opset_import[0].version}')
    except (onnx.checker.ValidationError, OSError, DecodeError):
        logger.exception('onnx check failed')
        report('seg/onnx_check', False, 'see traceback')

    try:
        import onnxruntime as ort

        session = ort.InferenceSession(
            str(onnx_path),
            providers=[
                ('CUDAExecutionProvider', {'device_id': 0}),
                'CPUExecutionProvider',
            ],
        )
        report('seg/ort_session', True, f'providers={session.get_providers()}')
    except _STEP_ERRORS as exc:
        logger.exception('step failed')
        report('seg/ort_session', False, str(exc))
        return

    try:
        # Use RANDOM input (not zeros) to catch numeric bugs that hide at 0
        test_input = torch.randn(2, 1, 160000, device='cuda')
        with torch.no_grad():
            y_eager = model(test_input)
        y_ort = session.run(None, {'input_values': test_input.cpu().numpy()})[0]
        y_ort_t = torch.from_numpy(y_ort).to(y_eager.device)
        diff = (y_eager - y_ort_t).abs().max().item()
        rtol_ok = diff < 1e-3  # seg logits; post-sigmoid <1e-3 drift is fine
        report('seg/parity', rtol_ok, f'max_abs_diff={diff:.3e}')
    except _STEP_ERRORS as exc:
        logger.exception('step failed')
        report('seg/parity', False, str(exc))


if __name__ == '__main__':
    torch.manual_seed(0)
    try_export_wespeaker_backbone()
    try_export_segmentation_v2()
    sys.exit(1 if _failures else 0)
