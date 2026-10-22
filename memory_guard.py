"""Keep a simulation run's memory use under a budget, so a big run can't exhaust the computer's memory
but still finishes with every result and every chart point.

Every run keeps each result's chart data (price, model value and indicator series), and a stress test
of many models makes hundreds of thousands of results: enough to fill the memory and stop the computer
responding. A MemoryGuard gives the run a budget, by default 80% of the memory that was free when the
simulator launched, and measures the run against it as the fall in free memory since then (so the
worker processes count too). As the run uses more of its budget, the guard steps in:

    share of the budget   chart series                          tasks running at once
    under 60%             kept in memory                        every worker
    60% and over          written to disk (data/chart_spill/)   every worker
    80% and over          on disk                               half the workers
    90% and over          on disk                               a quarter of them
    at the budget         on disk                               one

A spilled result keeps a SpilledSeries in place of its series: it reads the file when a chart or table
first needs it, and the SPILL_CACHE most recently read stay in memory. Nothing is thinned and no task
is cancelled: the run goes on, more slowly, and every chart keeps every point.

Free memory is read from the operating system (no extra package is needed): GlobalMemoryStatusEx on
Windows, /proc/meminfo on Linux, sysconf elsewhere. Where it can't be read, the guard does nothing.
"""

import collections
import collections.abc
import ctypes
import gc
import os
import pickle
import shutil
import sys
import time
from pathlib import Path

import numpy as np

DEFAULT_PERCENT = 80
SPILL_AT = 0.6  # share of the budget from which chart series are written to disk
# (share of the budget, share of the workers that may run at once), highest first.
THROTTLE = [(1.0, 0.0), (0.9, 0.25), (0.8, 0.5)]
SPILL_ROOT = Path(__file__).parent / "data" / "chart_spill"
SPILL_CACHE = 16  # spilled series kept in memory once read: enough for a chart comparing several models


def available_memory():
    """Bytes of physical memory free now, or None where it can't be read."""
    if sys.platform == "win32":
        class MemoryStatus(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total_physical", ctypes.c_ulonglong),
                        ("available_physical", ctypes.c_ulonglong), ("total_page_file", ctypes.c_ulonglong),
                        ("available_page_file", ctypes.c_ulonglong), ("total_virtual", ctypes.c_ulonglong),
                        ("available_virtual", ctypes.c_ulonglong), ("available_extended", ctypes.c_ulonglong)]
        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        return status.available_physical if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)) else None
    try:
        with open("/proc/meminfo", encoding="ascii") as meminfo:
            for line in meminfo:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    try:
        return os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, OSError, ValueError):
        return None


def format_bytes(count):
    return f"{count / 2 ** 30:.1f} GB"


# --- chart series on disk --------------------------------------------------------------------------------

_loaded = collections.OrderedDict()  # path -> series, the SPILL_CACHE most recently read


def _pack(values):
    """(array, was a list) for one series: a compact array on disk, the list given back as a list."""
    return np.asarray(values), isinstance(values, list)


def _unpack(packed):
    array, was_list = packed
    return array.tolist() if was_list else array


class SpilledSeries(collections.abc.Mapping):
    """A result's chart series, kept in a file: reads like the dict it replaced, loading it when first used."""

    def __init__(self, path):
        self.path = str(path)

    def _series(self):
        if self.path in _loaded:
            _loaded.move_to_end(self.path)
            return _loaded[self.path]
        with open(self.path, "rb") as handle:
            packed = pickle.load(handle)
        series = {key: ({name: _unpack(line) for name, line in value.items()} if key == "indicators"
                        else _unpack(value)) for key, value in packed.items()}
        _loaded[self.path] = series
        while len(_loaded) > SPILL_CACHE:
            _loaded.popitem(last=False)
        return series

    def __getitem__(self, key):
        return self._series()[key]

    def __iter__(self):
        return iter(self._series())

    def __len__(self):
        return len(self._series())


def spill_series(result, folder, number):
    """Write the result's chart series to `folder` and put a SpilledSeries in its place. Returns True if it did."""
    series = result.get("series")
    if not isinstance(series, dict) or not series:
        return False  # none, or already on disk
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{number:07d}.pkl"
    packed = {key: ({name: _pack(line) for name, line in value.items()} if key == "indicators" else _pack(value))
              for key, value in series.items()}
    with open(path, "wb") as handle:
        pickle.dump(packed, handle, protocol=pickle.HIGHEST_PROTOCOL)
    result["series"] = SpilledSeries(path)
    return True


