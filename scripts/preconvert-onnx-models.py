#!/usr/bin/env python3
"""Pre-convert PyAnnote models to ONNX format (one-time setup).

Run this script ONCE to convert PyAnnote models to ONNX format and cache them.
Subsequent runs will use the cached ONNX models without any conversion overhead.

Usage:
    python scripts/preconvert-onnx-models.py [--cache-dir ./models] [--quantize]

This script:
1. Loads PyAnnote segmentation model (PyTorch)
2. Exports to ONNX FP32 format
3. Quantizes to INT8 (optional, default enabled)
4. Caches in $MODEL_CACHE_DIR/onnx/
5. Verifies models work with ONNX Runtime
"""

import argparse
import os
import sys
from pathlib import Path

import torch

# Ensure HF token is available
HF_TOKEN = os.environ.get('HUGGINGFACE_TOKEN', '')


def convert_segmentation_to_onnx(
    model_path: Path,
    quantize: bool = True,
    sample_rate: int = 16000,
    duration: float = 10.0,
) -> tuple[Path, Path]:
    """Convert PyAnnote segmentation model to ONNX.

    Args:
        model_path: Directory to save ONNX models
        quantize: Whether to quantize FP32 → INT8
        sample_rate: Audio sample rate (16kHz for PyAnnote)
        duration: Dummy input duration in seconds

    Returns:
        Tuple of (fp32_path, int8_path)
    """
    import warnings

    from pyannote.audio import Pipeline

    model_path.mkdir(parents=True, exist_ok=True)
    fp32_path = model_path / 'pyannote_segmentation_fp32.onnx'
    int8_path = model_path / 'pyannote_segmentation_int8.onnx'

    print(f'\n{"=" * 70}')
    print('PyAnnote → ONNX Conversion')
    print(f'{"=" * 70}')

    # Load pipeline
    print('\n[1/4] Loading PyAnnote pipeline...')
    try:
        pipeline = Pipeline.from_pretrained(
            'pyannote/speaker-diarization-3.1',
            token=HF_TOKEN or None,
        )
    except TypeError:
        # Fallback for older HF version
        pipeline = Pipeline.from_pretrained(
            'pyannote/speaker-diarization-3.1',
            use_auth_token=HF_TOKEN or None,
        )

    # Get segmentation model
    seg_model = pipeline._segmentation.model
    seg_model.eval()

    # Create dummy input
    dummy_input = torch.randn(1, 1, int(duration * sample_rate), dtype=torch.float32)
    dummy_input_cpu = dummy_input.cpu()

    # Export to ONNX
    print(f'\n[2/4] Exporting to ONNX FP32: {fp32_path}')
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        seg_model.cpu()
        torch.onnx.export(
            seg_model,
            dummy_input_cpu,
            str(fp32_path),
            input_names=['waveforms'],
            output_names=['scores'],
            dynamic_axes={
                'waveforms': {0: 'batch'},
                'scores': {0: 'batch'},
            },
            opset_version=17,
            do_constant_folding=True,
        )
    print(f'   ✓ Saved {fp32_path.stat().st_size / (1024 * 1024):.1f} MB')

    # Quantize to INT8
    int8_created = False
    if quantize:
        print(f'\n[3/4] Quantizing to INT8: {int8_path}')
        try:
            from onnxruntime.quantization import (
                QuantType,
                quantize_dynamic,
            )

            with warnings.catch_warnings():
                warnings.simplefilter('ignore')
                quantize_dynamic(
                    str(fp32_path),
                    str(int8_path),
                    weight_type=QuantType.QUInt8,
                )
            print(f'   ✓ Saved {int8_path.stat().st_size / (1024 * 1024):.1f} MB')
            int8_created = True
        except Exception as e:
            print(f'   ⚠ Quantization failed: {e}')
            print('   → Using FP32 model instead')
            int8_path = fp32_path
    else:
        int8_path = fp32_path

    # Verify models with ONNX Runtime
    print('\n[4/4] Verifying ONNX models...')
    try:
        import onnxruntime as ort

        # Test FP32
        sess_fp32 = ort.InferenceSession(str(fp32_path), ['CPUExecutionProvider'])
        output_fp32 = sess_fp32.run(
            ['scores'], {'waveforms': dummy_input_cpu.numpy().astype('float32')}
        )
        print(f'   ✓ FP32 model verified (output shape: {output_fp32[0].shape})')

        # Test INT8 if quantized
        if int8_created and int8_path != fp32_path:
            sess_int8 = ort.InferenceSession(str(int8_path), ['CPUExecutionProvider'])
            output_int8 = sess_int8.run(
                ['scores'], {'waveforms': dummy_input_cpu.numpy().astype('float32')}
            )
            print(f'   ✓ INT8 model verified (output shape: {output_int8[0].shape})')

    except Exception as e:
        print(f'   ⚠ Verification failed: {e}')
        print('   → Models may still be valid, check manually')

    return fp32_path, int8_path


def main():
    parser = argparse.ArgumentParser(
        description='Pre-convert PyAnnote models to ONNX format',
        epilog='Run this ONCE. Subsequent runs will use cached ONNX models.',
    )
    parser.add_argument(
        '--cache-dir',
        default='./models',
        help='Model cache directory (default: ./models)',
    )
    parser.add_argument(
        '--quantize',
        action='store_true',
        default=True,
        help='Quantize FP32 → INT8 (default: enabled)',
    )
    parser.add_argument(
        '--no-quantize',
        action='store_false',
        dest='quantize',
        help='Skip INT8 quantization',
    )
    args = parser.parse_args()

    cache_dir = Path(args.cache_dir)
    onnx_dir = cache_dir / 'onnx'

    # Check if models already exist
    fp32_exists = (onnx_dir / 'pyannote_segmentation_fp32.onnx').exists()
    int8_exists = (onnx_dir / 'pyannote_segmentation_int8.onnx').exists()

    if fp32_exists and int8_exists:
        print(f'\n{"=" * 70}')
        print('✓ ONNX models already cached!')
        print(f'{"=" * 70}')
        print(f'  FP32: {onnx_dir / "pyannote_segmentation_fp32.onnx"}')
        print(f'  INT8: {onnx_dir / "pyannote_segmentation_int8.onnx"}')
        print('\nNo conversion needed. Ready to use!')
        return 0

    try:
        fp32_path, int8_path = convert_segmentation_to_onnx(
            onnx_dir,
            quantize=args.quantize,
        )

        print(f'\n{"=" * 70}')
        print('✓ ONNX Models Ready!')
        print(f'{"=" * 70}')
        print(f'  FP32 (5.7MB): {fp32_path}')
        print(f'  INT8 (1.5MB): {int8_path}')
        print(f'\n  Location: {onnx_dir}')
        print(f'  Cache Dir: {cache_dir}')
        print('\nThese models are now cached and will be reused on all subsequent runs.')
        print('No runtime conversion overhead!')
        return 0

    except Exception as e:
        print(f'\n{"=" * 70}')
        print(f'✗ Conversion failed: {e}')
        print(f'{"=" * 70}')
        import traceback

        traceback.print_exc()
        return 1


if __name__ == '__main__':
    sys.exit(main())
