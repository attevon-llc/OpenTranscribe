#!/usr/bin/env python3
"""Determine whether the 5.57e-3 segmentation ONNX drift matters for diarization.

Pyannote binarizes segmentation logits post-sigmoid; if post-binarization
frame-level predictions match, the logit drift is irrelevant to DER.

Also compares against onnx-community/pyannote-segmentation-3.0 to see if the
upstream artifact has different drift characteristics.
"""

from __future__ import annotations

import os
from pathlib import Path

import torch


def main():
    from pyannote.audio import Model

    token = os.environ.get('HF_TOKEN') or os.environ.get('HUGGINGFACE_TOKEN')
    model = Model.from_pretrained('pyannote/segmentation-3.0', token=token).eval().cuda()
    for mod in model.modules():
        mod.eval()

    import onnxruntime as ort

    local_onnx = Path('/tmp/onnx_spike_v2/segmentation.onnx')
    if not local_onnx.exists():
        print(f'Missing {local_onnx}; run spike-onnx-export-v2.py first')
        return

    session = ort.InferenceSession(
        str(local_onnx),
        providers=[
            ('CUDAExecutionProvider', {'device_id': 0}),
            'CPUExecutionProvider',
        ],
    )

    # Compare on multiple realistic inputs
    torch.manual_seed(42)
    for trial in range(3):
        x = torch.randn(4, 1, 160000, device='cuda') * 0.1  # realistic audio range
        with torch.no_grad():
            y_pt = model(x)
        y_ort = torch.from_numpy(session.run(None, {'input_values': x.cpu().numpy()})[0]).cuda()

        # Raw logit drift
        logit_diff = (y_pt - y_ort).abs().max().item()

        # Post-softmax (powerset 7 classes)
        p_pt = torch.softmax(y_pt, dim=-1)
        p_ort = torch.softmax(y_ort, dim=-1)
        prob_diff = (p_pt - p_ort).abs().max().item()

        # Argmax (which powerset class per frame)
        class_pt = y_pt.argmax(dim=-1)
        class_ort = y_ort.argmax(dim=-1)
        frame_mismatch = (class_pt != class_ort).float().mean().item()

        print(
            f'Trial {trial}: logit_abs={logit_diff:.3e} prob_abs={prob_diff:.3e} '
            f'frame_class_mismatch={frame_mismatch * 100:.4f}% of {class_pt.numel()} frames'
        )


if __name__ == '__main__':
    main()
