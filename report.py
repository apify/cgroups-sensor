from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

_BYTES_PER_GB = 1024**3
_BYTES_PER_MB = 1024**2


def _bytes_h(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return '-'
    if value >= _BYTES_PER_GB:
        return f'{value / _BYTES_PER_GB:.2f} GB'
    return f'{value / _BYTES_PER_MB:.2f} MB'


def _num(value: Any) -> str:
    if isinstance(value, float):
        return f'{value:g}'
    if isinstance(value, int):
        return str(value)
    return '-'


def _bool(value: Any) -> str:
    if value is True:
        return 'yes'
    if value is False:
        return 'no'
    return '-'


def _seconds(value: Any) -> str:
    return f'{value:.2f}s' if isinstance(value, (int, float)) else '-'


def _percent(value: Any) -> str:
    return f'{value:.0%}' if isinstance(value, (int, float)) else '-'


def _in_use(sensor: dict[str, Any]) -> str:
    """Memory in use, with its share of the limit it is charged against."""
    in_use = sensor.get('working_set_bytes')
    limit = sensor.get('memory_limit_bytes')
    if not isinstance(in_use, (int, float)):
        return '-'
    if isinstance(limit, (int, float)) and limit > 0:
        return f'{_bytes_h(in_use)} ({in_use / limit:.0%})'
    return _bytes_h(in_use)


def _status(result: dict[str, Any]) -> str:
    status = result.get('status', '?')
    if status == 'probe_failed':
        return '**PROBE FAILED**'
    if status == 'skipped':
        return f'skipped: {result.get("skip_reason", "?")}'
    return status


def _header(results: list[dict[str, Any]]) -> list[str]:
    """Identify the run: what was measured, and on what machine."""
    target = next((r.get('target') for r in results if r.get('target')), None) or {}
    host = next(((r.get('probe') or {}).get('host') for r in results if (r.get('probe') or {}).get('host')), {}) or {}

    runner = [
        f'kernel `{host.get("kernel", "?")}`',
        f'{host.get("ncpu", "?")} cpus',
        f'{_bytes_h(host.get("mem_total_bytes"))} RAM',
    ]
    # GitHub spells these two in mixed case; they name the runner image the results came from.
    image_os = os.environ.get('ImageOS')
    image_version = os.environ.get('ImageVersion')
    if image_os or image_version:
        runner.append(f'image `{image_os}/{image_version}`')

    probed = next(((r.get('probe') or {}).get('target') for r in results if (r.get('probe') or {}).get('target')), {})
    installed = ''
    if probed:
        installed = f' → crawlee {probed.get("version")} on python {probed.get("python")}'

    lines = [
        '## cgroups-sensor bench',
        '',
        f'target: `{target.get("repo", "?")}` @ `{target.get("ref", "?")}`{installed}',
        'runner: ' + ', '.join(runner),
    ]
    budget = next((r.get('cpu_budget') for r in results if r.get('cpu_budget')), None)
    if budget:
        lines.append(f'cpu budget of the limited scenarios: {budget} core(s)')

    # The engine is per scenario (the podman-* rows set their own), so it belongs in the result files rather
    # than the header. The image is one setting for the whole pass.
    image = next((r.get('image') for r in results if r.get('image')), None)
    if image:
        lines.append(f'container image: `{image}`')

    lines.append('')
    return lines


def _sensor_table(results: list[dict[str, Any]]) -> list[str]:
    """Render the sensor's readings, each next to what the scenario configured."""
    lines = [
        '### What the cgroup sensor read',
        '',
        (
            '| scenario | status | mem limit set | mem limit read | quota set | quota read | cpuset set '
            '| cpuset read | mem in use | levels | v2 | cpu time | errors |'
        ),
        '| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: | --- |',
    ]
    for result in results:
        sensor = (result.get('probe') or {}).get('sensor') or {}
        configured = result.get('configured') or {}
        levels = sensor.get('memory_levels')
        errors = (result.get('probe') or {}).get('errors') or []
        lines.append(
            f'| {result.get("scenario", "?")} | {_status(result)} '
            f'| {_bytes_h(configured.get("memory_bytes"))} | {_bytes_h(sensor.get("memory_limit_bytes"))} '
            f'| {_num(configured.get("cpu_cores"))} | {_num(sensor.get("cpu_quota_cores"))} '
            f'| {_num(configured.get("cpuset_cores"))} | {_num(sensor.get("cpu_set_cores"))} '
            f'| {_in_use(sensor)} | {len(levels) if isinstance(levels, list) else "-"} '
            f'| {_bool(sensor.get("is_v2"))} | {_seconds(sensor.get("cpu_usage_seconds"))} '
            f'| {f"{len(errors)} err" if errors else ""} |'
        )
    lines.append('')
    return lines


def _derived_table(results: list[dict[str, Any]]) -> list[str]:
    """Render what the library derives from those readings - the figures a crawler acts on."""
    lines = [
        '### What the library derives from them',
        '',
        '| scenario | memory available | used in that scope | this process | allowed cores | cpu used |',
        '| --- | ---: | ---: | ---: | ---: | ---: |',
    ]
    for result in results:
        derived = (result.get('probe') or {}).get('derived') or {}
        lines.append(
            f'| {result.get("scenario", "?")} | {_bytes_h(derived.get("total_size_bytes"))} '
            f'| {_bytes_h(derived.get("system_wide_used_bytes"))} | {_bytes_h(derived.get("current_size_bytes"))} '
            f'| {_num(derived.get("allowed_cpu_cores"))} | {_percent(derived.get("cpu_used_ratio"))} |'
        )
    lines.append('')
    return lines


_TAIL_LINES = 25


def _failures(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The rows that produced no data at all.

    A skip is not one of these: it is reported in the table with its reason, and a host that cannot host a
    scenario is a normal outcome, not a breakage.
    """
    return [result for result in results if result.get('status') == 'probe_failed']


def _failure_report(failures: list[dict[str, Any]]) -> list[str]:
    """Show what a failed row printed, so the run diagnoses itself instead of sending the reader to the artifact."""
    lines = ['### Failures', '']
    for result in failures:
        lines.append(f'**{result.get("scenario", "?")}** — probe failed, exit {result.get("exit")}')
        if result.get('probe_parse_error'):
            lines.append(f'output was not JSON: {result["probe_parse_error"]}')
        tail = (result.get('stderr_tail') or '').strip().splitlines()[-_TAIL_LINES:]
        if tail:
            lines += ['', '```', *tail, '```']
        lines.append('')
    return lines


def render(results: list[dict[str, Any]]) -> str:
    """Render one bench run as markdown."""
    lines = _header(results) + _sensor_table(results) + _derived_table(results)

    failures = _failures(results)
    if failures:
        lines += _failure_report(failures)

    return '\n'.join(lines)


def main(argv: list[str]) -> int:
    """Print the report; with `--check`, exit non-zero when a probe failed."""
    check = '--check' in argv[1:]
    positional = [arg for arg in argv[1:] if arg != '--check']
    results_dir = positional[0] if positional else 'results'

    results = []
    for path in sorted(Path(results_dir).glob('*.json')):
        with path.open() as file:
            results.append(json.load(file))

    if not results:
        print('no results found')
        return 1 if check else 0

    sys.stdout.write(render(results))

    if check and (failures := _failures(results)):
        print(f'check: {len(failures)} probe failure(s)', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
