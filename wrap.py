from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

SCHEMA = 1

_STDERR_TAIL_CHARS = 8000
_CONSOLE_TAIL_LINES = 15


def _read_tail(path: str | None, limit: int = _STDERR_TAIL_CHARS) -> str:
    if not path:
        return ''
    try:
        return Path(path).read_text(errors='replace')[-limit:]
    except OSError:
        return ''


def _parse_args(argv: list[str]) -> dict[str, str]:
    """Parse `--key value` and `--flag` pairs; run.sh is the only caller."""
    args: dict[str, str] = {}
    index = 0
    while index < len(argv):
        arg = argv[index]
        if not arg.startswith('--'):
            raise SystemExit(f'wrap.py: unexpected argument {arg!r}')
        key = arg.removeprefix('--')
        if index + 1 < len(argv) and not argv[index + 1].startswith('--'):
            args[key] = argv[index + 1]
            index += 2
        else:
            args[key] = '1'
            index += 1
    return args


def main(argv: list[str]) -> int:
    """Write one scenario's result JSON and print its status line."""
    args = _parse_args(argv[1:])

    probe: Any = None
    parse_error = None
    if args.get('probe-file'):
        try:
            probe = json.loads(Path(args['probe-file']).read_text())
        except (OSError, json.JSONDecodeError) as exc:
            parse_error = str(exc)

    result: dict[str, Any] = {
        'schema': SCHEMA,
        'scenario': args['scenario'],
        'desc': args.get('desc', ''),
        'cpu_budget': os.environ.get('BENCH_CPUS'),
        'engine': os.environ.get('ENGINE'),
        'image': os.environ.get('IMG'),
        # What the scenario configured. A None means it restricts nothing on that axis, so the sensor reporting
        # nothing there is the right answer rather than a miss.
        'configured': {
            'memory_bytes': int(args['set-memory']) if args.get('set-memory') else None,
            'cpu_cores': float(args['set-cpu']) if args.get('set-cpu') else None,
            'cpuset_cores': int(args['set-cpuset']) if args.get('set-cpuset') else None,
        },
        'target': json.loads(args.get('target-json', 'null')),
        'probe': probe,
        'exit': int(args['exit-rc']) if args.get('exit-rc') else None,
        'stderr_tail': _read_tail(args.get('stderr-file')),
        'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
    }

    if args.get('skip-reason'):
        result['status'] = 'skipped'
        result['skip_reason'] = args['skip-reason']
    elif result['exit'] == 0 and probe is not None:
        result['status'] = 'ok'
    else:
        result['status'] = 'probe_failed'
        if parse_error:
            result['probe_parse_error'] = parse_error

    out = Path(args['out'])
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open('w') as file:
        json.dump(result, file, indent=1)
        file.write('\n')

    detail = result.get('skip_reason', '')
    if result['status'] == 'probe_failed':
        detail = f'exit {result["exit"]}' + (f', {parse_error}' if parse_error else '')
    print(f'{args["scenario"]}: {result["status"]}' + (f' ({detail})' if detail else ''))

    # A failure is worth reading where it happens, not only in the artifact.
    if result['status'] == 'probe_failed':
        for line in result['stderr_tail'].strip().splitlines()[-_CONSOLE_TAIL_LINES:]:
            print(f'  | {line}')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
