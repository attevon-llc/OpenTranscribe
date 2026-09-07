"""Guard the ``--diar-native-gpu`` flag added for issue #711 criterion 5.

The shipped defaults describe a CROSS-CARD arrangement: ``GPU_SCALE_DEVICE_ID``
defaults to 2, the diar-native sidecar defaults to ``DIAR_NATIVE_GPU:-${GPU_DEVICE_ID:-0}``.
Nothing before this could express two different cards for a ``--fresh`` deployment
without hand-editing the shared ``.env`` (which also moves the LIVE stack) --
``--gpu-device N`` pins every var in ``GPU_DEVICE_VARS`` to the SAME value on purpose
(a flag that repoints one worker and leaves the rest behind just makes two stacks fight
over one card).

``--diar-native-gpu N`` is deliberately narrower: it moves ONLY ``DIAR_NATIVE_GPU``,
applied to ``start_app`` after ``apply_gpu_device_override`` runs, so
``--gpu-device 2 --diar-native-gpu 1`` (or the reverse) can put the sidecar and the
gpu-scale worker on different physical cards. These checks are static, over the shell
source, in the same shape as ``test_diar_native_overlay_wiring.py`` --  bringing up a
real cross-card deployment to find out is
``backend/tests/integration/test_diar_native_cross_card_placement_live.py``, which is
integration/gpu-marked and never runs in CI.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
OPENTR_SH = REPO_ROOT / "opentr.sh"


def _source() -> str:
    return OPENTR_SH.read_text(encoding="utf-8")


def _start_app_body(source: str) -> str:
    """The ``start_app() { ... }`` function body, not ``reset_app`` -- ``reset``
    explicitly refuses ``--fresh`` and has no business gaining this flag."""
    match = re.search(
        r"^start_app\(\)\s*\{(.*?)^\}\s*$",
        source,
        re.MULTILINE | re.DOTALL,
    )
    assert match, "could not find a start_app() function in opentr.sh -- did it get renamed?"
    return match.group(1)


def test_diar_native_gpu_flag_is_parsed_in_start_app() -> None:
    body = _start_app_body(_source())
    assert "--diar-native-gpu)" in body, (
        "opentr.sh's start_app() no longer parses --diar-native-gpu -- issue #711 "
        "criterion 5 (cross-card sidecar placement) has no way to be set up without "
        "hand-editing the shared .env, which also moves the live stack"
    )


def test_diar_native_gpu_override_is_applied_after_gpu_device_override() -> None:
    """Ordering is load-bearing: applying it first would let --gpu-device clobber it
    right back to the same value as every other GPU_DEVICE_VARS entry."""
    body = _start_app_body(_source())
    gpu_device_pos = body.find('apply_gpu_device_override "$GPU_DEVICE_OVERRIDE"')
    diar_native_pos = body.find('export DIAR_NATIVE_GPU="$DIAR_NATIVE_GPU_OVERRIDE"')

    assert gpu_device_pos != -1, "apply_gpu_device_override(...) call not found in start_app()"
    assert diar_native_pos != -1, (
        "start_app() no longer exports DIAR_NATIVE_GPU from DIAR_NATIVE_GPU_OVERRIDE -- "
        "the --diar-native-gpu flag would be parsed but never take effect"
    )
    assert gpu_device_pos < diar_native_pos, (
        "--diar-native-gpu is applied BEFORE --gpu-device in start_app() -- "
        "apply_gpu_device_override() sets every var in GPU_DEVICE_VARS (including "
        "DIAR_NATIVE_GPU) to the SAME value, so applying it after would silently "
        "collapse the cross-card pairing this flag exists to create"
    )


def test_diar_native_gpu_override_only_touches_diar_native_gpu() -> None:
    """--diar-native-gpu must NOT be folded into GPU_DEVICE_VARS (that would make it
    behave like --gpu-device and defeat the point: two different cards, expressible)."""
    source = _source()
    match = re.search(r"GPU_DEVICE_VARS=\((.*?)^\)", source, re.DOTALL | re.MULTILINE)
    assert match, "GPU_DEVICE_VARS array not found in opentr.sh"
    gpu_device_vars = match.group(1)

    body = _start_app_body(source)
    diar_block_match = re.search(
        r'if \[ -n "\$DIAR_NATIVE_GPU_OVERRIDE" \]; then(.*?)'
        r"pinning the diar-native sidecar",
        body,
        re.DOTALL,
    )
    assert diar_block_match, "no DIAR_NATIVE_GPU_OVERRIDE conditional block found in start_app()"
    diar_block = diar_block_match.group(1)

    exported_vars = set(re.findall(r"export\s+([A-Z_]+)=", diar_block))
    assert exported_vars == {"DIAR_NATIVE_GPU"}, (
        f"the --diar-native-gpu override block exports {sorted(exported_vars)}, not just "
        "DIAR_NATIVE_GPU -- it must move only the sidecar, or it degenerates into a second "
        "copy of --gpu-device"
    )
    # Sanity check on the fixture itself: GPU_DEVICE_VARS really does list DIAR_NATIVE_GPU
    # among the vars --gpu-device moves together, which is exactly what --diar-native-gpu
    # must be narrower than.
    assert "DIAR_NATIVE_GPU" in gpu_device_vars


def test_diar_native_gpu_flag_is_documented_in_help_text() -> None:
    source = _source()
    assert "--diar-native-gpu N" in source, (
        "the --diar-native-gpu flag has no line in opentr.sh's usage/help output"
    )
