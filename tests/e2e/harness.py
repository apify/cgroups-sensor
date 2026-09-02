from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / 'src'
PROBE = Path(__file__).parent / 'probe.py'

IMAGE = 'python:3.13-alpine'
"""The image the container tests use. uv brings the interpreter, so this one only has to be small."""

DOCKER_HUB = 'docker.io'
"""The registry the images above live in, for the engines that do not assume one."""


@dataclass(frozen=True)
class Engine:
    """A container engine, and how a test reaches it."""

    name: str
    """What this engine's uv cache is kept under. A rootful engine writes into it as real root, which a
    rootless one cannot then use."""

    command: tuple[str, ...]
    """How the engine is started, `sudo` included where the containers have to be root's."""

    registry: str | None = None
    """The registry to name in front of an image, or `None` where the engine assumes one. Podman refuses a
    bare name that several of them could answer."""


DOCKER = Engine(name='docker', command=('docker',))
PODMAN = Engine(name='podman', command=('podman',), registry=DOCKER_HUB)
ROOTFUL_PODMAN = Engine(name='podman-root', command=('sudo', 'podman'), registry=DOCKER_HUB)

DEBIAN_IMAGE = 'python:3.13-slim'
"""For the tests whose setup needs a tool the busybox of an alpine image does not carry, such as `unshare`."""