def _process_alive(pid):
    """Whether process `pid` is running (os.kill(pid, 0) would send CTRL_C_EVENT on Windows, so not that)."""
    if pid == os.getpid():
        return True
    if sys.platform == "win32":
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        alive = bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
        kernel32.CloseHandle(handle)
        return alive
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def clean_spills(keep=(), root=None):
    """Delete the spill folders of earlier runs, except those in `keep` (whose results are still shown) and
    those of other simulator windows still open (each folder is named after its process)."""
    root = Path(root or SPILL_ROOT)
    keep = {Path(folder).resolve() for folder in keep if folder is not None}
    for folder in root.glob("*") if root.is_dir() else ():
        parts = folder.name.split("_")
        owner = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else None
        if owner is not None and owner != os.getpid() and _process_alive(owner):
            continue  # another simulator window's results
        if folder.is_dir() and folder.resolve() not in keep:
            for path in folder.glob("*"):
                _loaded.pop(str(path), None)
            shutil.rmtree(folder, ignore_errors=True)


# --- the guard -------------------------------------------------------------------------------------------

class MemoryGuard:
    """A run's memory budget: `percent` of `baseline`, the bytes free when the simulator launched
    (measured now when not given). `measure` reads the bytes free now (see available_memory). Spilled
    chart series go to a new folder under `spill_root`."""

    def __init__(self, percent=DEFAULT_PERCENT, baseline=None, measure=available_memory, spill_root=None):
        if not 0 < percent <= 100:
            raise ValueError(f"The memory limit must be a percentage above 0 and at most 100, not {percent!r}")
        self.measure = measure
        self.percent = percent
        self.baseline = baseline if baseline is not None else measure()
        self.budget = None if not self.baseline else self.baseline * percent / 100
        self.spill_folder = Path(spill_root or SPILL_ROOT) / f"{time.strftime('%Y%m%d_%H%M%S')}_{os.getpid()}_{id(self)}"
        self.spilling = False  # chart series go to disk from here on
        self.spilled = 0
        self.fewest_workers = None  # the fewest tasks the guard let run at once, when it slowed the run
        self.peak = 0

    def used(self):
        """Bytes the free memory has fallen by since launch (0 when it can't be read)."""
        now = self.measure()
        if now is None or not self.baseline:
            return 0
        used = max(0, self.baseline - now)
        self.peak = max(self.peak, used)
        return used

    def share(self):
        """The share of the budget in use now (0 without a budget)."""
        return self.used() / self.budget if self.budget else 0.0

    def _spill(self, result):
        if spill_series(result, self.spill_folder, self.spilled):
            self.spilled += 1

    def admit(self, new_results, collected=()):
        """Once memory passes SPILL_AT of the budget, write `new_results`' chart series to disk, and the
        first time, those of the results `collected` earlier too, letting the memory they held go."""
        if not self.spilling and self.share() >= SPILL_AT:
            self.spilling = True
            for result in collected:
                self._spill(result)
            gc.collect()
        if self.spilling:
            for result in new_results:
                self._spill(result)

    def slots(self, workers, running=0):
        """How many more tasks may start now, with `running` already running on up to `workers`: fewer
        as memory fills, down to one at a time at the budget (so a new task waits for the running ones to
        finish and free their memory). Never 0 when nothing is running, so a run always moves on."""
        share = self.share()
        step = next((fraction for at, fraction in THROTTLE if share >= at), None)
        if step is None:
            return max(0, workers - running)
        allowed = max(1, int(workers * step))
        self.fewest_workers = allowed if self.fewest_workers is None else min(self.fewest_workers, allowed)
        return max(0, allowed - running)

    def describe(self):
        """A note for the run's status line, or "" when the guard didn't step in."""
        notes = []
        if self.spilled:
            notes.append(f"chart data of {self.spilled:,} results kept on disk")
        if self.fewest_workers is not None:
            notes.append(f"slowed to {self.fewest_workers} task{'s' if self.fewest_workers != 1 else ''} at a time")
        if not notes:
            return ""
        return (" and ".join(notes) + f" to stay under the memory limit (peak {format_bytes(self.peak)} of "
                f"{format_bytes(self.budget)}, {self.percent:g}% of the {format_bytes(self.baseline)} free at launch)")
