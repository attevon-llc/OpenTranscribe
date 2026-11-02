"""GPU statistics read from nvidia-smi, with a torch fallback for unified-memory boards.

The one parser behind both the periodic ``system.update_gpu_stats`` task and the admin
dashboard's direct read.
"""

import logging
import subprocess

from app.utils.stats_helpers import format_bytes

logger = logging.getLogger(__name__)


def _safe_float(value: str | None) -> float | None:
    """Parse a numeric string to float, returning None for non-numeric values.

    Handles nvidia-smi reporting '[N/A]' or 'N/A' on systems with unified
    memory (e.g. NVIDIA DGX Spark / Blackwell GB10).

    Args:
        value: String to parse, or None.

    Returns:
        Parsed float, or None if the value is not a valid number.
    """
    if value is None:
        return None
    value = value.strip()
    if not value or value.upper() in ("[N/A]", "N/A", "N/A%"):
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _query_gpu_memory_torch(device_id: int) -> tuple[float, float] | None:
    """Query GPU memory via torch.cuda.mem_get_info() as a fallback.

    This is needed for systems with unified CPU+GPU memory (e.g. DGX Spark)
    where nvidia-smi reports memory stats as '[N/A]'.

    Args:
        device_id: CUDA device index.

    Returns:
        Tuple of (free_bytes, total_bytes), or None if torch is unavailable.
    """
    try:
        import torch

        if not torch.cuda.is_available():
            return None
        free, total = torch.cuda.mem_get_info(device_id)
        return (float(free), float(total))
    except Exception as e:
        logger.debug(f"torch.cuda.mem_get_info({device_id}) failed: {e}")
        return None


def query_gpu_stats(device_id: int, *, timeout: float | None = 5) -> dict | None:
    """Query GPU stats for one device, with unified-memory fallback.

    First tries nvidia-smi for full stats. If memory values come back as
    '[N/A]' (unified memory systems like DGX Spark), falls back to
    torch.cuda.mem_get_info() for memory data.

    Args:
        device_id: NVIDIA device index to query.
        timeout: Seconds to wait for nvidia-smi before giving up.

    Returns:
        Dict of GPU stats, or None if the query fails.
    """
    try:
        result = subprocess.run(  # noqa: S603 # nosec B603 B607
            [  # noqa: S607
                "nvidia-smi",
                "--query-gpu=name,memory.used,memory.total,memory.free,"
                "utilization.gpu,temperature.gpu",
                "--format=csv,noheader,nounits",
                f"--id={device_id}",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=True,
        )
        parts = result.stdout.strip().split(", ")
        gpu_name = parts[0]
        memory_used_mib = _safe_float(parts[1])
        memory_total_mib = _safe_float(parts[2])
        memory_free_mib = _safe_float(parts[3])
        # int(float(...)), never int(raw) — issue #458. `_safe_float` was used as the
        # GUARD while `int()` took the raw string, so nvidia-smi emitting "37.5" (which
        # it does on some driver/board combinations) passed the guard and then raised
        # ValueError inside int(). That was swallowed by the broad except below and the
        # task reported "No GPU Available" for a perfectly healthy GPU.
        _utilization = _safe_float(parts[4]) if len(parts) > 4 else None
        utilization_percent = int(_utilization) if _utilization is not None else None
        _temperature = _safe_float(parts[5]) if len(parts) > 5 else None
        temperature_celsius = int(_temperature) if _temperature is not None else None

        memory_source = "nvidia-smi"

        # If nvidia-smi returned N/A for memory (unified memory systems),
        # fall back to torch.cuda.mem_get_info()
        if memory_total_mib is None or memory_used_mib is None:
            torch_mem = _query_gpu_memory_torch(device_id)
            if torch_mem is not None:
                free_bytes, total_bytes = torch_mem
                used_bytes = total_bytes - free_bytes
                memory_total = total_bytes
                memory_used = used_bytes
                memory_free = free_bytes
                memory_source = "torch.cuda.mem_get_info"
            else:
                # Both nvidia-smi and torch failed — report what we can
                return {
                    "available": True,
                    "device_id": device_id,
                    "name": gpu_name,
                    "memory_total": "N/A (unified memory)",
                    "memory_used": "N/A",
                    "memory_free": "N/A",
                    "memory_percent": "N/A",
                    "utilization_percent": f"{utilization_percent}%"
                    if utilization_percent is not None
                    else "N/A",
                    "temperature_celsius": temperature_celsius,
                    "memory_source": "unavailable",
                    "memory_note": "Unified memory system — install PyTorch "
                    "for memory stats via torch.cuda.mem_get_info()",
                }
        else:
            memory_used = memory_used_mib * 1024 * 1024
            memory_total = memory_total_mib * 1024 * 1024
            memory_free = (
                memory_free_mib * 1024 * 1024
                if memory_free_mib is not None
                else memory_total - memory_used
            )

        memory_percent = (memory_used / memory_total * 100) if memory_total > 0 else 0

        return {
            "available": True,
            "device_id": device_id,
            "name": gpu_name,
            "memory_total": format_bytes(memory_total),
            "memory_used": format_bytes(memory_used),
            "memory_free": format_bytes(memory_free),
            "memory_percent": f"{memory_percent:.1f}%",
            "utilization_percent": f"{utilization_percent}%"
            if utilization_percent is not None
            else "N/A",
            "temperature_celsius": temperature_celsius,
            "memory_source": memory_source,
        }
    except Exception as e:
        logger.warning(f"nvidia-smi query for device {device_id} failed: {e}")
        return None
