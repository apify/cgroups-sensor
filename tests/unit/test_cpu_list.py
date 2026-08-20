from __future__ import annotations

import pytest

from cgroups_sensor._cpu_list import count_cpu_list


@pytest.mark.parametrize(
    ('cpu_list', 'expected'),
    [
        pytest.param('0-3', 4, id='a range'),
        pytest.param('0-1,4,6-7', 5, id='ranges mixed with single cores'),
        pytest.param('7', 1, id='a single core'),
        pytest.param('5-4', None, id='a reversed range counts nothing, so it is not a count'),
        pytest.param('3-1', None, id='a range reversed by more than one'),
        pytest.param('0-,2', None, id='an unfinished range'),
        pytest.param('nonsense', None, id='not a number'),
    ],
)
def test_count_cpu_list(cpu_list: str, expected: int | None) -> None:
    """Counts a CPU list, and refuses one that does not describe a set of cores.

    A real kernel writes neither of the refused shapes. An emulated cgroupfs can, and a count of zero would
    become a limit of no cores that every consumer then divides by.
    """
    assert count_cpu_list(cpu_list) == expected
