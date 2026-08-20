from __future__ import annotations


def count_cpu_list(cpu_list: str) -> int | None:
    """Count the CPUs in a list of ranges and single numbers, e.g. `0-3,8`.

    The kernel writes this format in more than one place: `cpuset.cpus` inside a cgroup, and
    `/sys/devices/system/cpu/online` for the machine. It belongs to neither layer, so it lives here.

    Returns:
        The number of cores, or `None` when the list does not parse. A reversed range does not parse: a real
        kernel never writes one, but an emulated cgroupfs can, and counting it would yield zero cores.
    """
    count = 0

    try:
        for part in cpu_list.split(','):
            first, separator, last = part.partition('-')
            # A single core carries no separator. One that carries it has to carry both ends too.
            if separator and not last:
                return None

            span = int(last or first) - int(first) + 1
            if span <= 0:
                return None
            count += span
    except ValueError:
        return None

    return count
