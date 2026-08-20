from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / 'src'
PROBE = Path(__file__).parent / 'probe.py'

IMAGE = 'python:3.13-alpine'
"""The image the container tests use. uv brings the interpreter, so this one only has to be small."""

DISTRO_IMAGES = (
    'python:3.13-alpine',
    'python:3.13-slim',
    'fedora:43',
    'rockylinux:9',
)
"""Images the readings are compared across: musl, Debian glibc, and two distributions of another family."""

PYTHON_VERSION = '3.13'
"""The interpreter every probe runs on. uv installs it, so the image does not decide the version."""

PYTHON_VERSIONS = ('3.10', '3.11', '3.12', '3.13', '3.14')
"""Every interpreter the package supports, as `requires-python` spells it."""

UV_DOWNLOADS = Path(tempfile.gettempdir()) / 'cgroups-sensor-e2e-uv'
"""Where uv and its interpreters are kept, so only the first run pays for the download."""

MIB = 1024 * 1024
MEMORY_LIMIT = 512 * MIB
TIGHTER_MEMORY_LIMIT = 256 * MIB
QUOTA_CORES = 0.5
"""Half a core. Below any machine, so it is always a real restriction."""

TIMEOUT_SECONDS = 300

CANNOT_SET_UP = 77
"""What a wrapper exits with when the environment refuses to produce the shape a test needs.

GNU's convention for "skipped". A test that arranges a cgroup layout before probing has to say which of the
two happened: the layout could not be built here, or it was built and the reading is wrong. Without this the
setup fails silently and the assertion afterwards blames the sensor for it.
"""

CAPABILITIES = frozenset({'docker', 'sudo', 'systemd', 'kubernetes'})
"""What a lane can be told to prove. `E2E_REQUIRE` is checked against this, so a typo cannot quietly disarm it."""

REQUIRED_CAPABILITIES = frozenset(name for name in os.environ.get('E2E_REQUIRE', '').split(',') if name)
"""What this run must actually exercise, e.g. `E2E_REQUIRE=docker,systemd`.

A test skips where its environment is missing, which is what makes one suite run everywhere. In CI that turns
a broken runner into a green job, so each lane names what it is there for and a missing capability fails.
"""


def unavailable(capability: str, reason: str) -> None:
    """Skip because the environment cannot do this, or fail when this run was supposed to prove it."""
    if capability in REQUIRED_CAPABILITIES:
        pytest.fail(f'{reason}, and E2E_REQUIRE names {capability}')

    pytest.skip(reason)


@dataclass(frozen=True)
class Reading:
    """What the probe saw inside the environment under test."""

    version: str
    memory_limit: int | None
    working_set: int | None
    cpu_limit: float | None
    cpu_usage: float | None
    cpu_used_ratio: float | None
    raw_memory_limit: int | None
    raw_memory_working_set: int | None
    raw_cpu_quota: float | None
    raw_cpu_set_size: int | None
    memory_limit_level: str | None
    cpu_limit_level: str | None
    cpu_rate_level: str | None
    machine_memory_bytes: int | None
    machine_cpu_count: int | None
    allocated: int
    notices: tuple[str, ...]
    sources: dict[str, Any]

    @property
    def interfaces(self) -> set[str]:
        """The mechanisms the readings came from, e.g. `{'cgroup-v1'}`."""
        return {source['interface'] for source in self.sources.values() if source is not None}

    @property
    def limit_interfaces(self) -> set[str]:
        """The mechanisms the limits came from.

        The consumed CPU time is left out on purpose. It comes from the hierarchy carrying the limits
        wherever that hierarchy counts anything, but where nothing does, the base `cpu.stat` of a
        controller-less cgroup2 is all there is - and that is not a reason to fail a lane.
        """
        return {
            source['interface'] for name, source in self.sources.items() if name != 'cpu_usage' and source is not None
        }

    def __repr__(self) -> str:
        """Spell the numbers a failing assertion needs, with the sources summarized rather than dumped."""
        fields = ', '.join(
            f'{name}={getattr(self, name)!r}'
            for name in (
                'memory_limit',
                'working_set',
                'cpu_limit',
                'cpu_usage',
                'cpu_used_ratio',
                'raw_memory_limit',
                'raw_memory_working_set',
                'raw_cpu_quota',
                'raw_cpu_set_size',
                'memory_limit_level',
                'cpu_limit_level',
                'cpu_rate_level',
                'machine_memory_bytes',
                'machine_cpu_count',
                'notices',
            )
        )
        levels = {name: (source or {}).get('levels') for name, source in self.sources.items()}

        return f'Reading({fields}, interfaces={sorted(self.interfaces)}, levels={levels})'


