#!/usr/bin/env python3
"""Print the CUDA ordinal -> physical card mapping, WITHOUT touching any GPU.

Why this exists (issue #719): CUDA's default `FASTEST_FIRST` ordering does not match
`nvidia-smi` on multi-GPU hosts, so "GPU 1" means two different cards depending on who
is asking. Every claim in this repo about which card a service got should be checked
with this rather than reasoned about from an ordinal.

It uses the CUDA **driver** API directly (`cuInit` + `cuDeviceGetPCIBusId`). That
initialises the driver but creates no context and allocates no device memory, so it is
safe to run against cards owned by other work — unlike `torch.cuda.get_device_name()`,
which opens a ~300 MiB primary context on every device it touches.

    python3 scripts/gpu-device-order-probe.py
    CUDA_DEVICE_ORDER=PCI_BUS_ID python3 scripts/gpu-device-order-probe.py

Inside a container, to check what a given reservation actually handed over:

    docker run --rm --gpus '"device=1"' -v "$PWD/scripts:/s:ro" \
        opentranscribe-backend:latest python3 /s/gpu-device-order-probe.py

Cross-check the bus ids against `nvidia-smi --query-gpu=index,name,pci.bus_id --format=csv`.
"""

import ctypes
import os
import sys


def main() -> int:
    try:
        lib = ctypes.CDLL('libcuda.so.1')
    except OSError as e:
        print(f'libcuda.so.1 not loadable ({e}) — no NVIDIA driver visible here')
        return 1

    rc = lib.cuInit(0)
    if rc != 0:
        # 100 == CUDA_ERROR_NO_DEVICE, the signature of CUDA_VISIBLE_DEVICES naming an
        # index that does not exist inside a single-device container.
        print(f'cuInit failed rc={rc} (100 = CUDA_ERROR_NO_DEVICE)')
        print(
            f'  CUDA_VISIBLE_DEVICES={os.environ.get("CUDA_VISIBLE_DEVICES")!r} '
            f'CUDA_DEVICE_ORDER={os.environ.get("CUDA_DEVICE_ORDER")!r}'
        )
        return 1

    count = ctypes.c_int()
    lib.cuDeviceGetCount(ctypes.byref(count))

    order = os.environ.get('CUDA_DEVICE_ORDER')
    print(f'CUDA_DEVICE_ORDER    = {order!r}' + ('' if order else '  (default: FASTEST_FIRST)'))
    print(f'CUDA_VISIBLE_DEVICES = {os.environ.get("CUDA_VISIBLE_DEVICES")!r}')
    print(f'GPU_DEVICE_ID        = {os.environ.get("GPU_DEVICE_ID")!r}')
    print(f'visible CUDA devices = {count.value}')

    for i in range(count.value):
        dev = ctypes.c_int()
        lib.cuDeviceGet(ctypes.byref(dev), i)
        bus = ctypes.create_string_buffer(64)
        lib.cuDeviceGetPCIBusId(bus, 64, dev)
        name = ctypes.create_string_buffer(128)
        lib.cuDeviceGetName(name, 128, dev)
        print(f'  cuda ordinal {i}: pci={bus.value.decode()}  {name.value.decode()}')

    return 0


if __name__ == '__main__':
    sys.exit(main())
