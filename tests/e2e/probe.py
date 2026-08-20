from __future__ import annotations

import json
import threading
import time
from typing import Any

import cgroups_sensor

MEASUREMENT_SECONDS = 1.0
"""How long the CPU rate is measured for, and therefore how long the burner below runs."""

ALLOCATION_BYTES = 32 * 1024 * 1024
"""How much memory the probe charges to its own cgroup before reading, so that the working set has a floor.

Anonymous memory, touched: nothing about it is reclaimable file cache, so a working set below this would mean
the reading is not the memory of this process.
"""


def burn(seconds: float) -> None:
    """Keep one core busy for a while.

    The rate is the one reading that says nothing when it is measured in the wrong cgroup: an idle process
    reads as zero everywhere, correct or not. So the probe makes itself busy while it measures.
    """
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        pass


def source(value: cgroups_sensor.Source | None) -> dict[str, Any] | None:
    """Spell one source as JSON."""
    return None if value is None else {'interface': value.interface, 'levels': list(value.levels)}


def main() -> None:
    # Kept alive until everything has been read. `bytearray` zero-fills, so every page is really charged.
    ballast = bytearray(ALLOCATION_BYTES)

    reading = cgroups_sensor.snapshot()

    burner = threading.Thread(target=burn, args=(MEASUREMENT_SECONDS,), daemon=True)
    burner.start()
    cpu_used_ratio = cgroups_sensor.get_cpu_used_ratio(MEASUREMENT_SECONDS)
    burner.join()

    description = cgroups_sensor.describe()

    print(
        json.dumps(
            {
                'version': cgroups_sensor.__version__,
                'memory_limit': reading.memory_budget.limit if reading.memory_budget is not None else None,
                'working_set': reading.memory_budget.working_set if reading.memory_budget is not None else None,
                'cpu_limit': reading.cpu_limit,
                'cpu_usage': reading.cpu_usage,
                'cpu_used_ratio': cpu_used_ratio,
                'raw_memory_limit': description.raw_memory_limit,
                'raw_memory_working_set': description.raw_memory_working_set,
                'raw_cpu_quota': description.raw_cpu_quota,
                'raw_cpu_set_size': description.raw_cpu_set_size,
                'memory_limit_level': description.memory_limit_level,
                'cpu_limit_level': description.cpu_limit_level,
                'cpu_rate_level': description.cpu_rate_level,
                'machine_memory_bytes': description.machine_memory_bytes,
                'machine_cpu_count': description.machine_cpu_count,
                'allocated': len(ballast),
                'notices': [notice.code for notice in description.notices],
                'sources': {
                    'memory': source(description.memory_source),
                    'cpu_quota': source(description.cpu_quota_source),
                    'cpu_set': source(description.cpu_set_source),
                    'cpu_usage': source(description.cpu_usage_source),
                },
            }
        )
    )


if __name__ == '__main__':
    main()
