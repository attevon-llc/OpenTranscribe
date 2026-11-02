"""Duration and size formatters."""


def fmt_duration(seconds: float) -> str:
    """Format seconds into human-readable duration."""
    if seconds < 60:
        return f'{seconds:.1f}s'
    m, s = divmod(int(seconds), 60)
    if m < 60:
        return f'{m}m{s:02d}s'
    h, m = divmod(m, 60)
    return f'{h}h{m:02d}m{s:02d}s'


def fmt_size(bytes_val: int) -> str:
    """Format bytes into human-readable size."""
    if bytes_val < 1024:
        return f'{bytes_val}B'
    if bytes_val < 1024**2:
        return f'{bytes_val / 1024:.1f}KB'
    if bytes_val < 1024**3:
        return f'{bytes_val / (1024**2):.1f}MB'
    return f'{bytes_val / (1024**3):.2f}GB'