DISTRO_IMAGES = (
    IMAGE,
    DEBIAN_IMAGE,
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
"""What a wrapper exits with when it could not produce the shape a test needs.

Both fail the run, but differently: the layout could not be built, or it was built and the reading is wrong.
Without this the setup breaks silently and the assertion afterwards blames the sensor for it.
"""


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

        The consumed CPU time is left out. Where no hierarchy counts anything, the base `cpu.stat` of a
        controller-less cgroup2 is all there is.
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


def attempt(command: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Run one probe command and hand back how it went, whether or not it worked.

    For the tests that assert on a refusal. Everything else goes through `run`.
    """
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        env=env,
        check=False,
    )


def run(command: list[str], *, env: dict[str, str] | None = None) -> Reading:
    """Run one probe command and parse what it printed."""
    result = attempt(command, env=env)

    if result.returncode == CANNOT_SET_UP:
        pytest.fail(f'the layout this test needs could not be built: {result.stderr.strip()[-300:]}')

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


@cache
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
        pytest.fail('the uv version of this machine cannot be read')

    version = reported[1]

    machine = 'aarch64' if platform.machine() in {'aarch64', 'arm64'} else 'x86_64'
    url = f'https://github.com/astral-sh/uv/releases/download/{version}/uv-{machine}-unknown-linux-musl.tar.gz'

    UV_DOWNLOADS.mkdir(parents=True, exist_ok=True)
    archive = UV_DOWNLOADS / 'uv.tar.gz'
    if not command_works(['curl', '-fsSL', '-o', str(archive), url]):
        pytest.fail(f'{url} cannot be downloaded')

    with tarfile.open(archive) as tar:
        member = next(entry for entry in tar.getmembers() if entry.name.endswith('/uv'))
        member.name = 'uv'
        tar.extract(member, UV_DOWNLOADS, filter='data')

    target.chmod(0o755)

    return target


def container_command(
    *engine_args: str,
    engine: Engine = DOCKER,
    image: str = IMAGE,
    python_version: str = PYTHON_VERSION,
    command: list[str] | None = None,
) -> list[str]:
    """Spell the `run` command that starts the probe in a fresh container.

    Built rather than run, so that a test can also assert on an engine that refuses to start it.
    """
    uv_cache = UV_DOWNLOADS / engine.name / 'cache'
    interpreters = UV_DOWNLOADS / engine.name / 'python'
    uv_cache.mkdir(parents=True, exist_ok=True)
    interpreters.mkdir(parents=True, exist_ok=True)

    return [
        *engine.command,
        'run',
        '--rm',
        '--volume',
        f'{SRC_DIR}:/sensor/src:ro',
        '--volume',
        f'{PROBE}:/sensor/probe.py:ro',
        '--volume',
        f'{portable_uv()}:/uv/uv:ro',
        '--volume',
        f'{uv_cache}:/uv/cache',
        '--volume',
        f'{interpreters}:/uv/python',
        '--env',
        'PYTHONPATH=/sensor/src',
        '--env',
        'UV_CACHE_DIR=/uv/cache',
        '--env',
        'UV_PYTHON_INSTALL_DIR=/uv/python',
        *engine_args,
        qualified(image, engine),
        *(command or ['sh', '-c', probe_command(python_version)]),
    ]


def probe_in_container(
    *engine_args: str,
    engine: Engine = DOCKER,
    image: str = IMAGE,
    python_version: str = PYTHON_VERSION,
    command: list[str] | None = None,
) -> Reading:
    """Run the probe in a fresh container. The arguments go to the engine's `run`."""
    return run(
        container_command(
            *engine_args,
            engine=engine,
            image=image,
            python_version=python_version,
            command=command,
        )
    )


def qualified(image: str, engine: Engine) -> str:
    """Name the registry of an image, for an engine that does not assume one.

    A name that carries one already is left alone: that is a first component with a dot or a colon in it.
    """
    first, slash, _rest = image.partition('/')
    carries_registry = bool(slash) and ('.' in first or ':' in first or first == 'localhost')

    return image if engine.registry is None or carries_registry else f'{engine.registry}/{image}'


def pull_image(image: str, engine: Engine = DOCKER) -> None:
    """Pull one image ahead of the run that needs it, whether or not it arrives.

    Pulling separately gets its own generous timeout, which a slow guest network needs. A failure is no
    verdict: the image may already be here, and where it is not, the `run` that follows says so.
    """
    command_works([*engine.command, 'pull', '--quiet', qualified(image, engine)], timeout=TIMEOUT_SECONDS)


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

    Not the `get_machine_cpu_count()` of the package: a test that asks the subject for the expected value
    proves nothing.
    """
    return os.sysconf('SC_NPROCESSORS_ONLN')


def is_unified() -> bool:
    """Whether the controllers live on the cgroup v2 unified hierarchy."""
    controllers = Path('/sys/fs/cgroup/cgroup.controllers')

    # Every file in cgroupfs reports zero bytes, so the content has to be read rather than sized.
    return controllers.exists() and bool(controllers.read_text().strip())


def delegated_controllers() -> frozenset[str]:
    """The controllers the systemd user manager of this user may hand out.

    Everything a rootless engine starts lands below `user@<uid>.service`, and can be limited only by a
    controller delegated to that unit - which is what its `cgroup.controllers` lists, and not its
    `cgroup.subtree_control`, which is only what the manager has enabled for children so far. systemd 255
    delegates `cpu`, `memory` and `pids`, and not `cpuset`.

    The unit is named from the uid, not read out of `/proc/self/cgroup`: the process asking is not under the
    manager itself. Empty where this user has no manager running.
    """
    uid = os.getuid()
    unit = Path(f'/sys/fs/cgroup/user.slice/user-{uid}.slice/user@{uid}.service')

    try:
        return frozenset((unit / 'cgroup.controllers').read_text().split())
    except OSError:
        return frozenset()


def machine_memory_bytes() -> int:
    """The memory of this machine, in bytes. Read here rather than asked of the package, as above."""
    for line in Path('/proc/meminfo').read_text().splitlines():
        if line.startswith('MemTotal:'):
            return int(line.split()[1]) * 1024

    pytest.fail('/proc/meminfo carries no MemTotal')


def notices_about(reading: Reading, metric: str) -> list[str]:
    """The notices that explain a dropped reading of one metric.

    A notice of the other metric is no explanation, so the two are told apart here.
    """
    prefixes = {'memory': ('memory', 'machine-memory'), 'cpu': ('cpu',)}[metric]

    return [code for code in reading.notices if code.startswith(prefixes)]


def check_invariants(reading: Reading) -> None:
    """Check what must hold of every reading, whatever the environment.

    A reported limit has to be real, and a missing one has to be explained.
    """
    # `__version__` resolves lazily on first access, and nothing else in the suite touches that path.
    assert reading.version

    if reading.memory_limit is not None:
        assert reading.working_set is not None
        # The probe charged this much to its own cgroup first, so a smaller working set is not this process.
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
            # The probe keeps a core busy while it measures, so a rate of nothing was counted elsewhere.
            assert 0.0 < reading.cpu_used_ratio <= 1.0
            assert reading.cpu_rate_level is not None
        assert reading.cpu_limit_level is not None
    else:
        assert reading.cpu_used_ratio is None
        raw_cpu = (reading.raw_cpu_quota, reading.raw_cpu_set_size)
        assert raw_cpu == (None, None) or notices_about(reading, 'cpu')
