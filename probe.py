from __future__ import annotations

import json
import os
import sys
from importlib.metadata import version
from pathlib import Path
from typing import Any

SCHEMA = 1

_V2_EVIDENCE_FILES = (
    'memory.max',
    'memory.current',
    'cpu.max',
    'cpu.weight',
    'cpuset.cpus.effective',
    'cgroup.controllers',
)

_V1_EVIDENCE_FILES = (
    'memory.limit_in_bytes',
    'memory.usage_in_bytes',
    'cpu.cfs_quota_us',
    'cpu.cfs_period_us',
    'cpuacct.usage',
    'cpuset.cpus',
)

_V1_CONTROLLERS = ('memory', 'cpu', 'cpuacct', 'cpuset')

_MAX_LEVELS = 20
"""How far up the cgroup chain the evidence dump walks. Deeper than any real hierarchy, so it only bounds a loop."""

_FILE_CAP_CHARS = 200

errors: list[str] = []


def _guard(where: str, read: Any, default: Any = None) -> Any:
    """Run a reading that touches the library under test, recording rather than raising when it breaks.

    The probe imports private APIs from a moving branch. A rename there should degrade one column to null, not
    take the whole measurement down with it.
    """
    try:
        return read()
    except Exception as exc:
        errors.append(f'{where}: {type(exc).__name__}: {exc}')
        return default


def _host() -> dict[str, Any]:
    """Host facts, read directly - a cpuset only means something next to the machine's core count."""
    mem_total = None
    try:
        for line in Path('/proc/meminfo').read_text().splitlines():
            if line.startswith('MemTotal:'):
                mem_total = int(line.split()[1]) * 1024
                break
    except OSError:
        pass
    return {'kernel': os.uname().release, 'ncpu': os.cpu_count(), 'mem_total_bytes': mem_total}


def _read_control_file(path: Path) -> str | None:
    """Read one cgroup control file, or None when it does not exist at this level."""
    try:
        return path.read_text()[:_FILE_CAP_CHARS].strip()
    except OSError:
        return None


def _walk_levels(mount: Path, own_path: str, names: tuple[str, ...]) -> list[dict[str, Any]]:
    """Read the named control files at the process's own cgroup and at every level above it, leaf first."""
    levels = []
    directory = mount / own_path.lstrip('/') if '..' not in own_path else mount
    for _ in range(_MAX_LEVELS):
        files = {name: content for name in names if (content := _read_control_file(directory / name))}
        levels.append({'path': str(directory), 'files': files})
        if directory in (mount, directory.parent):
            break
        directory = directory.parent
    return levels


def _evidence() -> dict[str, Any]:
    """Dump raw cgroup file contents along the chain, verbatim - receipts, not a verdict, never interpreted.

    Both cgroup versions are dumped the same way, from the standard mount points: the unified hierarchy sits at
    /sys/fs/cgroup, while a v1 controller has a directory of its own under it. Resolving mounts properly is the
    library's job, and this deliberately does not repeat it - the receipts only have to be readable next to the
    values the library reported.
    """
    evidence: dict[str, Any] = {'proc_cgroup': None, 'levels': []}
    try:
        evidence['proc_cgroup'] = Path('/proc/self/cgroup').read_text().strip()
    except OSError:
        return evidence

    mount = Path('/sys/fs/cgroup')
    seen: set[str] = set()

    for line in evidence['proc_cgroup'].splitlines():
        parts = line.split(':', 2)
        if len(parts) != 3:
            continue
        _hierarchy_id, controllers, own_path = parts

        if not controllers:
            levels = _walk_levels(mount, own_path, _V2_EVIDENCE_FILES)
        else:
            # A v1 line can name several controllers sharing one mount, as `cpu,cpuacct` usually does.
            names = [name for name in controllers.split(',') if name in _V1_CONTROLLERS]
            levels = [
                level
                for name in names
                for level in _walk_levels(mount / name, own_path, _V1_EVIDENCE_FILES)
            ]

        for level in levels:
            if level['path'] not in seen:
                seen.add(level['path'])
                evidence['levels'].append(level)

    return evidence


def _sensor() -> dict[str, Any]:
    """Collect everything `crawlee._utils.cgroup` reports for this process."""
    # Imported here rather than at module level: a rename on the branch under test must be recorded as an entry
    # in `errors`, not kill the probe before it can report anything at all.
    from crawlee._utils import cgroup

    readings: dict[str, Any] = {
        'is_v2': None,
        'memory_levels': None,
        'memory_limit_bytes': None,
        'working_set_bytes': None,
        'cpu_quota_cores': _guard('get_cpu_quota', cgroup.get_cpu_quota),
        'cpu_set_cores': _guard('get_cpu_set_size', cgroup.get_cpu_set_size),
        'cpu_usage_seconds': _guard('get_cpu_usage', cgroup.get_cpu_usage),
    }

    memory_limit = _guard('get_memory_limit', cgroup.get_memory_limit)
    if memory_limit is not None:
        readings['memory_limit_bytes'] = memory_limit.limit
        readings['working_set_bytes'] = memory_limit.working_set

    def discovery() -> dict[str, Any]:
        memory = cgroup._get_controllers().memory
        return {
            'is_v2': memory.is_v2 if memory is not None else None,
            'memory_levels': [str(directory) for directory in memory.dirs] if memory is not None else None,
        }

    readings.update(_guard('_get_controllers', discovery, default={}))
    return readings


def _derived() -> dict[str, Any]:
    """Collect what `crawlee._utils.system` makes of those readings - the figures a crawler acts on.

    `get_memory_info()` substitutes the cgroup limit and its working set for the host totals whenever a limit
    applies, and `get_cpu_info()` measures utilization against the cores the cgroup allows instead of the whole
    machine. Where no limit is found both fall back to host-wide values, so these fields also show what a missed
    limit would cost.
    """
    from crawlee._utils.system import get_cpu_info, get_memory_info

    memory = _guard('get_memory_info', get_memory_info)
    cpu = _guard('get_cpu_info', get_cpu_info)

    def allowed_cores() -> float | None:
        from crawlee._utils.system import _get_allowed_cpu_cores

        return _get_allowed_cpu_cores()

    return {
        'total_size_bytes': memory.total_size.bytes if memory is not None else None,
        'system_wide_used_bytes': memory.system_wide_used_size.bytes if memory is not None else None,
        'current_size_bytes': memory.current_size.bytes if memory is not None else None,
        'allowed_cpu_cores': _guard('_get_allowed_cpu_cores', allowed_cores),
        'cpu_used_ratio': round(cpu.used_ratio, 3) if cpu is not None else None,
    }


def main() -> None:
    """Print one JSON object describing what this environment looks like to Crawlee."""
    source = sys.argv[sys.argv.index('--source') + 1] if '--source' in sys.argv else None

    report = {
        'schema': SCHEMA,
        'target': {
            'name': 'crawlee-python',
            'version': _guard('version', lambda: version('crawlee')),
            'source': source,
            'python': sys.version.split()[0],
        },
        'host': _host(),
        'evidence': _guard('evidence', _evidence, default={}),
        'sensor': _sensor(),
        'derived': _guard('derived', _derived, default={}),
        'errors': errors,
    }

    json.dump(report, sys.stdout, indent=1)
    sys.stdout.write('\n')


if __name__ == '__main__':
    main()