def parse(output: str) -> Reading:
    """Read the probe's JSON out of its output."""
    lines = [line for line in output.splitlines() if line.startswith('{')]
    if not lines:
        pytest.fail(f'the probe printed no JSON:\n{output}')

    payload = json.loads(lines[-1])
    payload['notices'] = tuple(payload['notices'])

    return Reading(**payload)


def run(command: list[str], *, env: dict[str, str] | None = None) -> Reading:
    """Run one probe command and parse what it printed."""
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        env=env,
        check=False,
    )

    if result.returncode == CANNOT_SET_UP:
        pytest.skip(f'the environment cannot set this up: {result.stderr.strip()[-300:]}')

    if result.returncode != 0:
        pytest.fail(
            f'{" ".join(command)} exited {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}'
        )

    return parse(result.stdout)


def probe_here(wrapper: list[str] | None = None, python_version: str | None = None) -> Reading:
    """Run the probe on this machine, optionally under a wrapper command such as `systemd-run`.

    The interpreter is the one running the suite, unless a version is named. uv then installs that one.
    """
    env = {**os.environ, 'PYTHONPATH': str(SRC_DIR)}

    if python_version is None:
        interpreter = [sys.executable, str(PROBE)]
    else:
        interpreter = ['uv', 'run', '--no-project', '--python', python_version, 'python', str(PROBE)]

    if wrapper is None:
        return run(interpreter, env=env)

    # A wrapper can strip the environment, so the variables travel as an explicit `env` call.
    passthrough = ['env', f'PYTHONPATH={SRC_DIR}', f'HOME={os.environ.get("HOME", "/root")}']

    return run([*wrapper, *passthrough, *interpreter], env=env)


def probe_command(python_version: str = PYTHON_VERSION) -> str:
    """Spell how the probe is started inside a container.

    uv brings the interpreter, so the image decides neither the version nor whether one exists at all.
    """
    return f'/uv/uv run --no-project --python {python_version} python /sensor/probe.py'


@lru_cache(maxsize=1)
def portable_uv() -> Path:
    """Download the uv build that runs in any image, and hand back its path.

    The musl build is statically linked, so one binary covers musl and glibc images alike. The version follows
    the uv of this machine, so the tests use the uv the project is developed with.
    """
    target = UV_DOWNLOADS / 'uv'
    if target.exists():
        return target

    # `uv --version`, not `uv version`: the latter reports the version of the project it is run in.
    reported = subprocess.run(
        ['uv', '--version'],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    ).stdout.split()
    if len(reported) < 2:
        unavailable('docker', 'the uv version of this machine cannot be read')

    version = reported[1]

    machine = 'aarch64' if platform.machine() in {'aarch64', 'arm64'} else 'x86_64'
    url = f'https://github.com/astral-sh/uv/releases/download/{version}/uv-{machine}-unknown-linux-musl.tar.gz'

    UV_DOWNLOADS.mkdir(parents=True, exist_ok=True)
    archive = UV_DOWNLOADS / 'uv.tar.gz'
    if not command_works(['curl', '-fsSL', '-o', str(archive), url]):
        unavailable('docker', f'{url} cannot be downloaded')

    with tarfile.open(archive) as tar:
        member = next(entry for entry in tar.getmembers() if entry.name.endswith('/uv'))
        member.name = 'uv'
        tar.extract(member, UV_DOWNLOADS, filter='data')

    target.chmod(0o755)

    return target


def probe_in_container(
    *docker_args: str,
    image: str = IMAGE,
    python_version: str = PYTHON_VERSION,
    command: list[str] | None = None,
) -> Reading:
    """Run the probe in a fresh container. The arguments go to `docker run`."""
    cache = UV_DOWNLOADS / 'cache'
    interpreters = UV_DOWNLOADS / 'python'
    cache.mkdir(parents=True, exist_ok=True)
    interpreters.mkdir(parents=True, exist_ok=True)

    return run(
        [
            'docker',
            'run',
            '--rm',
            '--volume',
            f'{SRC_DIR}:/sensor/src:ro',
            '--volume',
            f'{PROBE}:/sensor/probe.py:ro',
            '--volume',
            f'{portable_uv()}:/uv/uv:ro',
            '--volume',
            f'{cache}:/uv/cache',
            '--volume',
            f'{interpreters}:/uv/python',
            '--env',
            'PYTHONPATH=/sensor/src',
            '--env',
            'UV_CACHE_DIR=/uv/cache',
            '--env',
            'UV_PYTHON_INSTALL_DIR=/uv/python',
            *docker_args,
            image,
            *(command or ['sh', '-c', probe_command(python_version)]),
        ]
    )


