from ._sensor import (
    CpuLoad,
    Description,
    Interface,
    MemoryBudget,
    Notice,
    NoticeCode,
    Snapshot,
    Source,
    clear_cache,
    describe,
    get_cpu_limit,
    get_cpu_usage,
    get_cpu_used_ratio,
    get_cpu_used_ratio_async,
    get_machine_cpu_count,
    get_machine_memory_bytes,
    get_memory_budget,
    snapshot,
)


def __getattr__(name: str) -> str:
    """Read `__version__` from the installed metadata, the first time anything asks for it.

    Reading it costs more than everything else this package does at import time, and a consumer that only
    wants the limits never asks. `importlib.metadata` is therefore imported here rather than above.
    """
    if name != '__version__':
        raise AttributeError(f'module {__name__!r} has no attribute {name!r}')

    from importlib import metadata  # noqa: PLC0415

    try:
        return metadata.version('cgroups-sensor')
    except metadata.PackageNotFoundError:
        # Imported from a source tree nothing installed, which is how the end-to-end tests run it.
        return 'unknown'


__all__ = [
    'CpuLoad',
    'Description',
    'Interface',
    'MemoryBudget',
    'Notice',
    'NoticeCode',
    'Snapshot',
    'Source',
    '__version__',
    'clear_cache',
    'describe',
    'get_cpu_limit',
    'get_cpu_usage',
    'get_cpu_used_ratio',
    'get_cpu_used_ratio_async',
    'get_machine_cpu_count',
    'get_machine_memory_bytes',
    'get_memory_budget',
    'snapshot',
]