def pull_image(image: str) -> bool:
    """Pull one image, and report whether it arrived.

    Pulling gets its own generous timeout: inside a virtual machine the network is slow enough that the
    default one would report a working registry as a missing image.
    """
    return command_works(['docker', 'pull', '--quiet', image], timeout=TIMEOUT_SECONDS)


def have(tool: str) -> bool:
    """Whether a command exists on this machine."""
    return shutil.which(tool) is not None


def command_works(command: list[str], *, timeout: int = 60) -> bool:
    """Whether a command runs and succeeds."""
    try:
        return (
            subprocess.run(
                command,
                capture_output=True,
                timeout=timeout,
                check=False,
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


def machine_cpu_count() -> int:
    """The cores of this machine, counted independently of the package.

    The package answers this with `get_machine_cpu_count()`. This one deliberately does not use it: a test
    that asks the subject for the expected value proves nothing.
    """
    return os.sysconf('SC_NPROCESSORS_ONLN')


def is_unified() -> bool:
    """Whether the controllers live on the cgroup v2 unified hierarchy."""
    controllers = Path('/sys/fs/cgroup/cgroup.controllers')

    # Every file in cgroupfs reports zero bytes, so the content has to be read rather than sized.
    return controllers.exists() and bool(controllers.read_text().strip())


def machine_memory_bytes() -> int:
    """The memory of this machine, in bytes. Read here rather than asked of the package, as above."""
    for line in Path('/proc/meminfo').read_text().splitlines():
        if line.startswith('MemTotal:'):
            return int(line.split()[1]) * 1024

    pytest.skip('/proc/meminfo carries no MemTotal')


def notices_about(reading: Reading, metric: str) -> list[str]:
    """The notices that explain a dropped reading of one metric.

    A notice of the other metric is no explanation, so the two are told apart here rather than at each call
    site. The machine facts belong to the metric they were compared against.
    """
    prefixes = {'memory': ('memory', 'machine-memory'), 'cpu': ('cpu',)}[metric]

    return [code for code in reading.notices if code.startswith(prefixes)]


def check_invariants(reading: Reading) -> None:
    """Check what must hold of every reading, whatever the environment.

    Called by every test on top of its own assertions. A reported limit has to be real, and a missing one has
    to be explained.
    """
    # The probe reads `__version__`, which the package resolves lazily on first access. Nothing else in the
    # suite touches that path, and a source tree nothing installed reports `unknown` rather than nothing.
    assert reading.version

    if reading.memory_limit is not None:
        assert reading.working_set is not None
        # The probe charged this much anonymous memory to its own cgroup before reading, so a working set
        # below it is not the memory of this process - a raw counter, a stale file, or another cgroup.
        assert reading.allocated <= reading.working_set <= reading.memory_limit
        assert reading.machine_memory_bytes is not None
        assert reading.memory_limit < reading.machine_memory_bytes
    else:
        # Nothing was reported, so either no limit was found or a notice says why it was dropped.
        assert reading.raw_memory_limit is None or notices_about(reading, 'memory')

    if reading.raw_memory_limit is not None:
        # Whatever became of the limit, the level it was read at is one of the levels that were searched.
        assert reading.memory_limit_level in reading.sources['memory']['levels']
        if reading.raw_memory_working_set is not None:
            assert 0 <= reading.raw_memory_working_set <= reading.raw_memory_limit

    if reading.cpu_limit is not None:
        assert reading.cpu_limit > 0
        if reading.machine_cpu_count is not None:
            assert reading.cpu_limit < reading.machine_cpu_count
        # A limit whose level counts no CPU time is still reported, and then says so instead of a rate.
        if reading.cpu_used_ratio is None:
            assert 'cpu-usage-scope-mismatch' in reading.notices
        else:
            # The probe keeps a core busy while it measures, so a rate of nothing means the time was counted
            # somewhere this process is not. The level it was counted at is named next to the failure.
            assert 0.0 < reading.cpu_used_ratio <= 1.0
            assert reading.cpu_rate_level is not None
        assert reading.cpu_limit_level is not None
    else:
        assert reading.cpu_used_ratio is None
        raw_cpu = (reading.raw_cpu_quota, reading.raw_cpu_set_size)
        assert raw_cpu == (None, None) or notices_about(reading, 'cpu')
